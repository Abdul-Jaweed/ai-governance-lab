"""End-of-run evidence record (spec section 17).

This is the core governance artifact: a single JSON-serialisable document
describing what the agent was allowed to do, what it attempted, and what
the governor decided.
"""

from __future__ import annotations

from typing import Any, Iterable

from app.governance.models import GovernanceContext, GovernanceEvent

SCHEMA_VERSION = "governance-evidence-v1"


def summarize(events: Iterable[GovernanceEvent]) -> dict[str, int]:
    """Decision counts for a set of events."""

    allowed = denied = approval = 0
    for event in events:
        if event.decision == "ALLOW":
            allowed += 1
        elif event.decision == "DENY":
            denied += 1
        else:  # APPROVAL_REQUIRED
            approval += 1
    return {
        "total_actions": allowed + denied + approval,
        "allowed": allowed,
        "denied": denied,
        "approval_required": approval,
    }


def build_evidence_record(
    context: GovernanceContext,
    events: Iterable[GovernanceEvent],
) -> dict[str, Any]:
    """Assemble the governance evidence record for one run.

    Only sanitized arguments are ever included, so this document is safe
    to export.
    """

    event_list = list(events)

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": context.run_id,
        "agent_id": context.agent_id,
        "intent": context.intent,
        "scope": {
            "allowed_tools": sorted(context.scope.allowed_tools),
            "allowed_actions": sorted(context.scope.allowed_actions),
            "allowed_resources": sorted(context.scope.allowed_resources),
        },
        "policy_version": context.policy_version,
        "events": [
            {
                "event_id": e.event_id,
                "tool": e.tool_name,
                "action": e.action,
                "resource": e.resource,
                "decision": e.decision,
                "reason": e.reason,
                "policy_version": e.policy_version,
                "timestamp": e.timestamp.isoformat(),
                "arguments_sanitized": e.arguments_sanitized,
                "arguments_hash": e.arguments_hash,
            }
            for e in event_list
        ],
        "summary": summarize(event_list),
    }
