# app/api/v1/advisory.py
"""
Advisory endpoints — farmer-facing history and detail views.

Who uses what:
  GET  /advisory/farmer/{phone}          → farmer portal (no auth, rate-limited)
  GET  /advisory/farmer/{phone}/{id}     → farmer portal (no auth, rate-limited)
  POST /advisory/                        → farmer portal text-only queries
  GET  /advisory/status/{id}             → farmer portal polling scan results
  GET  /advisory/                        → dashboard (extension workers only)
"""

import uuid
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func

from app.core.dependencies import get_db, require_extension_worker
from app.core.rate_limit import RateLimit
from app.models.advisory import AdvisoryRequest, AdvisoryStatus
from app.models.farmer import Farmer
from app.schemas.advisory import AdvisoryHistoryItem, AdvisoryDetail, AdvisoryCreate

router = APIRouter()
logger = logging.getLogger(__name__)

# Farmer-facing endpoints are public but rate-limited
_farmer_read_limit  = RateLimit(times=30, seconds=60)   # 30 reads/min per IP
_farmer_write_limit = RateLimit(times=5,  seconds=60)   # 5 submissions/min per IP


# ── 1. Farmer's advisory history ─────────────────────────────
@router.get("/farmer/{farmer_phone}", response_model=list[AdvisoryHistoryItem])
async def get_farmer_advisory_history(
    farmer_phone: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _=Depends(_farmer_read_limit),
    limit: int = Query(default=20, le=50),
    offset: int = Query(default=0, ge=0),
):
    """
    Returns a paginated list of a farmer's past advisories.
    Used by the farmer portal to show advisory history.

    No auth required — farmer_phone is the identifier.
    Rate-limited to prevent enumeration.
    Deliberately excludes response_text (can be long) and image_b64 (can be huge)
    from the list view — those are in the detail endpoint.
    """
    # Confirm farmer exists before returning any data
    farmer = await db.scalar(
        select(Farmer).where(
            Farmer.phone_number == farmer_phone,
            Farmer.is_active == True,
        )
    )
    if not farmer:
        raise HTTPException(status_code=404, detail="Farmer not found")

    result = await db.execute(
        select(
            AdvisoryRequest.id,
            AdvisoryRequest.query_text,
            AdvisoryRequest.status,
            AdvisoryRequest.channel,
            AdvisoryRequest.image_url,
            AdvisoryRequest.image_b64,
            AdvisoryRequest.created_at,
        )
        .where(AdvisoryRequest.farmer_phone == farmer_phone)
        .order_by(desc(AdvisoryRequest.created_at))
        .limit(limit)
        .offset(offset)
    )
    rows = result.all()

    return [
        AdvisoryHistoryItem(
            id=row.id,
            query_text=row.query_text,
            status=row.status,
            channel=row.channel,
            has_image=bool(row.image_url or row.image_b64),
            created_at=row.created_at,
        )
        for row in rows
    ]


# ── 2. Single advisory detail ─────────────────────────────────
@router.get(
    "/farmer/{farmer_phone}/{advisory_id}",
    response_model=AdvisoryDetail,
)
async def get_advisory_detail(
    farmer_phone: str,
    advisory_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _=Depends(_farmer_read_limit),
):
    """
    Full advisory detail including the AI response text.
    Scoped to the farmer — can only retrieve their own advisories.
    """
    try:
        rid = uuid.UUID(advisory_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid advisory ID")

    # Scope to this farmer — prevents one farmer reading another's advisory
    result = await db.execute(
        select(
            AdvisoryRequest.id,
            AdvisoryRequest.query_text,
            AdvisoryRequest.response_text,
            AdvisoryRequest.status,
            AdvisoryRequest.channel,
            AdvisoryRequest.image_url,
            AdvisoryRequest.image_b64,
            AdvisoryRequest.created_at,
            AdvisoryRequest.updated_at,
        )
        .where(
            AdvisoryRequest.id == rid,
            AdvisoryRequest.farmer_phone == farmer_phone,
        )
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Advisory not found")

    return AdvisoryDetail(
        id=row.id,
        query_text=row.query_text,
        response_text=row.response_text,
        status=row.status,
        channel=row.channel,
        has_image=bool(row.image_url or row.image_b64),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ── 3. Submit a text-only advisory (no image) ─────────────────
@router.post("/", status_code=202)
async def submit_advisory(
    payload: AdvisoryCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _=Depends(_farmer_write_limit),
):
    """
    Farmer submits a text question from the portal (no photo).
    Returns immediately with advisory ID — client polls /advisory/status/{id}.

    202 Accepted means "queued, not yet complete."
    """
    from app.tasks.advisory_tasks import process_advisory_request

    # Validate farmer exists
    farmer = await db.scalar(
        select(Farmer).where(
            Farmer.phone_number == payload.farmer_phone,
            Farmer.is_active == True,
        )
    )
    if not farmer:
        raise HTTPException(status_code=404, detail="Farmer not found")

    if not payload.query_text.strip():
        raise HTTPException(status_code=422, detail="Query text cannot be empty")

    advisory = AdvisoryRequest(
        farmer_phone=payload.farmer_phone,
        query_text=payload.query_text.strip(),
        channel=payload.channel,
        status=AdvisoryStatus.pending,
    )
    db.add(advisory)
    await db.commit()
    await db.refresh(advisory)

    process_advisory_request.delay(str(advisory.id))

    logger.info(f"Text advisory queued: {advisory.id} for {payload.farmer_phone}")

    return {
        "id":      str(advisory.id),
        "status":  "pending",
        "message": "Advisory queued. Results available in ~15–20 seconds.",
    }


# ── 4. Poll advisory status ───────────────────────────────────
@router.get("/status/{advisory_id}")
async def get_advisory_status(
    advisory_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _=Depends(_farmer_read_limit),
):
    """
    Lightweight status poll — used by the portal while waiting for results.
    Returns status + response_text once completed.
    Does NOT require farmer_phone — UUID is unguessable enough for polling.
    """
    try:
        rid = uuid.UUID(advisory_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid advisory ID")

    result = await db.execute(
        select(
            AdvisoryRequest.id,
            AdvisoryRequest.status,
            AdvisoryRequest.response_text,
            AdvisoryRequest.updated_at,
        )
        .where(AdvisoryRequest.id == rid)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Advisory not found")

    return {
        "id":            str(row.id),
        "status":        row.status,
        "response_text": row.response_text,
        "updated_at":    row.updated_at.isoformat(),
    }


# ── 5. All advisories — extension workers only ────────────────
@router.get("/", dependencies=[Depends(require_extension_worker)])
async def list_all_advisories(
    db: AsyncSession = Depends(get_db),
    status: Optional[str] = Query(default=None),
    channel: Optional[str] = Query(default=None),
    days: int = Query(default=7, le=90),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    """
    Extension worker view — filterable list of all advisories across all farmers.
    Supports filtering by status, channel, and time window.
    """
    since = datetime.utcnow() - timedelta(days=days)

    query = (
        select(
            AdvisoryRequest.id,
            AdvisoryRequest.farmer_phone,
            AdvisoryRequest.query_text,
            AdvisoryRequest.status,
            AdvisoryRequest.channel,
            AdvisoryRequest.created_at,
            AdvisoryRequest.image_url,
            AdvisoryRequest.image_b64,
        )
        .join(Farmer, AdvisoryRequest.farmer_phone == Farmer.phone_number)
        .where(
            AdvisoryRequest.created_at >= since,
            Farmer.is_active == True,
        )
        .order_by(desc(AdvisoryRequest.created_at))
        .limit(limit)
        .offset(offset)
    )

    if status:
        try:
            query = query.where(
                AdvisoryRequest.status == AdvisoryStatus(status)
            )
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid status. Must be one of: {[s.value for s in AdvisoryStatus]}",
            )

    if channel:
        query = query.where(AdvisoryRequest.channel == channel)

    result = await db.execute(query)
    rows = result.all()

    # Total count for pagination
    count_query = (
        select(func.count(AdvisoryRequest.id))
        .join(Farmer, AdvisoryRequest.farmer_phone == Farmer.phone_number)
        .where(
            AdvisoryRequest.created_at >= since,
            Farmer.is_active == True,
        )
    )
    if status:
        count_query = count_query.where(
            AdvisoryRequest.status == AdvisoryStatus(status)
        )
    if channel:
        count_query = count_query.where(AdvisoryRequest.channel == channel)

    total = await db.scalar(count_query)

    return {
        "total":  total,
        "offset": offset,
        "limit":  limit,
        "items": [
            {
                "id":           str(r.id),
                "farmer_phone": r.farmer_phone,
                "query":        r.query_text,
                "status":       r.status,
                "channel":      r.channel,
                "has_image":    bool(r.image_url or r.image_b64),
                "created_at":   r.created_at.isoformat(),
            }
            for r in rows
        ],
    }