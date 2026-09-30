"""Runtime governor: the governance decision point (spec sections 9, 32, 36).

``authorize`` evaluates the action and records exactly one event before
returning the decision. ``guard`` additionally enforces the decision by
raising when the governor is in enforce mode. The decision itself is
deterministic and never depends on an LLM.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from app.governance.evaluator import GovernanceEvaluator
from app.governance.errors import (
    GovernanceApprovalRequired,
    GovernanceDenied,
    GovernanceRecordingError,
)
from app.governance.models import (
    ActionRequest,
    GovernanceContext,
    GovernanceDecision,
    GovernanceEvent,
)
from app.governance.policy import GovernancePolicy
from app.governance.recorder import GovernanceRecorder
from app.governance.sanitization import hash_arguments, sanitize_arguments

GovernanceMode = Literal["observe", "enforce"]
RecorderFailureMode = Literal["fail_closed", "allow"]


class RuntimeGovernor:
    """Owns evaluation, recording and enforcement for governed actions."""

    def __init__(
        self,
        recorder: GovernanceRecorder | None = None,
        evaluator: GovernanceEvaluator | None = None,
        mode: GovernanceMode = "enforce",
        on_recorder_failure: RecorderFailureMode = "fail_closed",
    ) -> None:
        if mode not in ("observe", "enforce"):
            raise ValueError(f"Unknown governance mode: {mode!r}")
        if on_recorder_failure not in ("fail_closed", "allow"):
            raise ValueError(
                f"Unknown recorder-failure mode: {on_recorder_failure!r}"
            )
        self.recorder = recorder
        self.evaluator = evaluator or GovernanceEvaluator()
        self.mode = mode
        self.on_recorder_failure = on_recorder_failure

    def evaluate(
        self,
        context: GovernanceContext,
        action: ActionRequest,
        policy: GovernancePolicy,
    ) -> GovernanceDecision:
        """Pure evaluation with no recording or enforcement."""

        return self.evaluator.evaluate(context, action, policy)

    def build_event(
        self,
        context: GovernanceContext,
        action: ActionRequest,
        decision: GovernanceDecision,
    ) -> GovernanceEvent:
        """Build the immutable governance event for a decision."""

        return GovernanceEvent(
            event_id=f"evt_{uuid.uuid4().hex}",
            run_id=context.run_id,
            agent_id=context.agent_id,
            tool_name=action.tool_name,
            action=action.action,
            resource=action.resource,
            decision=decision.decision,
            reason=decision.reason,
            policy_version=decision.policy_version,
            timestamp=datetime.now(timezone.utc),
            arguments_hash=hash_arguments(action.arguments),
            arguments_sanitized=sanitize_arguments(action.arguments),
        )

    async def authorize(
        self,
        context: GovernanceContext,
        action: ActionRequest,
        policy: GovernancePolicy,
    ) -> GovernanceDecision:
        """Evaluate and record a decision; does not block.

        Recording happens before any enforcement so that a decision is never
        silently lost. If recording fails, behaviour depends on
        ``on_recorder_failure`` (fail-closed by default).
        """

        decision = self.evaluate(context, action, policy)
        event = self.build_event(context, action, decision)

        if self.recorder is not None:
            try:
                await self.recorder.arecord(event)
            except Exception as exc:  # noqa: BLE001 - re-raised or swallowed by policy
                if self.on_recorder_failure == "fail_closed":
                    raise GovernanceRecordingError(
                        f"Failed to record governance event for tool "
                        f"'{action.tool_name}': {exc}"
                    ) from exc

        return decision

    async def guard(
        self,
        context: GovernanceContext,
        action: ActionRequest,
        policy: GovernancePolicy,
    ) -> GovernanceDecision:
        """Authorize and enforce: raise for blocked actions in enforce mode."""

        decision = await self.authorize(context, action, policy)

        if self.mode == "enforce":
            if decision.decision == "DENY":
                raise GovernanceDenied(decision, action)
            if decision.decision == "APPROVAL_REQUIRED":
                raise GovernanceApprovalRequired(decision, action)

        return decision
