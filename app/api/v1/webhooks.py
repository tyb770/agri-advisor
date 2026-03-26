# app/api/v1/webhooks.py
"""
WhatsApp webhook — single entry point for all farmer messages.

Routing logic per incoming message:
  1. Parse payload → normalised message dict
  2. Idempotency check on message_id
  3. Does the farmer have an active onboarding session?  OR  is not yet registered?
       YES  →  onboarding state machine (handle_onboarding)
       NO   →  advisory pipeline (process_advisory_request Celery task)
"""

import logging
from fastapi import APIRouter, Request, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.dependencies import get_db, get_redis
from app.core.config import settings
from app.models.advisory import AdvisoryRequest, AdvisoryStatus
from app.services.whatsapp import extract_message_from_payload, send_whatsapp_message
from app.services.onboarding import (
    get_session,
    is_farmer_registered,
    handle_onboarding,
)
from app.tasks.advisory_tasks import process_advisory_request

logger = logging.getLogger(__name__)
router = APIRouter()


# ── WhatsApp webhook verification (GET) ───────────────────────
@router.get("/webhook")
async def verify_whatsapp_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    """
    Meta calls this GET when you register the webhook URL.
    Must echo hub.challenge to prove server ownership.
    """
    if hub_mode == "subscribe" and hub_verify_token == settings.WHATSAPP_VERIFY_TOKEN:
        return int(hub_challenge)
    raise HTTPException(status_code=403, detail="Verification failed")


# ── Main WhatsApp webhook (POST) ──────────────────────────────
@router.post("/webhook")
async def receive_whatsapp(
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    payload = await request.json()

    # ── 1. Parse message ──────────────────────────────────────
    # Try real WhatsApp Cloud API format first.
    # extract_message_from_payload handles text / image / location.
    message = extract_message_from_payload(payload)

    # Fall back to our local test/dev format:
    # { "message_id": "...", "from": "+92...", "text": "...",
    #   "image_url": "...", "latitude": 31.5, "longitude": 74.3 }
    if not message:
        message_id   = payload.get("message_id")
        farmer_phone = payload.get("from")
        if message_id and farmer_phone:
            lat = payload.get("latitude")
            lng = payload.get("longitude")
            mtype = (
                "location" if (lat is not None and lng is not None)
                else "image"  if payload.get("image_url")
                else "text"
            )
            message = {
                "message_id":  message_id,
                "from_number": farmer_phone,
                "type":        mtype,
                "text":        payload.get("text"),
                "image_url":   payload.get("image_url"),
                "latitude":    float(lat) if lat is not None else None,
                "longitude":   float(lng) if lng is not None else None,
            }

    # Delivery receipts, read receipts, status callbacks — always 200
    if not message:
        return {"status": "ok"}

    message_id   = message["message_id"]
    farmer_phone = message["from_number"]
    text         = message.get("text") or ""
    image_url    = message.get("image_url")
    latitude     = message.get("latitude")
    longitude    = message.get("longitude")
    msg_type     = message.get("type", "text")   # "text" | "image" | "location"

    # ── 2. Idempotency ────────────────────────────────────────
    existing = await db.execute(
        select(AdvisoryRequest).where(AdvisoryRequest.message_id == message_id)
    )
    if existing.scalar_one_or_none():
        return {"status": "ok", "note": "already processed"}

    # ── 3. Route: onboarding vs advisory ─────────────────────
    session    = await get_session(redis, farmer_phone)
    registered = await is_farmer_registered(db, farmer_phone)

    # Send to onboarding if:
    #   a) farmer not in DB yet, OR
    #   b) farmer has an active onboarding session (mid-flow)
    needs_onboarding = (not registered) or (session is not None)

    if needs_onboarding:
        reply, completed = await handle_onboarding(
            phone=farmer_phone,
            text=text,
            redis=redis,
            db=db,
            latitude=latitude,
            longitude=longitude,
            msg_type=msg_type,
        )
        await send_whatsapp_message(farmer_phone, reply)
        return {
            "status":    "ok",
            "flow":      "onboarding",
            "completed": completed,
        }

    # ── Advisory path ─────────────────────────────────────────
    # Location messages from registered farmers don't make sense
    # as advisory queries — ignore them gracefully.
    if msg_type == "location":
        await send_whatsapp_message(
            farmer_phone,
            "شکریہ! آپ کی location موصول ہوئی۔\n"
            "فصل کے بارے میں مشورے کے لیے تصویر یا سوال بھیجیں۔\n\n"
            "_Thanks! For crop advice, please send a photo or ask a question._"
        )
        return {"status": "ok", "flow": "location_ignored"}

    advisory = AdvisoryRequest(
        farmer_phone=farmer_phone,
        message_id=message_id,
        query_text=text or None,
        image_url=image_url,
        channel="whatsapp",
        status=AdvisoryStatus.pending,
    )
    db.add(advisory)
    await db.commit()
    await db.refresh(advisory)

    process_advisory_request.delay(str(advisory.id))

    return {
        "status":      "ok",
        "flow":        "advisory",
        "advisory_id": str(advisory.id),
    }


# ── Status polling (dashboard / tests) ───────────────────────
@router.get("/advisory/{advisory_id}")
async def get_advisory_status(
    advisory_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AdvisoryRequest).where(AdvisoryRequest.id == advisory_id)
    )
    advisory = result.scalar_one_or_none()
    if not advisory:
        raise HTTPException(status_code=404, detail="Not found")

    return {
        "id":            str(advisory.id),
        "status":        advisory.status,
        "response_text": advisory.response_text,
        "created_at":    advisory.created_at,
    }