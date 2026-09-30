"""Tests for the governance read API (spec section 18)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.governance import router
from app.db.repositories import PostgresRecorder
from app.governance.governor import RuntimeGovernor
from app.governance.models import ActionRequest, GovernanceContext, GovernanceScope
from app.governance.policy import GovernancePolicy
from app.main import create_app


@pytest_asyncio.fixture
async def env(pg_factory, clean_db):
    """An app wired to a real recorder, plus an HTTP client for it."""
    recorder = PostgresRecorder(pg_factory)
    app = create_app(recorder=recorder)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield SimpleNamespace(http=client, recorder=recorder, app=app)
    await recorder.aclose()


async def seed(recorder: PostgresRecorder, run_id: str) -> None:
    await recorder.add_run(
        run_id=run_id,
        agent_id="customer-support-agent",
        intent="Update customer contact information",
        scope={
            "allowed_tools": ["get_customer", "update_customer"],
            "allowed_actions": ["read", "update"],
            "allowed_resources": ["customer_profile"],
        },
        policy_version="customer-support-v1",
    )
    context = GovernanceContext(
        run_id=run_id,
        agent_id="customer-support-agent",
        intent="Update customer contact information",
        scope=GovernanceScope(
            allowed_tools={"get_customer", "update_customer"},
            allowed_actions={"read", "update"},
            allowed_resources={"customer_profile"},
        ),
        policy_version="customer-support-v1",
    )
    policy = GovernancePolicy(
        version="customer-support-v1",
        allowed_tools={"get_customer", "update_customer"},
        allowed_actions={"read", "update"},
        allowed_resources={"customer_profile"},
        denied_tools={"refund_payment"},
    )
    governor = RuntimeGovernor(recorder=recorder)
    for action in (
        ActionRequest(tool_name="get_customer", action="read", resource="customer_profile"),
        ActionRequest(tool_name="refund_payment", action="update"),
    ):
        await governor.authorize(context, action, policy)
    await recorder.finalize_run(
        run_id=run_id, status="completed", ended_at=datetime.now(timezone.utc)
    )


async def test_health_endpoint(env):
    response = await env.http.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_list_runs_returns_persisted_runs(env):
    run_id = str(uuid4())
    await seed(env.recorder, run_id)

    response = await env.http.get("/api/governance/runs")
    assert response.status_code == 200

    body = response.json()
    assert body["total"] == 1
    run = body["items"][0]
    assert run["run_id"] == run_id
    assert run["agent_id"] == "customer-support-agent"
    assert run["status"] == "completed"
    assert run["summary"]["allowed"] == 1
    assert run["summary"]["denied"] == 1


async def test_get_run_by_id(env):
    run_id = str(uuid4())
    await seed(env.recorder, run_id)

    response = await env.http.get(f"/api/governance/runs/{run_id}")
    assert response.status_code == 200

    body = response.json()
    assert body["run_id"] == run_id
    assert body["intent"] == "Update customer contact information"
    assert body["scope"]["allowed_tools"] == ["get_customer", "update_customer"]
    assert body["policy_version"] == "customer-support-v1"


async def test_get_unknown_run_returns_404(env):
    response = await env.http.get(f"/api/governance/runs/{uuid4()}")
    assert response.status_code == 404


async def test_list_events_for_a_run(env):
    run_id = str(uuid4())
    await seed(env.recorder, run_id)

    response = await env.http.get(f"/api/governance/runs/{run_id}/events")
    assert response.status_code == 200

    events = response.json()
    assert [e["tool_name"] for e in events] == ["get_customer", "refund_payment"]
    assert events[0]["decision"] == "ALLOW"
    assert events[1]["decision"] == "DENY"
    assert "refund_payment" in events[1]["reason"]


async def test_events_for_unknown_run_is_empty(env):
    response = await env.http.get(f"/api/governance/runs/{uuid4()}/events")
    assert response.status_code == 200
    assert response.json() == []


async def test_runs_endpoint_paginates(env):
    for _ in range(3):
        await seed(env.recorder, str(uuid4()))

    response = await env.http.get("/api/governance/runs", params={"limit": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert body["limit"] == 2
    assert body["offset"] == 0


async def test_evidence_endpoint_returns_the_core_artifact(env):
    run_id = str(uuid4())
    await seed(env.recorder, run_id)

    response = await env.http.get(f"/api/governance/runs/{run_id}/evidence")
    assert response.status_code == 200

    body = response.json()
    assert body["schema_version"] == "governance-evidence-v1"
    assert body["run_id"] == run_id
    assert [e["tool"] for e in body["events"]] == ["get_customer", "refund_payment"]
    assert body["summary"] == {
        "total_actions": 2,
        "allowed": 1,
        "denied": 1,
        "approval_required": 0,
    }


async def test_evidence_for_unknown_run_returns_404(env):
    response = await env.http.get(f"/api/governance/runs/{uuid4()}/evidence")
    assert response.status_code == 404


def test_api_router_is_mounted_under_governance_prefix():
    assert router.prefix == "/api/governance"
