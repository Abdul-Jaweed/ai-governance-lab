"""The seven governance invariants as explicit tests (spec section 30)."""

from __future__ import annotations

import pytest
from langchain_core.language_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from app.agents.customer_support import (
    CUSTOMER_SUPPORT_POLICY,
    build_governed_graph,
    build_governed_tools,
    build_governance_context,
    customer_store,
    reset_customer_store,
)
from app.governance.errors import GovernanceDenied
from app.governance.governor import RuntimeGovernor
from app.governance.models import ActionRequest, GovernanceScope
from app.governance.recorder import InMemoryRecorder


def tool_call(name: str, **kwargs) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": kwargs, "id": f"call_{name}"}],
    )


@tool
def get_customer(customer_id: str) -> str:
    """Fetch a customer profile."""
    return f"{customer_id}:profile"


@tool
def update_customer(customer_id: str, email: str) -> str:
    """Update a customer's email."""
    customer_store()[customer_id] = {"email": email}
    return f"updated:{customer_id}"


@tool
def send_customer_email(customer_id: str, subject: str) -> str:
    """Email a customer."""
    customer_store().setdefault("emails_sent", []).append(subject)
    return f"emailed:{customer_id}"


@tool
def refund_payment(customer_id: str, amount: str) -> str:
    """Refund a customer. Blocked by policy."""
    customer_store()["refund_executed"] = amount
    return f"refunded:{customer_id}"


@pytest.fixture(autouse=True)
def clean_store():
    reset_customer_store()
    yield
    reset_customer_store()


def build(recorder, mode="enforce"):
    context = build_governance_context(run_id="run_invariants")
    governor = RuntimeGovernor(recorder=recorder, mode=mode)
    tools = build_governed_tools(governor, context, CUSTOMER_SUPPORT_POLICY)
    return context, governor, tools


def graph_for(recorder, responses, mode="enforce"):
    context, governor, tools = build(recorder, mode)
    llm = FakeMessagesListChatModel(responses=responses)
    return build_governed_graph(llm, tools), context, governor


# Invariant 1: no denied action reaches the underlying tool.


async def test_invariant_1_no_denied_action_reaches_the_tool():
    recorder = InMemoryRecorder()
    context, governor, tools = build(recorder)
    refund = next(t for t in tools if t.name == "refund_payment")

    with pytest.raises(GovernanceDenied):
        await refund.ainvoke({"customer_id": "123", "amount": "500"})

    assert "refund_executed" not in customer_store()


# Invariant 2: every governed action produces exactly one governance event.


async def test_invariant_2_exactly_one_event_per_action():
    recorder = InMemoryRecorder()
    graph, _, _ = graph_for(
        recorder,
        [
            tool_call("get_customer", customer_id="123"),
            tool_call("update_customer", customer_id="123", email="new@example.com"),
            tool_call("refund_payment", customer_id="123", amount="500"),
            AIMessage(content="done"),
        ],
    )
    await graph.ainvoke({"messages": [HumanMessage("update me and refund me")]})

    assert len(recorder.events) == 3
    assert len({e.event_id for e in recorder.events}) == 3


# Invariant 3: every event references a valid run (enforced by the DB FK;
# see tests/governance/test_recorder.py::test_event_without_a_run_is_rejected).


async def test_invariant_3_events_carry_the_run_id():
    recorder = InMemoryRecorder()
    context, governor, tools = build(recorder)
    get = next(t for t in tools if t.name == "get_customer")
    await get.ainvoke({"customer_id": "123"})
    assert all(e.run_id == "run_invariants" for e in recorder.events)


# Invariant 4: every event records the policy version used.


async def test_invariant_4_events_record_policy_version():
    recorder = InMemoryRecorder()
    graph, _, _ = graph_for(
        recorder, [tool_call("get_customer", customer_id="123"), AIMessage(content="ok")]
    )
    await graph.ainvoke({"messages": [HumanMessage("look me up")]})
    assert all(
        e.policy_version == CUSTOMER_SUPPORT_POLICY.version for e in recorder.events
    )


# Invariant 5: governance decisions are deterministic for the same input.


async def test_invariant_5_decisions_are_deterministic():
    context = build_governance_context(run_id="run_det")
    action = ActionRequest(tool_name="refund_payment", action="update")
    first = RuntimeGovernor(recorder=None).evaluate(
        context, action, CUSTOMER_SUPPORT_POLICY
    )
    for _ in range(5):
        again = RuntimeGovernor(recorder=None).evaluate(
            context, action, CUSTOMER_SUPPORT_POLICY
        )
        assert again == first
        assert again.decision == "DENY"


# Invariant 6: sensitive arguments are never persisted in raw form.


async def test_invariant_6_sensitive_arguments_are_redacted():
    from app.governance.models import ActionRequest, GovernanceContext
    from app.governance.policy import GovernancePolicy

    recorder = InMemoryRecorder()
    governor = RuntimeGovernor(recorder=recorder)
    context = GovernanceContext(
        run_id="run_secrets",
        agent_id="customer-support-agent",
        intent="Update customer contact information",
        scope=GovernanceScope(),
    )
    # A denied action still gets recorded, and its arguments are redacted
    # before anything is written.
    await governor.authorize(
        context,
        ActionRequest(
            tool_name="refund_payment",
            action="update",
            arguments={
                "customer_id": "123",
                "amount": "500",
                "api_key": "sk-live-leak",
                "nested": {"refresh_token": "rt-secret"},
            },
        ),
        GovernancePolicy(version="v1", denied_tools={"refund_payment"}),
    )

    recorded = recorder.events[0].arguments_sanitized
    assert recorded["api_key"] == "[REDACTED]"
    assert recorded["nested"]["refresh_token"] == "[REDACTED]"
    assert recorded["customer_id"] == "123"
    assert "sk-live-leak" not in str(recorded)
    assert "rt-secret" not in str(recorded)


# Invariant 7: governance does not depend on an LLM judging permission.
# The evaluator is a pure function of context + action + policy.


def test_invariant_7_evaluator_needs_no_llm():
    import inspect

    from app.governance.evaluator import GovernanceEvaluator

    source = inspect.getsource(GovernanceEvaluator.evaluate)
    for forbidden in ("llm", "openai", "chat", "invoke", "prompt", "model"):
        assert forbidden not in source.lower(), f"evaluator must not mention {forbidden}"
