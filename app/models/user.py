import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, Boolean, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base
import enum

class UserRole(str, enum.Enum):
    farmer = "farmer"
    extension_worker = "extension_worker"
    admin = "admin"

class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    phone_number: Mapped[str | None] = mapped_column(String(20), unique=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(SAEnum(UserRole), default=UserRole.farmer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)