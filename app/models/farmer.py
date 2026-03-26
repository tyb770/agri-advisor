# app/models/farmer.py

import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, Text, Boolean, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models import Base
from app.models.advisory import AdvisoryRequest
from app.models.field import Field


class Farmer(Base):
    __tablename__ = "farmers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    phone_number: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    village: Mapped[str | None] = mapped_column(String(100))
    district: Mapped[str | None] = mapped_column(String(100))
    preferred_language: Mapped[str] = mapped_column(String(10), default="ur")
    crop_profile: Mapped[dict | None] = mapped_column(JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships — allow ORM-level joins instead of N+1 queries
    fields: Mapped[list["Field"]] = relationship(  # noqa: F821
        "Field",
        back_populates="farmer",
        primaryjoin="and_(Farmer.phone_number == Field.farmer_phone, Field.is_active == True)",
        lazy="select",
    )
    advisory_requests: Mapped[list["AdvisoryRequest"]] = relationship(  # noqa: F821
        "AdvisoryRequest",
        back_populates="farmer",
        foreign_keys="AdvisoryRequest.farmer_phone",
        lazy="select",
    )

    __table_args__ = (
        Index("ix_farmers_is_active", "is_active"),
        Index("ix_farmers_district", "district"),
    )

    def __repr__(self):
        return f"<Farmer {self.name} ({self.phone_number})>"