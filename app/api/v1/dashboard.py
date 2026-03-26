# app/api/v1/dashboard.py
"""
Dashboard API — optimized queries + Redis caching on expensive aggregations.

Two remaining issues fixed from the logs:
  1. Recent advisories used SELECT * — pulled image_b64 (2MB blobs) for display
     that only needs id, farmer_phone, query_text, status, channel, created_at.
     Fixed with explicit column selection.

  2. /stats runs a table-scan aggregation on every page load.
     The numbers only change when a new advisory/field/farmer is created —
     not every few seconds. Cache it for 60s in Redis.
"""

import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, and_
import redis.asyncio as aioredis

from app.core.dependencies import get_db, get_redis, require_extension_worker
from app.models.farmer import Farmer
from app.models.field import Field
from app.models.advisory import AdvisoryRequest, AdvisoryStatus

router = APIRouter()

STATS_CACHE_KEY = "dashboard:stats"
STATS_CACHE_TTL = 60  # seconds — stats refresh at most once per minute


# ── 1. Summary stats — Redis-cached ──────────────────────────
@router.get("/stats")
async def get_dashboard_stats(
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    _=Depends(require_extension_worker),
):
    # Serve from cache if fresh
    cached = await redis.get(STATS_CACHE_KEY)
    if cached:
        return json.loads(cached)

    week_ago = datetime.utcnow() - timedelta(days=7)

    # Single aggregation query for farmer/field metrics
    stats = await db.execute(
        select(
            func.count(Farmer.id).filter(Farmer.is_active == True).label("total_farmers"),
            func.count(Field.id).filter(
                Field.is_active == True, Farmer.is_active == True
            ).label("total_fields"),
            func.count(Field.id).filter(
                Field.is_active == True,
                Farmer.is_active == True,
                Field.ndvi_score < 0.4,
                Field.ndvi_score.isnot(None),
            ).label("fields_poor_health"),
            func.avg(Field.ndvi_score).filter(
                Field.is_active == True,
                Farmer.is_active == True,
                Field.ndvi_score.isnot(None),
            ).label("avg_ndvi"),
        )
        .select_from(Farmer)
        .outerjoin(Field, Field.farmer_phone == Farmer.phone_number)
    )
    row = stats.one()

    advisory_count = await db.scalar(
        select(func.count(AdvisoryRequest.id))
        .join(Farmer, AdvisoryRequest.farmer_phone == Farmer.phone_number)
        .where(
            AdvisoryRequest.created_at >= week_ago,
            AdvisoryRequest.status == AdvisoryStatus.completed,
            Farmer.is_active == True,
        )
    )

    result = {
        "total_farmers":        row.total_farmers or 0,
        "total_fields":         row.total_fields or 0,
        "advisories_this_week": advisory_count or 0,
        "fields_poor_health":   row.fields_poor_health or 0,
        "avg_ndvi":             round(row.avg_ndvi, 3) if row.avg_ndvi else None,
    }

    await redis.setex(STATS_CACHE_KEY, STATS_CACHE_TTL, json.dumps(result))
    return result


# ── 2. Farmer list — 2 queries total, no blobs ───────────────
@router.get("/farmers")
async def get_farmers_overview(
    db: AsyncSession = Depends(get_db),
    _=Depends(require_extension_worker),
):
    week_ago = datetime.utcnow() - timedelta(days=7)

    farmer_rows = await db.execute(
        select(
            Farmer.id,
            Farmer.name,
            Farmer.phone_number,
            Farmer.district,
            Farmer.village,
            Farmer.preferred_language,
            Farmer.created_at,
            func.count(Field.id).filter(Field.is_active == True).label("total_fields"),
            func.avg(Field.ndvi_score).filter(
                Field.is_active == True,
                Field.ndvi_score.isnot(None),
            ).label("avg_ndvi"),
        )
        .outerjoin(Field, Field.farmer_phone == Farmer.phone_number)
        .where(Farmer.is_active == True)
        .group_by(
            Farmer.id, Farmer.name, Farmer.phone_number,
            Farmer.district, Farmer.village,
            Farmer.preferred_language, Farmer.created_at,
        )
        .order_by(Farmer.created_at.desc())
    )
    farmers = farmer_rows.all()

    if not farmers:
        return []

    farmer_phones = [f.phone_number for f in farmers]
    advisory_rows = await db.execute(
        select(
            AdvisoryRequest.farmer_phone,
            func.count(AdvisoryRequest.id).label("count"),
        )
        .where(
            AdvisoryRequest.farmer_phone.in_(farmer_phones),
            AdvisoryRequest.created_at >= week_ago,
        )
        .group_by(AdvisoryRequest.farmer_phone)
    )
    advisory_counts = {row.farmer_phone: row.count for row in advisory_rows.all()}

    return [
        {
            "id":                   str(f.id),
            "name":                 f.name,
            "phone_number":         f.phone_number,
            "district":             f.district,
            "village":              f.village,
            "preferred_language":   f.preferred_language,
            "total_fields":         f.total_fields or 0,
            "avg_ndvi":             round(f.avg_ndvi, 3) if f.avg_ndvi else None,
            "health_status":        _ndvi_to_status(f.avg_ndvi),
            "advisories_this_week": advisory_counts.get(f.phone_number, 0),
            "joined":               f.created_at.isoformat(),
        }
        for f in farmers
    ]


# ── 3. Single farmer detail ───────────────────────────────────
@router.get("/farmers/{phone_number}")
async def get_farmer_detail(
    phone_number: str,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_extension_worker),
):
    farmer = await db.scalar(
        select(Farmer).where(Farmer.phone_number == phone_number)
    )
    if not farmer:
        raise HTTPException(status_code=404, detail="Farmer not found")

    # Fields — no blobs, explicit columns
    fields_result = await db.execute(
        select(
            Field.id, Field.name, Field.crop_type, Field.area_ha,
            Field.latitude, Field.longitude, Field.soil_type,
            Field.irrigation_method, Field.ndvi_score, Field.ndvi_updated_at,
        )
        .where(Field.farmer_phone == phone_number, Field.is_active == True)
        .order_by(Field.created_at.desc())
    )
    fields = fields_result.all()

    # Advisories — explicitly exclude image_b64 (was SELECT * before)
    advisories_result = await db.execute(
        select(
            AdvisoryRequest.id,
            AdvisoryRequest.query_text,
            AdvisoryRequest.response_text,
            AdvisoryRequest.status,
            AdvisoryRequest.channel,
            AdvisoryRequest.created_at,
        )
        .where(AdvisoryRequest.farmer_phone == phone_number)
        .order_by(desc(AdvisoryRequest.created_at))
        .limit(10)
    )
    advisories = advisories_result.all()

    return {
        "farmer": {
            "id":                 str(farmer.id),
            "name":               farmer.name,
            "phone_number":       farmer.phone_number,
            "village":            farmer.village,
            "district":           farmer.district,
            "preferred_language": farmer.preferred_language,
            "crop_profile":       farmer.crop_profile,
            "joined":             farmer.created_at.isoformat(),
        },
        "fields": [
            {
                "id":                str(f.id),
                "name":              f.name,
                "crop_type":         f.crop_type,
                "area_ha":           f.area_ha,
                "latitude":          f.latitude,
                "longitude":         f.longitude,
                "soil_type":         f.soil_type,
                "irrigation_method": f.irrigation_method,
                "ndvi_score":        f.ndvi_score,
                "health_status":     _ndvi_to_status(f.ndvi_score),
                "ndvi_updated_at":   f.ndvi_updated_at.isoformat() if f.ndvi_updated_at else None,
            }
            for f in fields
        ],
        "recent_advisories": [
            {
                "id":         str(a.id),
                "query":      a.query_text,
                "response":   a.response_text,
                "status":     a.status,
                "channel":    a.channel,
                "created_at": a.created_at.isoformat(),
            }
            for a in advisories
        ],
    }


# ── 4. Field health map ───────────────────────────────────────
@router.get("/field-health")
async def get_field_health_map(
    db: AsyncSession = Depends(get_db),
    _=Depends(require_extension_worker),
):
    result = await db.execute(
        select(
            Field.id, Field.name, Field.farmer_phone, Field.crop_type,
            Field.latitude, Field.longitude, Field.ndvi_score, Field.ndvi_updated_at,
        )
        .join(Farmer, Field.farmer_phone == Farmer.phone_number)
        .where(
            Field.is_active == True,
            Field.latitude.isnot(None),
            Field.longitude.isnot(None),
            Farmer.is_active == True,
        )
        .order_by(Field.ndvi_score.asc().nulls_last())
    )
    fields = result.all()

    return [
        {
            "field_id":        str(f.id),
            "field_name":      f.name,
            "farmer_phone":    f.farmer_phone,
            "crop_type":       f.crop_type,
            "latitude":        f.latitude,
            "longitude":       f.longitude,
            "ndvi_score":      f.ndvi_score,
            "health_status":   _ndvi_to_status(f.ndvi_score),
            "ndvi_updated_at": f.ndvi_updated_at.isoformat() if f.ndvi_updated_at else None,
        }
        for f in fields
    ]


# ── 5. Recent advisories — explicit columns, no image_b64 ────
@router.get("/advisories/recent")
async def get_recent_advisories(
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_extension_worker),
):
    # BEFORE: SELECT * pulled image_b64 (up to 2MB per row) across the wire.
    # AFTER:  Explicit columns — only what the frontend actually uses.
    result = await db.execute(
        select(
            AdvisoryRequest.id,
            AdvisoryRequest.farmer_phone,
            AdvisoryRequest.query_text,
            AdvisoryRequest.status,
            AdvisoryRequest.channel,
            AdvisoryRequest.created_at,
        )
        .join(Farmer, AdvisoryRequest.farmer_phone == Farmer.phone_number)
        .where(Farmer.is_active == True)
        .order_by(desc(AdvisoryRequest.created_at))
        .limit(min(limit, 100))
    )
    advisories = result.all()

    return [
        {
            "id":           str(a.id),
            "farmer_phone": a.farmer_phone,
            "query":        a.query_text,
            "status":       a.status,
            "channel":      a.channel,
            "created_at":   a.created_at.isoformat(),
        }
        for a in advisories
    ]


# ── Helper ────────────────────────────────────────────────────
def _ndvi_to_status(ndvi: float | None) -> str:
    if ndvi is None: return "unknown"
    if ndvi >= 0.6:  return "good"
    if ndvi >= 0.4:  return "moderate"
    return "poor"