"""SQLAlchemy ORM models mirroring the spec's PostgreSQL schema (section 14)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class GovernanceRunRow(Base):
    """One governed agent run and its declared authority."""

    __tablename__ = "governance_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    intent: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(255), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="running", server_default="running"
    )


class GovernanceEventRow(Base):
    """One immutable governance event, one per attempted action."""

    __tablename__ = "governance_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("governance_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    )

    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="tool_action", server_default="tool_action"
    )

    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    resource: Mapped[str | None] = mapped_column(String(255), nullable=True)

    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    policy_version: Mapped[str] = mapped_column(String(255), nullable=False)

    arguments_sanitized: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    arguments_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("idx_governance_events_run_id", "run_id"),
        Index("idx_governance_events_decision", "decision"),
        Index("idx_governance_events_created_at", "created_at"),
    )
