"""Background scheduler loops coordinated via Redis leader locks."""

import asyncio
import logging
import uuid

from redis.asyncio import Redis
from redis.exceptions import RedisError

from core.database import AsyncSessionLocal
from core.redis import redis_pool
from services.mpesa import expire_all_stale_orders

logger = logging.getLogger("dukaflow")

SWEEP_LOCK_KEY = "mpesa:lock:expire_all_stale"


async def run_stale_order_sweep_tick(max_age_minutes: int = 15) -> int:
    """Run one Redis-locked pass to expire abandoned PENDING orders across all merchants."""
    lock_token = uuid.uuid4().hex
    redis_client = Redis(connection_pool=redis_pool)
    try:
        acquired = await redis_client.set(SWEEP_LOCK_KEY, lock_token, nx=True, ex=55)
        if not acquired:
            return 0
        try:
            async with AsyncSessionLocal() as db:
                expired = await expire_all_stale_orders(db, max_age_minutes=max_age_minutes)
            if expired > 0:
                logger.info("Expired %d stale pending order(s)", expired)
            return expired
        finally:
            try:
                if await redis_client.get(SWEEP_LOCK_KEY) == lock_token:
                    await redis_client.delete(SWEEP_LOCK_KEY)
            except RedisError:
                logger.warning("Failed to release stale-order sweep lock")
    finally:
        await redis_client.aclose()


async def stale_order_sweep_loop(
    stop_event: asyncio.Event,
    interval_seconds: float,
    max_age_minutes: int = 15,
) -> None:
    """Periodically sweep stale PENDING orders until ``stop_event`` is set."""
    while not stop_event.is_set():
        try:
            await run_stale_order_sweep_tick(max_age_minutes=max_age_minutes)
        except Exception:
            logger.exception("Stale order sweep tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
