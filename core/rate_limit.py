"""Redis-backed fixed-window rate limiter for public HTTP endpoints."""

import logging

from fastapi import HTTPException, Request
from redis.asyncio import Redis
from redis.exceptions import RedisError

from core.config import settings
from core.redis import RedisClient

logger = logging.getLogger("dukaflow")


def extract_client_ip(request: Request) -> str:
    """Return the direct peer IP address without trusting raw forwarding headers."""
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


async def enforce_rate_limit(
    redis: Redis,
    scope: str,
    client_ip: str,
    max_requests: int,
    window_seconds: int = 60,
) -> None:
    """Increment the request counter for ``(scope, client_ip)`` and raise 429 if exceeded."""
    redis_key = f"ratelimit:{scope}:{client_ip}"
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(redis_key)
            pipe.expire(redis_key, window_seconds, nx=True)
            pipe.ttl(redis_key)
            res = await pipe.execute()
    except RedisError:
        logger.warning("Redis unavailable for rate limit scope=%s; failing open", scope)
        return

    count = int(res[0])
    ttl = int(res[2])
    if count > max_requests:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Please try again later.",
            headers={"Retry-After": str(max(ttl, 1))},
        )


async def rate_limit_merchant_registration(request: Request, redis: RedisClient) -> None:
    """Throttle public merchant registration requests per client IP."""
    await enforce_rate_limit(
        redis,
        "merchant_register",
        extract_client_ip(request),
        settings.rate_limit_merchant_register_per_minute,
        60,
    )


async def rate_limit_whatsapp_verify(request: Request, redis: RedisClient) -> None:
    """Throttle incoming WhatsApp verification challenges per client IP."""
    await enforce_rate_limit(
        redis,
        "whatsapp_verify",
        extract_client_ip(request),
        settings.rate_limit_webhook_per_minute,
        60,
    )


async def rate_limit_whatsapp_webhook(request: Request, redis: RedisClient) -> None:
    """Throttle incoming WhatsApp Cloud API event webhooks per client IP."""
    await enforce_rate_limit(
        redis,
        "whatsapp_webhook",
        extract_client_ip(request),
        settings.rate_limit_webhook_per_minute,
        60,
    )


async def rate_limit_mpesa_webhook(request: Request, redis: RedisClient) -> None:
    """Throttle incoming Safaricom Daraja callback webhooks per client IP."""
    await enforce_rate_limit(
        redis,
        "mpesa_webhook",
        extract_client_ip(request),
        settings.rate_limit_webhook_per_minute,
        60,
    )
