# app/core/rate_limit.py
"""
IP-based rate limiting using Redis sliding window counters.

Usage:
    from app.core.rate_limit import RateLimit

    # As a FastAPI dependency:
    @router.post("/register")
    async def register(
        request: Request,
        _=Depends(RateLimit(times=5, seconds=60)),
    ):
        ...

    # Multiple limits on one endpoint (strictest wins):
    @router.post("/scan")
    async def scan(
        request: Request,
        _=Depends(RateLimit(times=10, seconds=60)),   # 10/min
        __=Depends(RateLimit(times=50, seconds=3600)), # 50/hr
    ):
        ...
"""

import time
from fastapi import Request, HTTPException, Depends
import redis.asyncio as aioredis
from app.core.dependencies import get_redis


def _get_client_ip(request: Request) -> str:
    """
    Extract real client IP, respecting X-Forwarded-For from Nginx proxy.
    Falls back to direct connection IP if header not present.
    """
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # Header can be a comma-separated list — first entry is the real client
        return forwarded_for.split(",")[0].strip()
    return request.client.host


class RateLimit:
    """
    Sliding window rate limiter.

    Redis key: ratelimit:{endpoint_key}:{ip}
    Uses INCR + EXPIRE so the window resets cleanly after `seconds`.

    Args:
        times:    Max requests allowed in the window.
        seconds:  Window duration in seconds.
        key:      Optional override for the Redis key prefix.
                  Defaults to the request path so each endpoint has
                  its own independent counter.
    """

    def __init__(self, times: int, seconds: int, key: str | None = None):
        self.times   = times
        self.seconds = seconds
        self.key     = key

    async def __call__(
        self,
        request: Request,
        redis: aioredis.Redis = Depends(get_redis),
    ):
        ip          = _get_client_ip(request)
        endpoint    = self.key or request.url.path
        redis_key   = f"ratelimit:{endpoint}:{ip}"

        # Atomic increment
        count = await redis.incr(redis_key)

        if count == 1:
            # First request in this window — set the expiry
            await redis.expire(redis_key, self.seconds)

        if count > self.times:
            # Add Retry-After header so clients know when to back off
            ttl = await redis.ttl(redis_key)
            raise HTTPException(
                status_code=429,
                detail=f"Too many requests. Try again in {ttl} seconds.",
                headers={"Retry-After": str(ttl)},
            )


# ── Pre-built limiters for common patterns ────────────────────
# Import and use directly as dependencies:
#   Depends(farmer_registration_limit)

# Farmer registration: 5 registrations per IP per hour
# Prevents bulk fake-farmer creation from a single source
farmer_registration_limit = RateLimit(times=5, seconds=3600)

# Disease scan: 20 scans per IP per hour
# Each scan triggers an LLM call — this caps the cost exposure
disease_scan_limit = RateLimit(times=20, seconds=3600)

# Webhook: 100 messages per IP per minute
# WhatsApp cloud API sends from a fixed IP range — generous limit
# but stops accidental or malicious replay floods
webhook_limit = RateLimit(times=100, seconds=60)

# Auth login: 10 attempts per IP per 15 minutes
# Prevents credential stuffing
login_limit = RateLimit(times=10, seconds=900)