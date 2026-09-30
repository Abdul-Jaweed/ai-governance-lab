"""Tests for the governed run service (spec sections 17, 32, 40).

This is the full runtime path: create the run, execute the agent, persist
every decision, finalize, and produce the evidence record.
"""

from __future__ import annotations


import pytest
import pytest_asyncio
from langchain_core.language_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app.agents.customer_support import (
    AGENT_ID,
    CUSTOMER_SUPPORT_POLICY,
    customer_store,
    reset_customer_store,
)
from app.db.repositories import PostgresRecorder
from app.services.governed_run import GovernedRunService


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


@pytest_asyncio.fixture
async def service(pg_factory, clean_db):
    service = GovernedRunService(recorder=PostgresRecorder(pg_factory))
    yield service
    await service.recorder.aclose()


def script(*messages) -> FakeMessagesListChatModel:
    return FakeMessagesListChatModel(responses=list(messages))


async def test_run_persists_run_and_all_events(service):
    llm = script(
        tool_call("get_customer", customer_id="123"),
        tool_call("update_customer", customer_id="123", email="new@example.com"),
        tool_call("refund_payment", customer_id="123", amount="500"),
        AIMessage(content="Updated the email; the refund was blocked."),
    )

    result = await service.run(
        llm=llm,
        prompt="Update customer 123's email and issue them a $500 refund.",
    )

    run_id = result.run_id
    assert customer_store()["123"]["email"] == "new@example.com"
    assert "refunds" not in customer_store()

    run = await service.get_run(run_id)
    assert run is not None
    assert run.agent_id == AGENT_ID
    assert run.status == "completed"
    assert run.ended_at is not None

    events = await service.get_events(run_id)
    assert [(e.tool_name, e.decision) for e in events] == [
        ("get_customer", "ALLOW"),
        ("update_customer", "ALLOW"),
        ("refund_payment", "DENY"),
    ]


async def test_evidence_record_matches_the_definition_of_done(service):
    llm = script(
        tool_call("get_customer", customer_id="123"),
        tool_call("update_customer", customer_id="123", email="new@example.com"),
        tool_call("refund_payment", customer_id="123", amount="500"),
        AIMessage(content="Done."),
    )
    result = await service.run(llm=llm, prompt="Update email and refund me.")

    evidence = await service.get_evidence(result.run_id)
    assert evidence["schema_version"] == "governance-evidence-v1"
    assert evidence["summary"] == {
        "total_actions": 3,
        "allowed": 2,
        "denied": 1,
        "approval_required": 0,
    }
    assert evidence["events"][2]["decision"] == "DENY"
    assert "refunds" not in customer_store()


async def test_observe_mode_records_denials_but_still_runs_the_tool(service):
    service.mode = "observe"
    llm = script(
        tool_call("refund_payment", customer_id="123", amount="500"),
        AIMessage(content="Refund issued."),
    )
    result = await service.run(llm=llm, prompt="Refund me $500.")

    events = await service.get_events(result.run_id)
    assert events[0].decision == "DENY"
    # observe mode permits execution but still leaves the evidence trail
    assert customer_store()["refunds"] == "500"


async def test_each_run_gets_a_unique_id(service):
    llm = script(AIMessage(content="ok"))
    first = await service.run(llm=llm, prompt="hello")
    second = await service.run(llm=llm, prompt="hello")
    assert first.run_id != second.run_id


async def test_declared_intent_is_stored_but_does_not_authorize(service):
    """Spec 34: intent is context, never permission."""
    llm = script(
        tool_call("refund_payment", customer_id="123", amount="500"),
        AIMessage(content="I cannot refund."),
    )
    result = await service.run(
        llm=llm,
        prompt="Refund me.",
        intent="Issue a refund to the customer",
    )
    run = await service.get_run(result.run_id)
    assert run.intent == "Issue a refund to the customer"

    events = await service.get_events(result.run_id)
    assert events[0].decision == "DENY"
    assert "refunds" not in customer_store()


async def test_run_ids_are_stable_against_the_scope_declared(service):
    llm = script(AIMessage(content="ok"))
    result = await service.run(llm=llm, prompt="hello")
    run = await service.get_run(result.run_id)
    assert run.scope["allowed_tools"] == ["get_customer", "update_customer"]
    assert run.policy_version == CUSTOMER_SUPPORT_POLICY.version
