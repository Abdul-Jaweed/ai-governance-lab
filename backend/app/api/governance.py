"""Read-only governance API (spec section 18).

These endpoints exist for inspection: a UI or CLI can list runs, read a
run's declared authority, and walk its decision timeline. The agent runtime
writes through the Python service layer, not by calling itself over HTTP.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.db.repositories import PostgresRecorder
from app.governance.events import build_evidence_record
from app.governance.models import GovernanceContext, GovernanceScope

router = APIRouter(prefix="/api/governance", tags=["governance"])


def get_recorder(request: Request) -> PostgresRecorder:
    recorder = getattr(request.app.state, "recorder", None)
    if recorder is None:  # pragma: no cover - misconfiguration
        raise HTTPException(status_code=503, detail="recorder not configured")
    return recorder


def _serialize_run(row, summary: dict[str, int]) -> dict[str, Any]:
    return {
        "run_id": row.run_id,
        "agent_id": row.agent_id,
        "intent": row.intent,
        "scope": row.scope,
        "policy_version": row.policy_version,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "status": row.status,
        "summary": summary,
    }


def _serialize_event(row) -> dict[str, Any]:
    return {
        "event_id": row.event_id,
        "run_id": row.run_id,
        "agent_id": row.agent_id,
        "event_type": row.event_type,
        "tool_name": row.tool_name,
        "action": row.action,
        "resource": row.resource,
        "decision": row.decision,
        "reason": row.reason,
        "policy_version": row.policy_version,
        "arguments_sanitized": row.arguments_sanitized,
        "arguments_hash": row.arguments_hash,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/runs")
async def list_runs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    recorder: PostgresRecorder = Depends(get_recorder),
) -> dict[str, Any]:
    rows = await recorder.list_runs(limit=limit, offset=offset)
    total = await recorder.count_runs()
    items = []
    for row in rows:
        summary = await recorder.summarize_run(row.run_id)
        items.append(_serialize_run(row, summary))
    return {"items": items, "limit": limit, "offset": offset, "total": total}


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str, recorder: PostgresRecorder = Depends(get_recorder)
) -> dict[str, Any]:
    row = await recorder.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    summary = await recorder.summarize_run(run_id)
    return _serialize_run(row, summary)


@router.get("/runs/{run_id}/events")
async def list_events(
    run_id: str,
    limit: int = Query(default=1000, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    recorder: PostgresRecorder = Depends(get_recorder),
) -> list[dict[str, Any]]:
    rows = await recorder.list_events(run_id, limit=limit, offset=offset)
    return [_serialize_event(r) for r in rows]


@router.get("/runs/{run_id}/evidence")
async def get_evidence(
    run_id: str, recorder: PostgresRecorder = Depends(get_recorder)
) -> dict[str, Any]:
    """The core governance artifact for a run (spec section 17)."""

    row = await recorder.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")

    from app.governance.models import GovernanceEvent

    events = [
        GovernanceEvent(
            event_id=e.event_id,
            run_id=e.run_id,
            agent_id=e.agent_id,
            event_type=e.event_type,
            tool_name=e.tool_name,
            action=e.action,
            resource=e.resource,
            decision=e.decision,
            reason=e.reason,
            policy_version=e.policy_version,
            timestamp=e.created_at,
            arguments_hash=e.arguments_hash,
            arguments_sanitized=e.arguments_sanitized,
        )
        for e in await recorder.list_events(run_id)
    ]

    scope = row.scope or {}
    context = GovernanceContext(
        run_id=row.run_id,
        agent_id=row.agent_id,
        intent=row.intent,
        scope=GovernanceScope(
            allowed_tools=set(scope.get("allowed_tools", [])),
            allowed_actions=set(scope.get("allowed_actions", [])),
            allowed_resources=set(scope.get("allowed_resources", [])),
        ),
        policy_version=row.policy_version,
    )
    return build_evidence_record(context, events)
