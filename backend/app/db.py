"""Database engine and session management.

One engine per process, one session per request. The session dependency is
where transaction boundaries live: commit on success, rollback on any
exception, always close.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def build_engine(url: str | None = None) -> AsyncEngine:
    return create_async_engine(
        url or settings.database_url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        # Recycle before most cloud proxies drop idle connections, and verify
        # liveness on checkout so a stale connection surfaces here rather than
        # as a confusing mid-request error.
        pool_recycle=1800,
        pool_pre_ping=True,
    )


engine: AsyncEngine = build_engine()

SessionFactory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Request-scoped session.

    Injected with Depends(get_db). Overridden wholesale in tests via
    app.dependency_overrides -- which is the practical reason to put the
    session behind a dependency instead of importing it directly.
    """
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def database_reachable() -> bool:
    """Can we get a working connection out of the pool the app actually uses?

    THE POINT OF GOING THROUGH SessionFactory RATHER THAN THE ENGINE.

    The first version of the readiness probe called `engine.connect()` on the
    module-level engine directly. That looks equivalent and is not: it checks a
    connection, but not *the* connection path requests take. A readiness probe
    that passes while every request fails is worse than no probe, because it
    keeps the replica in the load balancer.

    The test suite proved the point immediately. It builds its own engine bound
    to the test event loop and overrides the session dependency; the readiness
    endpoint kept using the module-level engine, and asyncpg refused with
    "attached to a different loop". The endpoint reported 503 while every other
    request in the same test worked perfectly, which is exactly the class of
    disagreement this function exists to eliminate.

    Never raises. The caller wants a boolean, not an exception to catch.
    """
    try:
        async with SessionFactory() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
