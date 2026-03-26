import uuid
from datetime import datetime
from pydantic import BaseModel, EmailStr
from app.models.user import UserRole

class UserCreate(BaseModel):
    email: EmailStr
    password: str
    phone_number: str | None = None
    role: UserRole = UserRole.farmer

class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    phone_number: str | None
    role: UserRole
    is_active: bool
    created_at: datetime
    model_config = {"from_attributes": True}

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse