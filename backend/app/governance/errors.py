"""Exceptions raised by the runtime governor."""

from __future__ import annotations

from app.governance.models import ActionRequest, GovernanceDecision


class GovernanceError(Exception):
    """Base class for all governance errors."""


class GovernanceDenied(GovernanceError):
    """Raised when enforcement blocks an action.

    Carries both the :class:`GovernanceDecision` and the rejected
    :class:`ActionRequest` so callers can surface a structured error.
    """

    def __init__(self, decision: GovernanceDecision, action: ActionRequest) -> None:
        self.decision = decision
        self.action = action
        super().__init__(
            f"Governance denied tool '{action.tool_name}': {decision.reason}"
        )


class GovernanceApprovalRequired(GovernanceDenied):
    """Raised when an action is legal only with human approval.

    Subclasses :class:`GovernanceDenied` so a caller that blocks on any
    non-ALLOW outcome still catches it.
    """

    def __init__(self, decision: GovernanceDecision, action: ActionRequest) -> None:
        super().__init__(decision, action)
        self.args = (
            f"Governance requires approval for tool '{action.tool_name}': "
            f"{decision.reason}",
        )


class GovernanceRecordingError(GovernanceError):
    """Raised when a decision could not be persisted and fail-closed applies."""
