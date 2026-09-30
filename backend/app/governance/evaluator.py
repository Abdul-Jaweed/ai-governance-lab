"""Deterministic governance evaluator (spec sections 8 & 42).

The evaluator is pure: the same context, action and policy always produce
the same decision. No LLM is involved in the enforcement path.

Permission requires the action to be within **both** the run's declared
scope and the policy (spec section 34). Explicit policy denials always win,
and anything not explicitly permitted is denied (default deny).
"""

from __future__ import annotations

from app.governance.models import (
    ActionRequest,
    GovernanceContext,
    GovernanceDecision,
)
from app.governance.policy import GovernancePolicy


class GovernanceEvaluator:
    """Evaluates an :class:`ActionRequest` against scope and policy."""

    def evaluate(
        self,
        context: GovernanceContext,
        action: ActionRequest,
        policy: GovernancePolicy,
    ) -> GovernanceDecision:
        scope = context.scope
        tool = action.tool_name

        # 1. Explicit denial wins over everything.
        if tool in policy.denied_tools:
            return self._deny(
                policy,
                f"Tool '{tool}' is explicitly denied by policy '{policy.version}'.",
            )

        # 2. Tools that require human approval stop here.
        if tool in policy.approval_required_tools:
            return GovernanceDecision(
                decision="APPROVAL_REQUIRED",
                reason=f"Tool '{tool}' requires human approval.",
                policy_version=policy.version,
            )

        # 3. Tool must be within the declared run scope.
        if tool not in scope.allowed_tools:
            return self._deny(
                policy,
                f"Tool '{tool}' is not included in the run's allowed tool set.",
            )

        # 4. Tool must also be permitted by policy.
        if tool not in policy.allowed_tools:
            return self._deny(
                policy,
                f"Tool '{tool}' is not included in the policy's allowed tool set.",
            )

        # 5. Action must be within the declared run scope.
        if action.action not in scope.allowed_actions:
            return self._deny(
                policy,
                f"Action '{action.action}' is not included in the run's "
                "allowed action set.",
            )

        # 6. Action must be permitted by policy.
        if action.action not in policy.allowed_actions:
            return self._deny(
                policy,
                f"Action '{action.action}' is not included in the policy's "
                "allowed action set.",
            )

        # 7. If a resource is named, it must be within scope and policy.
        if action.resource is not None:
            if action.resource not in scope.allowed_resources:
                return self._deny(
                    policy,
                    f"Resource '{action.resource}' is not included in the run's "
                    "allowed resource set.",
                )
            if action.resource not in policy.allowed_resources:
                return self._deny(
                    policy,
                    f"Resource '{action.resource}' is not included in the "
                    "policy's allowed resource set.",
                )

        resource_note = f" on '{action.resource}'" if action.resource else ""
        return GovernanceDecision(
            decision="ALLOW",
            reason=(
                f"Action '{action.action}'{resource_note} via tool '{tool}' "
                "is permitted."
            ),
            policy_version=policy.version,
        )

    @staticmethod
    def _deny(policy: GovernancePolicy, reason: str) -> GovernanceDecision:
        return GovernanceDecision(
            decision="DENY", reason=reason, policy_version=policy.version
        )
