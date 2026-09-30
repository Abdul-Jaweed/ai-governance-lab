"""Governance recorder interface and in-memory implementations.

The recorder is the only I/O boundary in the governance core; it is async
so the PostgreSQL implementation can use SQLAlchemy's async engine.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.governance.models import GovernanceEvent


@runtime_checkable
class GovernanceRecorder(Protocol):
    """Persists a single governance event."""

    async def arecord(self, event: GovernanceEvent) -> None: ...


class NullRecorder:
    """A recorder that discards events (used when persistence is disabled)."""

    async def arecord(self, event: GovernanceEvent) -> None:
        return None


class InMemoryRecorder:
    """Collects events in memory; intended for unit tests."""

    def __init__(self) -> None:
        self.events: list[GovernanceEvent] = []

    async def arecord(self, event: GovernanceEvent) -> None:
        self.events.append(event)
