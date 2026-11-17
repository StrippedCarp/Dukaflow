"""Shared test setup.

Tests run against a *separate* Postgres database (dukaflow_test) so they can
never touch your development data. The schema is built by running the real
Alembic migrations, so the migrations themselves get tested too.
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

APP_DIR = Path(__file__).resolve().parent.parent


def _resolve_test_database_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    dev_url = os.environ.get("DATABASE_URL") or dotenv_values(APP_DIR / ".env").get("DATABASE_URL")
    if not dev_url:
        raise RuntimeError("Set TEST_DATABASE_URL or DATABASE_URL to run the tests")
    return make_url(dev_url).set(database="dukaflow_test").render_as_string(hide_password=False)


TEST_DATABASE_URL = _resolve_test_database_url()
if not (make_url(TEST_DATABASE_URL).database or "").endswith("_test"):
    raise RuntimeError("Refusing to run tests: database name must end with '_test'")

# Must happen BEFORE the app is imported, because core.config reads the
# environment at import time and environment variables override .env.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["REDIS_URL"] = "redis://127.0.0.1:6380/15"
os.environ["WHATSAPP_VERIFY_TOKEN"] = "test-verify-token"
os.environ["WHATSAPP_APP_SECRET"] = "test-whatsapp-app-secret"
os.environ["MPESA_WEBHOOK_SECRET"] = "test-mpesa-webhook-secret"

from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from redis import Redis as SyncRedis  # noqa: E402
from redis.asyncio import Redis  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from alembic import command  # noqa: E402
from core.config import settings  # noqa: E402
from core.database import AsyncSessionLocal, engine  # noqa: E402
from core.redis import redis_pool  # noqa: E402
from main import app  # noqa: E402

SYNC_TEST_URL = TEST_DATABASE_URL.replace("postgresql+asyncpg", "postgresql+psycopg2")


def alembic_config() -> Config:
    cfg = Config(str(APP_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(APP_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", SYNC_TEST_URL)
    return cfg


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> Iterator[None]:
    """Build the schema from scratch with migrations and clear test Redis."""
    cfg = alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    sync_redis = SyncRedis.from_url(settings.redis_url)
    try:
        sync_redis.flushdb()
    finally:
        sync_redis.close()
    yield


@pytest.fixture(autouse=True)
async def clean_tables() -> AsyncIterator[None]:
    """Give every test empty tables and Redis keys, then release connections."""
    yield
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE order_items, orders, products, merchants RESTART IDENTITY CASCADE")
        )
    # Connections are tied to the test's event loop; drop them so the next test starts fresh.
    await engine.dispose()
    redis_client = Redis(connection_pool=redis_pool)
    try:
        await redis_client.flushdb()
    finally:
        await redis_client.aclose()
        await redis_pool.disconnect()


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
