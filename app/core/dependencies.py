# app/core/dependencies.py

import json
import uuid
import redis.asyncio as aioredis

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select

from app.core.config import settings
from app.core.security import decode_access_token

# ── PostgreSQL ────────────────────────────────────────────────
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    # Pool tuning — defaults are too conservative for production
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,       # detect stale connections before using them
    pool_recycle=3600,        # recycle connections every hour
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


# ── Redis ─────────────────────────────────────────────────────
_redis_pool: aioredis.Redis | None = None

def get_redis_pool() -> aioredis.Redis:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            max_connections=20,
        )
    return _redis_pool

async def get_redis() -> aioredis.Redis:
    yield get_redis_pool()


# ── Auth with Redis caching ───────────────────────────────────
#
# Problem visible in logs:
#   Every request fired "SELECT users ... WHERE users.id = $1::UUID"
#   Even back-to-back requests from the same user within seconds.
#   That's a DB round trip that adds ~10-50ms to every API call.
#
# Fix:
#   Cache the user record in Redis for 5 minutes keyed by user_id.
#   First request hits DB and warms the cache.
#   All subsequent requests within 5 min read from Redis (~0.3ms).
#   Cache is invalidated on logout or if user is deactivated.
#
# TTL is 5 minutes — short enough that role changes propagate quickly,
# long enough to eliminate the DB hit on dashboard polling.

AUTH_CACHE_TTL = 300  # 5 minutes
AUTH_CACHE_KEY = "auth:user:{user_id}"

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    payload = decode_access_token(token)
    if not payload:
        raise credentials_exception

    user_id: str = payload.get("sub")
    if not user_id:
        raise credentials_exception

    # 1. Try Redis cache first
    cache_key = AUTH_CACHE_KEY.format(user_id=user_id)
    cached = await redis.get(cache_key)
    if cached:
        user_data = json.loads(cached)
        # Return a lightweight namespace object — avoids ORM overhead entirely
        return _UserProxy(user_data)

    # 2. Cache miss — hit DB
    from app.models.user import User
    result = await db.execute(
        select(User).where(User.id == uuid.UUID(user_id))
    )
    user = result.scalar_one_or_none()

    if not user or not user.is_active:
        raise credentials_exception

    # 3. Populate cache
    user_data = {
        "id":           str(user.id),
        "email":        user.email,
        "phone_number": user.phone_number,
        "role":         user.role.value,
        "is_active":    user.is_active,
        "created_at":   user.created_at.isoformat(),
    }
    await redis.setex(cache_key, AUTH_CACHE_TTL, json.dumps(user_data))

    return _UserProxy(user_data)


async def invalidate_user_cache(user_id: str, redis: aioredis.Redis):
    """Call this on logout or when a user's role/status changes."""
    cache_key = AUTH_CACHE_KEY.format(user_id=user_id)
    await redis.delete(cache_key)


class _UserProxy:
    """
    Lightweight stand-in for the SQLAlchemy User ORM object.
    Returned from cache — no DB session needed, no ORM overhead.
    Matches the same attribute interface so all existing code works unchanged.
    """
    __slots__ = ("id", "email", "phone_number", "role", "is_active", "created_at")

    def __init__(self, data: dict):
        from app.models.user import UserRole
        self.id           = uuid.UUID(data["id"])
        self.email        = data["email"]
        self.phone_number = data.get("phone_number")
        self.role         = UserRole(data["role"])
        self.is_active    = data["is_active"]
        self.created_at   = data["created_at"]


async def require_extension_worker(current_user=Depends(get_current_user)):
    from app.models.user import UserRole
    if current_user.role not in (UserRole.extension_worker, UserRole.admin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Extension worker or admin role required",
        )
    return current_user


async def require_admin(current_user=Depends(get_current_user)):
    from app.models.user import UserRole
    if current_user.role != UserRole.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return current_user