# app/schemas/advisory.py

import uuid
from datetime import datetime
from typing import Optional
from pydantic import BaseModel
from app.models.advisory import AdvisoryStatus


class AdvisoryHistoryItem(BaseModel):
    """One advisory in a farmer's history list — no image blob, no response."""
    id: uuid.UUID
    query_text: Optional[str]
    status: AdvisoryStatus
    channel: str
    has_image: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class AdvisoryDetail(BaseModel):
    """Full advisory including the AI response — for the detail view."""
    id: uuid.UUID
    query_text: Optional[str]
    response_text: Optional[str]
    status: AdvisoryStatus
    channel: str
    has_image: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AdvisoryCreate(BaseModel):
    """Body for submitting a text-only advisory request (no image)."""
    farmer_phone: str
    query_text: str
    channel: str = "web"