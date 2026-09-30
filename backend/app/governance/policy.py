"""Governance policy model (spec section 7).

Policy is intentionally a flat set of allow/deny/approval lists. It is
deterministic and data-only so it can eventually be loaded from YAML
(policy-as-code) without changing the evaluator.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class GovernancePolicy(BaseModel):
    version: str

    allowed_tools: set[str] = Field(default_factory=set)
    denied_tools: set[str] = Field(default_factory=set)
    approval_required_tools: set[str] = Field(default_factory=set)

    allowed_actions: set[str] = Field(default_factory=set)
    allowed_resources: set[str] = Field(default_factory=set)
