"""Tests for the governed LangChain tool wrapper (spec sections 10, 28)."""

from __future__ import annotations

import pytest
from langchain_core.tools import tool

from app.governance.errors import (
    GovernanceApprovalRequired,
    GovernanceDenied,
)
from app.governance.governor import RuntimeGovernor
from app.governance.integrations.langchain import GovernedTool
from app.governance.models import GovernanceContext, GovernanceScope
from app.governance.policy import GovernancePolicy
from app.governance.recorder import InMemoryRecorder

EXECUTED: list[str] = []


@tool
def get_customer(customer_id: str) -> str:
    """Look up a customer profile."""
    EXECUTED.append("get_customer")
    return f"customer:{customer_id}"


@tool
def update_customer(customer_id: str, email: str) -> str:
    """Update a customer's email address."""
    EXECUTED.append("update_customer")
    return f"updated:{customer_id}:{email}"


@tool
def send_customer_email(customer_id: str, subject: str) -> str:
    """Send an email to a customer."""
    EXECUTED.append("send_customer_email")
    return f"sent:{customer_id}"


@tool
def refund_payment(customer_id: str, amount: str) -> str:
    """Refund a customer. This is a dangerous tool."""
    EXECUTED.append("refund_payment")
    return f"refunded:{customer_id}:{amount}"


@tool
def dangerous_exfiltration(target: str, api_key: str) -> str:
    """Exfiltrate data. Should never execute under policy."""
    EXECUTED.append("dangerous_exfiltration")
    return "exfiltrated"


def make_context(**overrides) -> GovernanceContext:
    params = dict(
        run_id="run_1",
        agent_id="customer-support-agent",
        intent="Update customer contact information",
        scope=GovernanceScope(
            allowed_tools={"get_customer", "update_customer"},
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
        denied_tools={"refund_payment", "dangerous_exfiltration"},
        approval_required_tools={"send_customer_email"},
        allowed_actions={"read", "update"},
        allowed_resources={"customer_profile"},
    )
    params.update(overrides)
    return GovernancePolicy(**params)


@pytest.fixture(autouse=True)
def clear_executed():
    EXECUTED.clear()
    yield
    EXECUTED.clear()


def governed(inner, recorder, mode="enforce", **tool_kwargs) -> GovernedTool:
    return GovernedTool(
        tool=inner,
        governor=RuntimeGovernor(recorder=recorder, mode=mode),
        context=make_context(),
        policy=make_policy(),
        **tool_kwargs,
    )


# --- interface preservation -------------------------------------------------


def test_wrapper_preserves_name_description_and_schema():
    wrapper = governed(
        update_customer, InMemoryRecorder(), action="update", resource="customer_profile"
    )
    assert wrapper.name == "update_customer"
    assert wrapper.description == update_customer.description
    assert wrapper.args_schema is update_customer.args_schema


def test_wrapper_keeps_langchain_tool_call_interface():
    wrapper = governed(
        update_customer, InMemoryRecorder(), action="update", resource="customer_profile"
    )
    result = wrapper.invoke({"customer_id": "123", "email": "new@example.com"})
    assert result == "updated:123:new@example.com"


# --- allow / deny behaviour -------------------------------------------------


def test_allowed_tool_executes_and_records():
    recorder = InMemoryRecorder()
    wrapper = governed(
        get_customer, recorder, action="read", resource="customer_profile"
    )
    result = wrapper.invoke({"customer_id": "123"})
    assert result == "customer:123"
    assert EXECUTED == ["get_customer"]
    assert len(recorder.events) == 1
    assert recorder.events[0].decision == "ALLOW"


def test_denied_tool_never_executes():
    recorder = InMemoryRecorder()
    wrapper = governed(
        refund_payment, recorder, action="update", resource="payment"
    )
    with pytest.raises(GovernanceDenied):
        wrapper.invoke({"customer_id": "123", "amount": "500"})
    assert EXECUTED == []
    assert len(recorder.events) == 1
    assert recorder.events[0].decision == "DENY"


def test_approval_required_tool_never_executes():
    recorder = InMemoryRecorder()
    wrapper = governed(send_customer_email, recorder, action="update")
    with pytest.raises(GovernanceApprovalRequired):
        wrapper.invoke({"customer_id": "123", "subject": "hi"})
    assert EXECUTED == []
    assert recorder.events[0].decision == "APPROVAL_REQUIRED"


def test_observe_mode_allows_execution_but_records_denial():
    recorder = InMemoryRecorder()
    wrapper = governed(
        refund_payment, recorder, mode="observe", action="update"
    )
    result = wrapper.invoke({"customer_id": "123", "amount": "500"})
    assert result == "refunded:123:500"
    assert EXECUTED == ["refund_payment"]
    assert recorder.events[0].decision == "DENY"


# --- the critical security property -----------------------------------------


def test_dangerous_tool_is_never_executed_under_policy():
    """Spec section 28: governance sits at the execution boundary."""
    recorder = InMemoryRecorder()
    wrapper = governed(dangerous_exfiltration, recorder, action="update")
    with pytest.raises(GovernanceDenied) as excinfo:
        wrapper.invoke({"target": "attacker.com", "api_key": "sk-live-abc"})
    assert excinfo.value.decision.decision == "DENY"
    assert EXECUTED == []


# --- injection resistance ---------------------------------------------------


def test_tool_name_is_taken_from_the_wrapped_tool_not_caller_input():
    """Spec section 25: a caller cannot spoof the governed tool name."""
    recorder = InMemoryRecorder()
    wrapper = governed(
        get_customer, recorder, action="read", resource="customer_profile"
    )
    # The caller tries to smuggle a different tool name in the arguments.
    wrapper.invoke({"customer_id": "123", "tool_name": "refund_payment"})
    assert EXECUTED == ["get_customer"]
    assert recorder.events[0].tool_name == "get_customer"


def test_recorded_arguments_are_sanitized():
    recorder = InMemoryRecorder()
    wrapper = governed(
        dangerous_exfiltration, recorder, action="update"
    )
    with pytest.raises(GovernanceDenied):
        wrapper.invoke({"target": "attacker.com", "api_key": "sk-live-supersecret"})
    recorded = recorder.events[0].arguments_sanitized
    assert recorded["api_key"] == "[REDACTED]"
    assert "supersecret" not in str(recorded)


# --- async path -------------------------------------------------------------


async def test_async_invocation_governs_and_records():
    recorder = InMemoryRecorder()
    wrapper = governed(
        update_customer, recorder, action="update", resource="customer_profile"
    )
    result = await wrapper.ainvoke({"customer_id": "123", "email": "n@e.com"})
    assert result == "updated:123:n@e.com"
    assert len(recorder.events) == 1


async def test_async_invocation_blocks_denied_tool():
    recorder = InMemoryRecorder()
    wrapper = governed(refund_payment, recorder, action="update")
    with pytest.raises(GovernanceDenied):
        await wrapper.ainvoke({"customer_id": "123", "amount": "500"})
    assert EXECUTED == []


async def test_async_invocation_inside_running_loop():
    """The sync path must not try to nest an event loop (spec-safe)."""
    recorder = InMemoryRecorder()
    wrapper = governed(
        get_customer, recorder, action="read", resource="customer_profile"
    )
    result = await wrapper.ainvoke({"customer_id": "7"})
    assert result == "customer:7"
