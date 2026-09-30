"""Tests for the governance policy model (spec section 7)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.governance.policy import GovernancePolicy


def test_policy_defaults_to_empty_sets():
    policy = GovernancePolicy(version="v1")
    assert policy.allowed_tools == set()
    assert policy.denied_tools == set()
    assert policy.approval_required_tools == set()
    assert policy.allowed_actions == set()
    assert policy.allowed_resources == set()


def test_policy_requires_version():
    with pytest.raises(ValidationError):
        GovernancePolicy()


def test_policy_coerces_lists_to_sets():
    policy = GovernancePolicy(version="v1", allowed_tools=["a", "a", "b"])
    assert policy.allowed_tools == {"a", "b"}
