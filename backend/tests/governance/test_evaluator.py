"""Tests for the deterministic evaluator (spec sections 8 & 27)."""

from __future__ import annotations

import pytest

from app.governance.evaluator import GovernanceEvaluator
from app.governance.models import (
    ActionRequest,
    GovernanceContext,
    GovernanceScope,
)
from app.governance.policy import GovernancePolicy


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
        denied_tools={"delete_customer", "refund_payment"},
        approval_required_tools={"send_customer_email"},
        allowed_actions={"read", "update"},
        allowed_resources={"customer_profile"},
    )
    params.update(overrides)
    return GovernancePolicy(**params)


@pytest.fixture()
def evaluator() -> GovernanceEvaluator:
    return GovernanceEvaluator()


def test_allowed_update_action(evaluator):
    decision = evaluator.evaluate(
        make_context(),
        ActionRequest(
            tool_name="update_customer",
            action="update",
            resource="customer_profile",
        ),
        make_policy(),
    )
    assert decision.decision == "ALLOW"


def test_allowed_read_without_resource(evaluator):
    decision = evaluator.evaluate(
        make_context(),
        ActionRequest(tool_name="get_customer", action="read"),
        make_policy(),
    )
    assert decision.decision == "ALLOW"


def test_unknown_tool_is_denied(evaluator):
    decision = evaluator.evaluate(
        make_context(),
        ActionRequest(tool_name="transfer_funds", action="read"),
        make_policy(),
    )
    assert decision.decision == "DENY"
    assert "transfer_funds" in decision.reason


def test_explicitly_denied_tool_is_denied(evaluator):
    decision = evaluator.evaluate(
        make_context(),
        ActionRequest(tool_name="refund_payment", action="update"),
        make_policy(),
    )
    assert decision.decision == "DENY"
    assert "refund_payment" in decision.reason


def test_explicit_deny_beats_allowlist(evaluator):
    policy = make_policy(allowed_tools={"get_customer", "refund_payment"})
    decision = evaluator.evaluate(
        make_context(scope=GovernanceScope(allowed_tools={"refund_payment"})),
        ActionRequest(tool_name="refund_payment", action="update"),
        policy,
    )
    assert decision.decision == "DENY"


def test_approval_required_tool(evaluator):
    context = make_context(
        scope=GovernanceScope(allowed_tools={"send_customer_email"})
    )
    decision = evaluator.evaluate(
        context,
        ActionRequest(tool_name="send_customer_email", action="update"),
        make_policy(),
    )
    assert decision.decision == "APPROVAL_REQUIRED"
    assert "send_customer_email" in decision.reason


def test_explicit_deny_beats_approval(evaluator):
    policy = make_policy(
        denied_tools={"refund_payment"}, approval_required_tools={"refund_payment"}
    )
    decision = evaluator.evaluate(
        make_context(),
        ActionRequest(tool_name="refund_payment", action="update"),
        policy,
    )
    assert decision.decision == "DENY"


def test_disallowed_action_is_denied(evaluator):
    decision = evaluator.evaluate(
        make_context(),
        ActionRequest(tool_name="update_customer", action="delete"),
        make_policy(),
    )
    assert decision.decision == "DENY"
    assert "delete" in decision.reason


def test_disallowed_resource_is_denied(evaluator):
    decision = evaluator.evaluate(
        make_context(),
        ActionRequest(
            tool_name="update_customer", action="update", resource="billing_account"
        ),
        make_policy(),
    )
    assert decision.decision == "DENY"
    assert "billing_account" in decision.reason


def test_empty_allowlist_denies_everything(evaluator):
    decision = evaluator.evaluate(
        make_context(scope=GovernanceScope()),
        ActionRequest(tool_name="get_customer", action="read"),
        make_policy(allowed_tools=set()),
    )
    assert decision.decision == "DENY"


def test_scope_restriction_denies_even_if_policy_allows(evaluator):
    decision = evaluator.evaluate(
        make_context(scope=GovernanceScope(allowed_tools={"get_customer"})),
        ActionRequest(tool_name="update_customer", action="update"),
        make_policy(),
    )
    assert decision.decision == "DENY"


def test_policy_restriction_denies_even_if_scope_allows(evaluator):
    decision = evaluator.evaluate(
        make_context(
            scope=GovernanceScope(allowed_tools={"update_customer"}),
        ),
        ActionRequest(tool_name="update_customer", action="update"),
        make_policy(allowed_tools={"get_customer"}),
    )
    assert decision.decision == "DENY"


def test_decision_records_policy_version(evaluator):
    decision = evaluator.evaluate(
        make_context(),
        ActionRequest(tool_name="get_customer", action="read"),
        make_policy(version="customer-support-v9"),
    )
    assert decision.policy_version == "customer-support-v9"


def test_decisions_are_deterministic(evaluator):
    context, action, policy = (
        make_context(),
        ActionRequest(tool_name="update_customer", action="update"),
        make_policy(),
    )
    first = evaluator.evaluate(context, action, policy)
    second = evaluator.evaluate(context, action, policy)
    assert first == second
