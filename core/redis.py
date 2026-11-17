"""Async Redis client dependency backed by a shared connection pool."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from redis.asyncio import ConnectionPool, Redis

from core.config import settings

redis_pool: ConnectionPool = ConnectionPool.from_url(settings.redis_url, decode_responses=True)


async def get_redis() -> AsyncIterator[Redis]:
    """Yield an async Redis client backed by the shared connection pool."""
    client: Redis = Redis(connection_pool=redis_pool)
    try:
        yield client
    finally:
        await client.aclose()


RedisClient = Annotated[Redis, Depends(get_redis)]
