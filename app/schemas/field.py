# app/schemas/field.py

import uuid
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, field_validator

class FieldCreate(BaseModel):
    farmer_phone: str
    name: str
    crop_type: str
    area_ha: Optional[float] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    soil_type: Optional[str] = None
    irrigation_method: Optional[str] = None

    @field_validator("crop_type")
    @classmethod
    def crop_must_be_known(cls, v):
        allowed = {"wheat", "cotton", "rice", "sugarcane", "maize", "other"}
        if v.lower() not in allowed:
            raise ValueError(f"crop_type must be one of: {allowed}")
        return v.lower()

class FieldResponse(BaseModel):
    id: uuid.UUID
    farmer_phone: str
    name: str
    crop_type: str
    area_ha: Optional[float]
    latitude: Optional[float]
    longitude: Optional[float]
    soil_type: Optional[str]
    irrigation_method: Optional[str]
    ndvi_score: Optional[float]
    ndvi_updated_at: Optional[datetime]
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}