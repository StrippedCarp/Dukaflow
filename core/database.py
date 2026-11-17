from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from core.config import settings

engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    """Parent class for every ORM model; holds the shared table metadata."""


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yields one database session per request."""
    async with AsyncSessionLocal() as session:
        yield session
