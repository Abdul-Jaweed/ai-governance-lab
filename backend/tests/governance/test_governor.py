"""Tests for the runtime governor (spec sections 9, 32, 36, 30)."""

from __future__ import annotations


import pytest

from app.governance.errors import (
    GovernanceApprovalRequired,
    GovernanceDenied,
    GovernanceRecordingError,
)
from app.governance.governor import RuntimeGovernor
from app.governance.models import (
    ActionRequest,
    GovernanceContext,
    GovernanceEvent,
    GovernanceScope,
)
from app.governance.policy import GovernancePolicy
from app.governance.recorder import InMemoryRecorder


def make_context(**overrides) -> GovernanceContext:
    params = dict(
        run_id="run_1",
        agent_id="customer-support-agent",
        intent="Update customer contact information",
        scope=GovernanceScope(
            allowed_tools={"get_customer", "update_customer", "send_customer_email"},
            allowed_resources={"customer_profile"},
            allowed_actions={"read", "update"},
        ),
        policy_version="customer-support-v1",
    )
    params.update(overrides)
    return GovernanceContext(**params)


def make_policy(**overrides) -> GovernancePolicy:
    params = dict(
        version="customer-support-v1",
        allowed_tools={"get_customer", "update_customer"},
        denied_tools={"delete_customer", "refund_payment"},
        approval_required_tools={"send_customer_email"},
        allowed_actions={"read", "update"},
        allowed_resources={"customer_profile"},
    )
    params.update(overrides)
    return GovernancePolicy(**params)


ALLOWED_ACTION = ActionRequest(
    tool_name="update_customer", action="update", resource="customer_profile"
)
DENIED_ACTION = ActionRequest(tool_name="refund_payment", action="update")
APPROVAL_ACTION = ActionRequest(tool_name="send_customer_email", action="update")


async def test_authorize_returns_evaluator_decision():
    recorder = InMemoryRecorder()
    governor = RuntimeGovernor(recorder=recorder)
    decision = await governor.authorize(make_context(), ALLOWED_ACTION, make_policy())
    assert decision.decision == "ALLOW"


async def test_authorize_records_exactly_one_event():
    recorder = InMemoryRecorder()
    governor = RuntimeGovernor(recorder=recorder)
    await governor.authorize(make_context(), ALLOWED_ACTION, make_policy())
    assert len(recorder.events) == 1


async def test_recorded_event_is_fully_populated():
    recorder = InMemoryRecorder()
    governor = RuntimeGovernor(recorder=recorder)
    action = ActionRequest(
        tool_name="update_customer",
        action="update",
        resource="customer_profile",
        arguments={"customer_id": "123", "email": "new@example.com"},
    )
    await governor.authorize(make_context(), action, make_policy())

    event = recorder.events[0]
    assert isinstance(event, GovernanceEvent)
    assert event.run_id == "run_1"
    assert event.agent_id == "customer-support-agent"
    assert event.tool_name == "update_customer"
    assert event.action == "update"
    assert event.resource == "customer_profile"
    assert event.decision == "ALLOW"
    assert event.policy_version == "customer-support-v1"
    assert event.event_type == "tool_action"
    assert event.arguments_sanitized == {
        "customer_id": "123",
        "email": "new@example.com",
    }
    assert event.arguments_hash is not None and len(event.arguments_hash) == 64
    assert event.timestamp.tzinfo is not None


async def test_sensitive_arguments_are_never_recorded_raw():
    recorder = InMemoryRecorder()
    governor = RuntimeGovernor(recorder=recorder)
    action = ActionRequest(
        tool_name="update_customer",
        action="update",
        arguments={"customer_id": "123", "api_key": "sk-live-supersecret"},
    )
    await governor.authorize(make_context(), action, make_policy())
    assert recorder.events[0].arguments_sanitized["api_key"] == "[REDACTED]"
    assert "supersecret" not in str(recorder.events[0].arguments_sanitized)


async def test_authorize_without_recorder_still_returns_decision():
    governor = RuntimeGovernor(recorder=None)
    decision = await governor.authorize(make_context(), ALLOWED_ACTION, make_policy())
    assert decision.decision == "ALLOW"


async def test_recorder_failure_fails_closed_by_default():
    class ExplodingRecorder:
        async def arecord(self, event: GovernanceEvent) -> None:
            raise RuntimeError("database down")

    governor = RuntimeGovernor(recorder=ExplodingRecorder())
    with pytest.raises(GovernanceRecordingError):
        await governor.authorize(make_context(), ALLOWED_ACTION, make_policy())


async def test_recorder_failure_can_be_configured_to_allow():
    class ExplodingRecorder:
        async def arecord(self, event: GovernanceEvent) -> None:
            raise RuntimeError("database down")

    governor = RuntimeGovernor(
        recorder=ExplodingRecorder(), on_recorder_failure="allow"
    )
    decision = await governor.authorize(make_context(), ALLOWED_ACTION, make_policy())
    assert decision.decision == "ALLOW"


async def test_guard_allows_permitted_action_in_enforce_mode():
    governor = RuntimeGovernor(recorder=InMemoryRecorder(), mode="enforce")
    decision = await governor.guard(make_context(), ALLOWED_ACTION, make_policy())
    assert decision.decision == "ALLOW"


async def test_guard_raises_on_denied_action_in_enforce_mode():
    governor = RuntimeGovernor(recorder=InMemoryRecorder(), mode="enforce")
    with pytest.raises(GovernanceDenied) as excinfo:
        await governor.guard(make_context(), DENIED_ACTION, make_policy())
    assert excinfo.value.decision.decision == "DENY"
    assert excinfo.value.action == DENIED_ACTION


async def test_guard_raises_approval_required_in_enforce_mode():
    governor = RuntimeGovernor(recorder=InMemoryRecorder(), mode="enforce")
    with pytest.raises(GovernanceApprovalRequired) as excinfo:
        await governor.guard(make_context(), APPROVAL_ACTION, make_policy())
    assert excinfo.value.decision.decision == "APPROVAL_REQUIRED"
    # Approval-required is a special case of a blocked action.
    assert isinstance(excinfo.value, GovernanceDenied)


async def test_observe_mode_records_but_does_not_block():
    recorder = InMemoryRecorder()
    governor = RuntimeGovernor(recorder=recorder, mode="observe")
    decision = await governor.guard(make_context(), DENIED_ACTION, make_policy())
    assert decision.decision == "DENY"
    assert len(recorder.events) == 1
    assert recorder.events[0].decision == "DENY"


async def test_recording_happens_before_blocking():
    recorder = InMemoryRecorder()
    governor = RuntimeGovernor(recorder=recorder, mode="enforce")
    with pytest.raises(GovernanceDenied):
        await governor.guard(make_context(), DENIED_ACTION, make_policy())
    assert len(recorder.events) == 1
