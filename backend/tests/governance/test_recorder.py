"""Tests for the PostgreSQL recorder (spec sections 14, 32, invariant 3).

These are integration tests: they need the docker-compose Postgres on
localhost:5433. They are skipped automatically when it is not reachable.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import GovernanceEventRow, GovernanceRunRow
from app.db.repositories import PostgresRecorder
from app.governance.errors import GovernanceRecordingError
from app.governance.governor import RuntimeGovernor
from app.governance.models import (
    ActionRequest,
    GovernanceContext,
    GovernanceScope,
)
from app.governance.policy import GovernancePolicy

DATABASE_URL = "postgresql+asyncpg://governance:governance@localhost:5433/governance"


@pytest_asyncio.fixture
async def engine():
    """Function-scoped engine: each test gets a fresh loop and connections.

    ``pool_pre_ping`` discards stale/broken connections, which matters
    because one test deliberately aborts a transaction to prove the foreign
    key rejects an event with no run.
    """
    try:
        eng = create_async_engine(DATABASE_URL, pool_pre_ping=True)
        async with eng.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not available at {DATABASE_URL}: {exc}")
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def make_recorder(engine):
    """Return a factory for recorders, all closed again at test teardown.

    Wiping the tables *before* each test gives isolation without needing
    per-test transactions, which matters because the FK-violation test
    deliberately leaves a transaction in an error state.
    """
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    recorders: list[PostgresRecorder] = []

    async def wipe() -> None:
        async with factory() as session:
            await session.execute(delete(GovernanceEventRow))
            await session.execute(delete(GovernanceRunRow))
            await session.commit()

    async def build() -> PostgresRecorder:
        recorder = PostgresRecorder(factory)
        recorders.append(recorder)
        return recorder

    await wipe()
    yield build
    for recorder in recorders:
        try:
            await recorder.aclose()
        except Exception:
            pass  # a poisoned session cannot always be closed cleanly
    await wipe()


def make_context(run_id: str) -> GovernanceContext:
    return GovernanceContext(
        run_id=run_id,
        agent_id="customer-support-agent",
        intent="Update customer contact information",
        scope=GovernanceScope(
            allowed_tools={"get_customer", "update_customer"},
            allowed_actions={"read", "update"},
            allowed_resources={"customer_profile"},
        ),
        policy_version="customer-support-v1",
    )


def policy_allowing(*tools: str) -> GovernancePolicy:
    return GovernancePolicy(
        version="customer-support-v1",
        allowed_tools=set(tools),
        allowed_actions={"read", "update"},
    )


async def add_run(recorder: PostgresRecorder, run_id: str) -> None:
    await recorder.add_run(
        run_id=run_id,
        agent_id="customer-support-agent",
        intent="Update customer contact information",
        scope={"allowed_tools": ["get_customer", "update_customer"]},
        policy_version="customer-support-v1",
    )


async def test_run_is_persisted(make_recorder):
    run_id = str(uuid4())
    recorder = await make_recorder()
    await add_run(recorder, run_id)

    async with recorder._session_factory() as session:
        row = await session.get(GovernanceRunRow, run_id)
        assert row is not None
        assert row.agent_id == "customer-support-agent"
        assert row.status == "running"
        assert row.scope["allowed_tools"] == ["get_customer", "update_customer"]


async def test_event_is_persisted_with_sanitized_arguments(make_recorder):
    run_id = str(uuid4())
    recorder = await make_recorder()
    await add_run(recorder, run_id)

    governor = RuntimeGovernor(recorder=recorder)
    await governor.authorize(
        make_context(run_id),
        ActionRequest(
            tool_name="update_customer",
            action="update",
            arguments={"customer_id": "123", "api_key": "sk-live-supersecret"},
        ),
        policy_allowing("update_customer"),
    )
    await recorder.flush()

    async with recorder._session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(GovernanceEventRow).where(
                        GovernanceEventRow.run_id == run_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].decision == "ALLOW"
        assert rows[0].tool_name == "update_customer"
        assert rows[0].arguments_sanitized["api_key"] == "[REDACTED]"
        assert "supersecret" not in str(rows[0].arguments_sanitized)
        assert rows[0].arguments_hash


async def test_denial_is_persisted(make_recorder):
    run_id = str(uuid4())
    recorder = await make_recorder()
    await add_run(recorder, run_id)

    governor = RuntimeGovernor(recorder=recorder)
    decision = await governor.authorize(
        make_context(run_id),
        ActionRequest(tool_name="refund_payment", action="update"),
        GovernancePolicy(version="customer-support-v1", denied_tools={"refund_payment"}),
    )
    await recorder.flush()

    assert decision.decision == "DENY"
    async with recorder._session_factory() as session:
        row = (
            (
                await session.execute(
                    select(GovernanceEventRow).where(
                        GovernanceEventRow.run_id == run_id
                    )
                )
            )
            .scalars()
            .one()
        )
        assert row.decision == "DENY"
        assert "refund_payment" in row.reason


async def test_event_without_a_run_is_rejected(make_recorder):
    """Invariant 3: every event must reference a valid run.

    The foreign key rejects the insert, and because the governor fails
    closed on recorder errors the caller sees GovernanceRecordingError with
    the IntegrityError as its cause.
    """
    run_id = str(uuid4())
    recorder = await make_recorder()
    governor = RuntimeGovernor(recorder=recorder)

    with pytest.raises(GovernanceRecordingError) as excinfo:
        await governor.authorize(
            make_context(run_id),
            ActionRequest(tool_name="get_customer", action="read"),
            policy_allowing("get_customer"),
        )
    assert isinstance(excinfo.value.__cause__, IntegrityError)


async def test_run_can_be_finalized(make_recorder):
    from datetime import datetime, timezone

    run_id = str(uuid4())
    recorder = await make_recorder()
    await add_run(recorder, run_id)
    await recorder.finalize_run(
        run_id=run_id,
        status="completed",
        ended_at=datetime.now(timezone.utc),
    )

    async with recorder._session_factory() as session:
        row = await session.get(GovernanceRunRow, run_id)
        assert row.status == "completed"
        assert row.ended_at is not None


async def test_run_summary_counts_decisions(make_recorder):
    run_id = str(uuid4())
    recorder = await make_recorder()
    await add_run(recorder, run_id)

    governor = RuntimeGovernor(recorder=recorder)
    policy = GovernancePolicy(
        version="customer-support-v1",
        allowed_tools={"get_customer", "update_customer"},
        allowed_actions={"read", "update"},
        denied_tools={"refund_payment"},
    )
    for action in (
        ActionRequest(tool_name="get_customer", action="read"),
        ActionRequest(tool_name="update_customer", action="update"),
        ActionRequest(tool_name="refund_payment", action="update"),
    ):
        await governor.authorize(make_context(run_id), action, policy)
    await recorder.flush()

    summary = await recorder.summarize_run(run_id)
    assert summary == {"total": 3, "allowed": 2, "denied": 1, "approval_required": 0}
