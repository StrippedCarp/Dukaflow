import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import get_db
from core.observability import configure_logging, init_sentry, request_id_ctx, resolve_request_id
from core.redis import RedisClient
from routers.merchant import router as merchant_router
from routers.mpesa import router as mpesa_router
from routers.order import router as order_router
from routers.product import router as product_router
from routers.whatsapp import router as whatsapp_router
from services.scheduler import stale_order_sweep_loop

DbSession = Annotated[AsyncSession, Depends(get_db)]


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Configure observability and run the background stale-order sweep scheduler."""
    configure_logging(settings.log_level)
    init_sentry(settings.sentry_dsn, settings.sentry_environment)
    stop_event = asyncio.Event()
    sweep_task: asyncio.Task[None] | None = None
    if settings.stale_order_sweep_interval_seconds > 0:
        sweep_task = asyncio.create_task(
            stale_order_sweep_loop(
                stop_event,
                settings.stale_order_sweep_interval_seconds,
                settings.stale_order_max_age_minutes,
            )
        )
    try:
        yield
    finally:
        stop_event.set()
        if sweep_task is not None:
            await sweep_task


app = FastAPI(title="DukaFlow B2B Engine", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def request_id_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Attach a validated/generated correlation ID to contextvars and response headers."""
    req_id = resolve_request_id(request.headers.get("X-Request-ID"))
    token = request_id_ctx.set(req_id)
    try:
        response = await call_next(request)
    finally:
        request_id_ctx.reset(token)
    response.headers["X-Request-ID"] = req_id
    return response


@app.get("/health", response_class=PlainTextResponse)
async def health_check() -> str:
    """Liveness probe for hosting platforms / uptime monitors."""
    return "Service is healthy"


@app.get("/health/ready")
async def readiness_check(db: DbSession, redis: RedisClient) -> dict[str, str]:
    """Readiness probe verifying live PostgreSQL and Redis connectivity."""
    try:
        await db.execute(text("SELECT 1"))
        await redis.ping()
    except Exception:
        logging.getLogger("dukaflow").exception("Readiness check failed")
        raise HTTPException(status_code=503, detail="Service dependency unavailable") from None
    return {"status": "ready", "database": "ok", "redis": "ok"}


app.include_router(merchant_router)
app.include_router(product_router)
app.include_router(order_router)
app.include_router(mpesa_router)
app.include_router(whatsapp_router)
