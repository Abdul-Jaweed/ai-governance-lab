"""Repository / recorder implementations backed by PostgreSQL.

The recorder owns a single long-lived write session so that a run and all
of its events are committed atomically. Read helpers use short-lived
sessions so they observe committed data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import GovernanceEventRow, GovernanceRunRow
from app.governance.models import GovernanceEvent

SessionFactory = async_sessionmaker[AsyncSession]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PostgresRecorder:
    """Persists governance runs and events.

    Implements the ``GovernanceRecorder`` protocol consumed by the runtime
    governor, plus run-level helpers for the service and API layers.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    # -- write path ----------------------------------------------------------

    async def _write_session(self) -> AsyncSession:
        if self._session is None:
            self._session = self._session_factory()
        return self._session

    async def add_run(
        self,
        *,
        run_id: str,
        agent_id: str,
        intent: str,
        scope: dict[str, Any],
        policy_version: str,
        started_at: datetime | None = None,
    ) -> None:
        """Create a new run.

        Committed immediately so the run is visible while it is still
        executing, which is also what makes a mid-run crash auditable.
        """

        session = await self._write_session()
        session.add(
            GovernanceRunRow(
                run_id=run_id,
                agent_id=agent_id,
                intent=intent,
                scope=scope,
                policy_version=policy_version,
                started_at=started_at or _utcnow(),
                status="running",
            )
        )
        await session.commit()

    async def arecord(self, event: GovernanceEvent) -> None:
        """Persist one governance event and commit it (recorder protocol).

        A failed write is rolled back so the session stays usable for the
        next event; otherwise Postgres leaves the transaction aborted and
        every subsequent write fails with a confusing error.
        """

        session = await self._write_session()
        session.add(self._to_row(event))
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    async def flush(self) -> None:
        session = await self._write_session()
        await session.flush()

    async def commit(self) -> None:
        session = await self._write_session()
        await session.commit()

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def finalize_run(
        self, *, run_id: str, status: str, ended_at: datetime | None = None
    ) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(GovernanceRunRow)
                .where(GovernanceRunRow.run_id == run_id)
                .values(status=status, ended_at=ended_at or _utcnow())
            )
            await session.commit()

    # -- read path -----------------------------------------------------------

    async def get_run(self, run_id: str) -> GovernanceRunRow | None:
        async with self._session_factory() as session:
            return await session.get(GovernanceRunRow, run_id)

    async def list_runs(
        self, *, limit: int = 50, offset: int = 0
    ) -> list[GovernanceRunRow]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(GovernanceRunRow)
                .order_by(GovernanceRunRow.started_at.desc())
                .limit(limit)
                .offset(offset)
            )
            return list(result.scalars().all())

    async def list_events(
        self, run_id: str, *, limit: int = 1000, offset: int = 0
    ) -> list[GovernanceEventRow]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(GovernanceEventRow)
                .where(GovernanceEventRow.run_id == run_id)
                .order_by(GovernanceEventRow.created_at.asc())
                .limit(limit)
                .offset(offset)
            )
            return list(result.scalars().all())

    async def summarize_run(self, run_id: str) -> dict[str, int]:
        """Decision counts for a run, used by the evidence record."""

        async with self._session_factory() as session:
            result = await session.execute(
                select(GovernanceEventRow.decision, func.count())
                .where(GovernanceEventRow.run_id == run_id)
                .group_by(GovernanceEventRow.decision)
            )
            counts = dict(result.all())

        return {
            "total": sum(counts.values()),
            "allowed": counts.get("ALLOW", 0),
            "denied": counts.get("DENY", 0),
            "approval_required": counts.get("APPROVAL_REQUIRED", 0),
        }

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _to_row(event: GovernanceEvent) -> GovernanceEventRow:
        return GovernanceEventRow(
            event_id=event.event_id,
            run_id=event.run_id,
            agent_id=event.agent_id,
            event_type=event.event_type,
            tool_name=event.tool_name,
            action=event.action,
            resource=event.resource,
            decision=event.decision,
            reason=event.reason,
            policy_version=event.policy_version,
            arguments_sanitized=event.arguments_sanitized,
            arguments_hash=event.arguments_hash,
            created_at=event.timestamp,
        )
