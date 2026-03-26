# app/models/advisory.py

import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, Text, Enum as SAEnum, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models import Base
import enum


class AdvisoryStatus(str, enum.Enum):
    pending    = "pending"
    processing = "processing"
    completed  = "completed"
    failed     = "failed"


class AdvisoryRequest(Base):
    __tablename__ = "advisory_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    farmer_phone: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("farmers.phone_number", ondelete="CASCADE"),
        nullable=False,
    )
    message_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    query_text: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    # base64 image for direct web uploads — kept short-term, cleared after processing
    image_b64: Mapped[str | None] = mapped_column(Text)
    image_media_type: Mapped[str | None] = mapped_column(String(50))
    response_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[AdvisoryStatus] = mapped_column(
        SAEnum(AdvisoryStatus), default=AdvisoryStatus.pending, nullable=False
    )
    channel: Mapped[str] = mapped_column(String(20), default="whatsapp", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    # Relationship — lets us do advisory.farmer without a separate query
    farmer: Mapped["Farmer"] = relationship(  # noqa: F821
        "Farmer",
        back_populates="advisory_requests",
        foreign_keys=[farmer_phone],
    )

    __table_args__ = (
        # Most-used query patterns get explicit indexes
        Index("ix_advisory_farmer_phone", "farmer_phone"),
        Index("ix_advisory_farmer_phone_status", "farmer_phone", "status"),
        Index("ix_advisory_status", "status"),
        Index("ix_advisory_created_at", "created_at"),
        # Composite for the "stuck in processing" cleanup job
        Index("ix_advisory_status_updated_at", "status", "updated_at"),
    )