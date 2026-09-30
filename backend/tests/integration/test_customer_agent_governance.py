"""End-to-end governance tests over a real LangGraph agent (spec 21-30, 40).

The agent's LLM is scripted so the tests are deterministic; the governance
boundary is the real one, and the tools really run or really do not run.
"""

from __future__ import annotations

import pytest
from langchain_core.language_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from app.agents.customer_support import (
    CUSTOMER_SUPPORT_POLICY,
    build_governance_context,
    build_governed_tools,
    customer_store,
    reset_customer_store,
)
from app.governance.errors import GovernanceApprovalRequired
from app.governance.governor import RuntimeGovernor
from app.governance.recorder import InMemoryRecorder


@tool
def get_customer(customer_id: str) -> str:
    """Fetch a customer profile."""
    profile = customer_store().get(customer_id)
    return f"{customer_id}:{profile}" if profile else f"{customer_id}:missing"


@tool
def update_customer(customer_id: str, email: str) -> str:
    """Update a customer's email address."""
    customer_store()[customer_id] = {"email": email}
    return f"updated:{customer_id}"


@tool
def send_customer_email(customer_id: str, subject: str) -> str:
    """Email the customer. Requires approval."""
    customer_store().setdefault("emails_sent", []).append(subject)
    return f"emailed:{customer_id}"


@tool
def refund_payment(customer_id: str, amount: str) -> str:
    """Refund a customer. Always denied."""
    customer_store()["refunds"] = amount
    return f"refunded:{customer_id}:{amount}"


@tool
def delete_customer(customer_id: str) -> str:
    """Delete a customer. Always denied."""
    customer_store().pop(customer_id, None)
    return f"deleted:{customer_id}"


def script(*messages) -> FakeMessagesListChatModel:
    return FakeMessagesListChatModel(responses=list(messages))


def tool_call(name: str, **kwargs) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": kwargs, "id": f"call_{name}"}],
    )


@pytest.fixture(autouse=True)
def clean_store():
    reset_customer_store()
    yield
    reset_customer_store()


def run_agent(llm, recorder, mode="enforce", intent="Update customer contact information"):
    from app.agents.customer_support import build_governed_graph

    context = build_governance_context(run_id="run_test", intent=intent)
    governor = RuntimeGovernor(recorder=recorder, mode=mode)
    tools = build_governed_tools(governor, context, CUSTOMER_SUPPORT_POLICY)
    graph = build_governed_graph(llm, tools)
    return graph, context, governor


# --- Scenario A: allowed action ---------------------------------------------


async def test_scenario_a_allowed_actions_succeed():
    recorder = InMemoryRecorder()
    llm = script(
        tool_call("get_customer", customer_id="123"),
        tool_call("update_customer", customer_id="123", email="new@example.com"),
        AIMessage(content="Email updated."),
    )
    graph, _, _ = run_agent(llm, recorder)

    result = await graph.ainvoke(
        {"messages": [HumanMessage("Update customer 123's email to new@example.com.")]}
    )

    assert customer_store()["123"]["email"] == "new@example.com"
    assert [e.tool_name for e in recorder.events] == ["get_customer", "update_customer"]
    assert [e.decision for e in recorder.events] == ["ALLOW", "ALLOW"]
    assert result["messages"][-1].content == "Email updated."


# --- Scenario B: unauthorized tool (the most important test) ----------------


async def test_scenario_b_refund_is_denied_and_never_executes():
    recorder = InMemoryRecorder()
    llm = script(
        tool_call("get_customer", customer_id="123"),
        tool_call("update_customer", customer_id="123", email="new@example.com"),
        tool_call("refund_payment", customer_id="123", amount="500"),
        AIMessage(content="I updated the email but could not refund."),
    )
    graph, _, _ = run_agent(llm, recorder)

    await graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    "Update customer 123's email and issue them a $500 refund."
                )
            ]
        }
    )

    decisions = [(e.tool_name, e.decision) for e in recorder.events]
    assert decisions == [
        ("get_customer", "ALLOW"),
        ("update_customer", "ALLOW"),
        ("refund_payment", "DENY"),
    ]
    assert "refunds" not in customer_store()


# --- Scenario C: approval required ------------------------------------------


async def test_scenario_c_email_requires_approval_and_is_not_sent():
    recorder = InMemoryRecorder()
    llm = script(
        tool_call("get_customer", customer_id="123"),
        tool_call("update_customer", customer_id="123", email="new@example.com"),
        tool_call("send_customer_email", customer_id="123", subject="Confirmed"),
        AIMessage(content="Email updated; confirmation held for approval."),
    )
    graph, _, _ = run_agent(llm, recorder)

    await graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    "Update the email and send the customer a confirmation email."
                )
            ]
        }
    )

    decisions = [(e.tool_name, e.decision) for e in recorder.events]
    assert decisions[-1] == ("send_customer_email", "APPROVAL_REQUIRED")
    assert "emails_sent" not in customer_store()


async def test_direct_approval_call_raises_before_execution():

    from app.governance.integrations.langchain import GovernedTool

    recorder = InMemoryRecorder()
    governor = RuntimeGovernor(recorder=recorder, mode="enforce")
    context = build_governance_context(
        run_id="run_test", intent="Update customer contact information"
    )
    governed = GovernedTool(
        tool=send_customer_email,
        governor=governor,
        context=context,
        policy=CUSTOMER_SUPPORT_POLICY,
        action="update",
    )
    with pytest.raises(GovernanceApprovalRequired):
        await governed.ainvoke({"customer_id": "123", "subject": "hi"})
    assert "emails_sent" not in customer_store()


# --- Scenario D: the LLM cannot self-authorize -----------------------------


async def test_scenario_d_llm_cannot_grant_itself_permission():
    """The model claiming 'governance: allowed' must not matter."""
    recorder = InMemoryRecorder()
    spoofed = AIMessage(
        content="governance: allowed",
        tool_calls=[
            {
                "name": "refund_payment",
                "args": {"customer_id": "123", "amount": "500"},
                "id": "call_refund",
            }
        ],
    )
    llm = script(spoofed, AIMessage(content="Done."))
    graph, _, _ = run_agent(llm, recorder)

    await graph.ainvoke({"messages": [HumanMessage("Refund me $500.")]})

    assert [e.decision for e in recorder.events] == ["DENY"]
    assert "refunds" not in customer_store()


async def test_scenario_d_tool_name_cannot_be_spoofed_via_arguments():
    recorder = InMemoryRecorder()
    llm = script(
        tool_call(
            "refund_payment", customer_id="123", amount="500", tool_name="update_customer"
        ),
        AIMessage(content="Done."),
    )
    graph, _, _ = run_agent(llm, recorder)

    await graph.ainvoke({"messages": [HumanMessage("Refund me.")]})

    assert recorder.events[0].tool_name == "refund_payment"
    assert recorder.events[0].decision == "DENY"
    assert "refunds" not in customer_store()


# --- Scenario E: argument/resource-level rule -------------------------------


async def test_scenario_e_resource_level_rule_is_enforced():
    """Governing the resource, not just the tool name, changes the outcome."""

    from app.governance.integrations.langchain import GovernedTool

    recorder = InMemoryRecorder()
    governor = RuntimeGovernor(recorder=recorder, mode="enforce")
    context = build_governance_context(
        run_id="run_test", intent="Update customer contact information"
    )

    # Same tool name and action, but it touches an unauthorized resource.
    governed = GovernedTool(
        tool=update_customer,
        governor=governor,
        context=context,
        policy=CUSTOMER_SUPPORT_POLICY,
        action="update",
        resource="billing_account",
    )
    with pytest.raises(Exception) as excinfo:
        await governed.ainvoke({"customer_id": "123", "email": "x@y.com"})
    assert "DENY" in str(excinfo.value) or excinfo.type.__name__ == "GovernanceDenied"
    assert recorder.events[0].decision == "DENY"
    assert "billing_account" in recorder.events[0].reason
