"""Tests for governance domain models (spec section 6 & 12)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.governance.models import (
    ActionRequest,
    GovernanceContext,
    GovernanceDecision,
    GovernanceEvent,
    GovernanceScope,
)


def test_scope_defaults_to_empty_sets():
    scope = GovernanceScope()
    assert scope.allowed_tools == set()
    assert scope.allowed_resources == set()
    assert scope.allowed_actions == set()


def test_scope_coerces_lists_to_sets():
    scope = GovernanceScope(allowed_tools=["a", "b"], allowed_actions=["read"])
    assert scope.allowed_tools == {"a", "b"}
    assert scope.allowed_actions == {"read"}


def test_context_defaults_policy_version_to_v1():
    ctx = GovernanceContext(
        run_id="run_1",
        agent_id="agent_1",
        intent="do a thing",
        scope=GovernanceScope(),
    )
    assert ctx.policy_version == "v1"


def test_context_requires_identity_fields():
    with pytest.raises(ValidationError):
        GovernanceContext(agent_id="agent_1", intent="x", scope=GovernanceScope())


def test_action_request_defaults():
    action = ActionRequest(tool_name="get_customer", action="read")
    assert action.resource is None
    assert action.arguments == {}


def test_decision_accepts_only_known_outcomes():
    for outcome in ("ALLOW", "DENY", "APPROVAL_REQUIRED"):
        decision = GovernanceDecision(
            decision=outcome, reason="because", policy_version="v1"
        )
        assert decision.decision == outcome


def test_decision_rejects_unknown_outcome():
    with pytest.raises(ValidationError):
        GovernanceDecision(decision="MAYBE", reason="because", policy_version="v1")


def test_event_defaults():
    event = GovernanceEvent(
        event_id="evt_1",
        run_id="run_1",
        agent_id="agent_1",
        tool_name="update_customer",
        action="update",
        decision="ALLOW",
        reason="ok",
        policy_version="v1",
        timestamp=datetime.now(timezone.utc),
    )
    assert event.event_type == "tool_action"
    assert event.resource is None
    assert event.arguments_sanitized is None
    assert event.arguments_hash is None
