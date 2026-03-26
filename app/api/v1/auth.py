# app/api/v1/auth.py

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import redis.asyncio as aioredis

from app.core.dependencies import get_db, get_current_user, get_redis, invalidate_user_cache
from app.core.rate_limit import login_limit
from app.core.security import hash_password, verify_password, create_access_token
from app.models.user import User
from app.schemas.user import UserCreate, UserResponse, TokenResponse

router = APIRouter()


@router.post("/register", response_model=UserResponse, status_code=201)
async def register(payload: UserCreate, db: AsyncSession = Depends(get_db)):
    """
    Internal endpoint — creates extension worker / admin accounts.
    Not exposed to farmers. No rate limit needed (admin-only operation).
    """
    existing = await db.execute(select(User).where(User.email == payload.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already registered")

    user = User(
        email=payload.email,
        phone_number=payload.phone_number,
        hashed_password=hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post(
    "/login",
    response_model=TokenResponse,
    dependencies=[Depends(login_limit)],
)
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """
    Rate-limited to 10 attempts per IP per 15 minutes.
    Prevents credential stuffing against extension worker accounts.
    """
    result = await db.execute(select(User).where(User.email == form_data.username))
    user = result.scalar_one_or_none()

    # Deliberate: same error for wrong email AND wrong password
    # Don't leak which one failed — attacker shouldn't know if the email exists
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")

    token = create_access_token({"sub": str(user.id), "role": user.role})
    return TokenResponse(access_token=token, user=UserResponse.model_validate(user))


@router.get("/me", response_model=UserResponse)
async def get_me(current_user=Depends(get_current_user)):
    return current_user


@router.post("/logout")
async def logout(
    current_user=Depends(get_current_user),
    redis: aioredis.Redis = Depends(get_redis),
):
    """Invalidates the Redis auth cache so role changes take effect immediately."""
    await invalidate_user_cache(str(current_user.id), redis)
    return {"status": "logged out"}