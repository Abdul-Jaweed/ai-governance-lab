"""Governance domain models (spec sections 6 & 12).

These models are the stable contract shared by the evaluator, governor,
recorder and API. They deliberately contain no behaviour beyond validation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Decision = Literal["ALLOW", "DENY", "APPROVAL_REQUIRED"]


class GovernanceScope(BaseModel):
    """The authority declared for a run."""

    allowed_tools: set[str] = Field(default_factory=set)
    allowed_resources: set[str] = Field(default_factory=set)
    allowed_actions: set[str] = Field(default_factory=set)


class GovernanceContext(BaseModel):
    """Declared identity + intent for a governed run.

    ``intent`` is context for audit only; it never grants permission
    (spec section 34).
    """

    run_id: str
    agent_id: str
    intent: str
    scope: GovernanceScope
    policy_version: str = "v1"


class ActionRequest(BaseModel):
    """A normalized, trusted description of a single governed action."""

    tool_name: str
    action: str
    resource: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class GovernanceDecision(BaseModel):
    """The deterministic outcome of evaluating an action."""

    decision: Decision
    reason: str
    policy_version: str


class GovernanceEvent(BaseModel):
    """One immutable governance event, one per attempted action."""

    event_id: str
    run_id: str
    agent_id: str

    event_type: str = "tool_action"

    tool_name: str
    action: str
    resource: str | None = None

    decision: Decision
    reason: str

    policy_version: str

    timestamp: datetime

    arguments_hash: str | None = None
    arguments_sanitized: dict[str, Any] | None = None
