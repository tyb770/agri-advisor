# app/models/field.py

import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, Float, ForeignKey, Text, Boolean, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models import Base
# from app.models.farmer import Farmer


class Field(Base):
    __tablename__ = "fields"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    farmer_phone: Mapped[str] = mapped_column(
        String(20), ForeignKey("farmers.phone_number", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    crop_type: Mapped[str] = mapped_column(String(50), nullable=False)
    area_ha: Mapped[float | None] = mapped_column(Float)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    soil_type: Mapped[str | None] = mapped_column(String(50))
    irrigation_method: Mapped[str | None] = mapped_column(String(50))
    ndvi_score: Mapped[float | None] = mapped_column(Float)
    ndvi_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    extra_data: Mapped[dict | None] = mapped_column(JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relationship back to Farmer — enables farmer.fields and field.farmer
    farmer: Mapped["Farmer"] = relationship("Farmer", back_populates="fields")  # noqa: F821

    # Explicit indexes — these were missing, causing full table scans
    __table_args__ = (
        Index("ix_fields_farmer_phone", "farmer_phone"),
        Index("ix_fields_farmer_phone_active", "farmer_phone", "is_active"),
        Index("ix_fields_lat_lng", "latitude", "longitude"),
        Index("ix_fields_ndvi_score", "ndvi_score"),
    )