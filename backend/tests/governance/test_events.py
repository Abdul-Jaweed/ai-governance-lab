"""Tests for the end-of-run evidence record (spec section 17)."""

from __future__ import annotations

from datetime import datetime, timezone

from app.governance.events import build_evidence_record
from app.governance.models import GovernanceContext, GovernanceEvent, GovernanceScope


def make_context() -> GovernanceContext:
    return GovernanceContext(
        run_id="run_123",
        agent_id="customer-support-agent",
        intent="Update customer contact information",
        scope=GovernanceScope(
            allowed_tools={"get_customer", "update_customer"},
            allowed_resources={"customer_profile"},
            allowed_actions={"read", "update"},
        ),
        policy_version="customer-support-v1",
    )


def event(tool: str, decision: str) -> GovernanceEvent:
    return GovernanceEvent(
        event_id=f"evt_{tool}",
        run_id="run_123",
        agent_id="customer-support-agent",
        tool_name=tool,
        action="update",
        decision=decision,
        reason=f"{decision} reason for {tool}",
        policy_version="customer-support-v1",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_evidence_record_has_the_documented_shape():
    record = build_evidence_record(
        make_context(),
        [event("get_customer", "ALLOW"), event("refund_payment", "DENY")],
    )
    assert record["schema_version"] == "governance-evidence-v1"
    assert record["run_id"] == "run_123"
    assert record["agent_id"] == "customer-support-agent"
    assert record["intent"] == "Update customer contact information"
    assert record["policy_version"] == "customer-support-v1"


def test_evidence_record_includes_scope():
    record = build_evidence_record(make_context(), [])
    assert record["scope"]["allowed_tools"] == ["get_customer", "update_customer"]
    assert record["scope"]["allowed_actions"] == ["read", "update"]


def test_evidence_record_lists_events_in_order():
    events = [
        event("get_customer", "ALLOW"),
        event("update_customer", "ALLOW"),
        event("refund_payment", "DENY"),
    ]
    record = build_evidence_record(make_context(), events)
    assert [e["tool"] for e in record["events"]] == [
        "get_customer",
        "update_customer",
        "refund_payment",
    ]
    assert record["events"][2]["decision"] == "DENY"


def test_evidence_record_summarizes_decisions():
    events = [
        event("get_customer", "ALLOW"),
        event("update_customer", "ALLOW"),
        event("refund_payment", "DENY"),
    ]
    record = build_evidence_record(make_context(), events)
    assert record["summary"] == {
        "total_actions": 3,
        "allowed": 2,
        "denied": 1,
        "approval_required": 0,
    }


def test_evidence_record_counts_approval_required():
    events = [event("send_customer_email", "APPROVAL_REQUIRED")]
    record = build_evidence_record(make_context(), events)
    assert record["summary"] == {
        "total_actions": 1,
        "allowed": 0,
        "denied": 0,
        "approval_required": 1,
    }


def test_evidence_record_never_exposes_raw_secrets():
    secret_event = GovernanceEvent(
        event_id="evt_1",
        run_id="run_123",
        agent_id="customer-support-agent",
        tool_name="update_customer",
        action="update",
        decision="ALLOW",
        reason="ok",
        policy_version="customer-support-v1",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        arguments_sanitized={"customer_id": "123", "api_key": "[REDACTED]"},
        arguments_hash="deadbeef",
    )
    record = build_evidence_record(make_context(), [secret_event])
    assert record["events"][0]["arguments_sanitized"]["api_key"] == "[REDACTED]"
    assert "arguments_hash" in record["events"][0]


def test_evidence_record_is_json_serialisable():
    import json

    record = build_evidence_record(
        make_context(), [event("get_customer", "ALLOW")]
    )
    assert json.loads(json.dumps(record)) == record
