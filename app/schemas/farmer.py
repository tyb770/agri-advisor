import uuid
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, field_validator

class FarmerCreate(BaseModel):
    phone_number: str
    name: str
    village: Optional[str] = None
    district: Optional[str] = None
    preferred_language: str = "ur"
    crop_profile: Optional[dict] = None

    @field_validator("phone_number")
    @classmethod
    def phone_must_start_with_plus(cls, v):
        if not v.startswith("+"):
            raise ValueError("Phone number must be in international format e.g. +923001234567")
        return v

class FarmerResponse(BaseModel):
    id: uuid.UUID
    phone_number: str
    name: str
    village: Optional[str]
    district: Optional[str]
    preferred_language: str
    crop_profile: Optional[dict]
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}