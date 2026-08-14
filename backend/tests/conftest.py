"""Test fixtures.

Tests run against a real PostgreSQL database, not SQLite. The schema uses JSONB
and Postgres enums, and a SQLite test suite would pass while the production
queries failed -- which is worse than no test suite, because it is confidently
wrong.

Isolation comes from dropping and recreating the schema per test. Slower than
transactional rollback, and chosen deliberately: the streaming endpoint opens
its own session to persist a turn, so a single shared outer transaction would
deadlock against itself.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/cadence_test"
)
os.environ.setdefault("JWT_SECRET", "test-secret")

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.db import Base, get_db  # noqa: E402
from app.llm.client import ScriptedProvider  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402

TEST_DB_URL = os.environ["DATABASE_URL"]


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(autouse=True)
async def reset_rate_limiters():
    """Rate limiters are process-global; tests must not inherit each other's hits.

    Without this, the sixth test that registers a user gets a 429 from the fifth,
    and the failure surfaces somewhere unrelated to the cause. Shared mutable
    state between tests is the most confusing kind of flake, so it is cleared
    explicitly rather than hoped away.
    """
    from app import ratelimit

    limiters = [
        ratelimit.login_limit,
        ratelimit.register_limit,
        ratelimit.session_create_limit,
        ratelimit.stream_limit,
    ]
    for limiter in limiters:
        limiter.limiter.reset()
    yield
    for limiter in limiters:
        limiter.limiter.reset()


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(TEST_DB_URL, poolclass=None)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture
async def app(engine, session_factory, monkeypatch):
    """The real app, with the database and model provider swapped out.

    dependency_overrides is the reason get_db is a dependency rather than a
    module-level import: it makes the whole app testable without patching.
    """
    import app.db as db_module
    import app.routers.sessions as sessions_module

    monkeypatch.setattr(db_module, "SessionFactory", session_factory)
    monkeypatch.setattr(sessions_module, "SessionFactory", session_factory, raising=False)

    async def _override_db() -> AsyncGenerator:
        async with session_factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    fastapi_app.dependency_overrides[get_db] = _override_db
    # Zero delay: the streaming behaviour is what is under test, not the pacing.
    fastapi_app.state.llm = ScriptedProvider(delay=0.0)
    yield fastapi_app
    fastapi_app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app) -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _register(client: AsyncClient, email: str, name: str) -> str:
    resp = await client.post(
        "/auth/register",
        json={"email": email, "password": "correct-horse-battery", "display_name": name},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"]


@pytest_asyncio.fixture
async def auth_client(client: AsyncClient) -> AsyncClient:
    token = await _register(client, "rodrigo@example.com", "Rodrigo")
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest_asyncio.fixture
async def other_client(app) -> AsyncGenerator[AsyncClient, None]:
    """A second, unrelated account -- used to prove cross-user isolation."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        token = await _register(c, "someone.else@example.com", "Someone Else")
        c.headers["Authorization"] = f"Bearer {token}"
        yield c
