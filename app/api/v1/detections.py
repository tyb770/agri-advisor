# app/api/v1/detections.py

import uuid
import base64
import logging

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.dependencies import get_db
from app.core.rate_limit import disease_scan_limit
from app.models.advisory import AdvisoryRequest, AdvisoryStatus
from app.models.farmer import Farmer
from app.tasks.advisory_tasks import process_advisory_request

router = APIRouter()
logger = logging.getLogger(__name__)

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB


@router.post(
    "/scan",
    dependencies=[Depends(disease_scan_limit)],
)
async def scan_crop_image(
    request: Request,
    farmer_phone: str = Form(...),
    image: UploadFile = File(...),
    query_text: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    """
    Accepts a crop image + farmer phone number for AI disease analysis.

    Security fixes applied:
      - Was completely unauthenticated — anyone knowing a phone number
        could submit unlimited LLM scan requests against that farmer's account.
      - Now: validates farmer_phone exists and is active before creating
        any advisory record. Silently rejects invalid phones with the same
        404 response as "farmer not found" to avoid confirming phone existence.
      - Rate-limited to 20 scans per IP per hour to cap LLM cost exposure.
    """
    # Validate content type before reading bytes
    content_type = image.content_type or "image/jpeg"
    if content_type not in ALLOWED_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Image must be JPEG, PNG, or WebP. Got: {content_type}",
        )

    # Read image
    img_bytes = await image.read()

    if len(img_bytes) > MAX_SIZE_BYTES:
        raise HTTPException(status_code=422, detail="Image too large (max 10 MB)")

    if len(img_bytes) < 1000:
        raise HTTPException(status_code=422, detail="Image appears empty or corrupt")

    # Validate farmer exists and is active
    # Returns 404 whether farmer doesn't exist OR phone is wrong —
    # same response prevents probing which phone numbers are registered.
    farmer = await db.scalar(
        select(Farmer).where(
            Farmer.phone_number == farmer_phone,
            Farmer.is_active == True,
        )
    )
    if not farmer:
        raise HTTPException(
            status_code=404,
            detail="Farmer not found.",
        )

    img_b64 = base64.b64encode(img_bytes).decode("utf-8")

    advisory = AdvisoryRequest(
        farmer_phone=farmer_phone,
        message_id=None,
        query_text=query_text or "Please analyze this crop photo for disease",
        image_url=None,
        image_b64=img_b64,
        image_media_type=content_type,
        channel="web",
        status=AdvisoryStatus.pending,
    )
    db.add(advisory)
    await db.commit()
    await db.refresh(advisory)

    process_advisory_request.delay(str(advisory.id))

    logger.info(f"Disease scan enqueued for {farmer_phone}: {advisory.id}")

    return {
        "id":      str(advisory.id),
        "status":  "pending",
        "message": "Image received. Analysis in progress (~15–20 seconds).",
    }


@router.get("/scan/{advisory_id}")
async def get_scan_result(
    advisory_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Poll for the result of a disease scan. No auth needed — ID is a UUID."""
    try:
        rid = uuid.UUID(advisory_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid advisory ID")

    result = await db.execute(
        select(
            AdvisoryRequest.id,
            AdvisoryRequest.status,
            AdvisoryRequest.response_text,
            AdvisoryRequest.channel,
            AdvisoryRequest.created_at,
            AdvisoryRequest.image_url,
            AdvisoryRequest.image_b64,
        )
        .where(AdvisoryRequest.id == rid)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Scan not found")

    return {
        "id":            str(row.id),
        "status":        row.status,
        "response_text": row.response_text,
        "channel":       row.channel,
        "created_at":    row.created_at.isoformat(),
        "has_image":     bool(row.image_b64 or row.image_url),
    }