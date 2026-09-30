"""Shared fixtures.

The database engine is module-scoped and paired with a module-scoped event
loop (``asyncio_default_*_loop_scope`` in pyproject). Without that pairing,
pooled asyncpg connections outlive the loop they were created on and every
query fails with "transaction is in error state".
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import GovernanceEventRow, GovernanceRunRow

DATABASE_URL = "postgresql+asyncpg://governance:governance@localhost:5433/governance"


@pytest_asyncio.fixture(scope="module")
async def pg_engine():
    try:
        engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not available at {DATABASE_URL}: {exc}")
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="module")
async def pg_factory(pg_engine):
    return async_sessionmaker(bind=pg_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def clean_db(pg_factory):
    """Wipe the governance tables around each test.

    Table-level wipes (rather than per-test transactions) keep tests
    isolated even when a test deliberately aborts a transaction.
    """

    async def wipe():
        async with pg_factory() as session:
            await session.execute(delete(GovernanceEventRow))
            await session.execute(delete(GovernanceRunRow))
            await session.commit()

    await wipe()
    yield
    await wipe()
