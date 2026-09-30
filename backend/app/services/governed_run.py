"""The governed run service: the full runtime path for one agent run.

Creates the run, builds the governed tools, executes the graph, finalizes
the run, and exposes the resulting evidence. This is the piece an
application would call; it never goes back out over HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from langchain_core.language_models import BaseChatModel

from app.agents.customer_support import (
    CUSTOMER_SUPPORT_POLICY,
    RAW_TOOLS,
    build_governed_graph,
    build_governed_tools,
    build_governance_context,
)
from app.db.models import GovernanceEventRow, GovernanceRunRow
from app.db.repositories import PostgresRecorder
from app.governance.events import build_evidence_record
from app.governance.governor import RuntimeGovernor
from app.governance.models import GovernanceContext, GovernanceEvent, GovernanceScope
from app.governance.policy import GovernancePolicy

DEFAULT_INTENT = "Update customer contact information"


@dataclass(frozen=True)
class RunResult:
    """The outcome of a governed run."""

    run_id: str
    status: str
    final_message: str
    blocked: list[str]


class GovernedRunService:
    """Runs the sandbox agent under governance and records the evidence."""

    def __init__(
        self,
        recorder: PostgresRecorder,
        *,
        policy: GovernancePolicy = CUSTOMER_SUPPORT_POLICY,
        mode: str = "enforce",
        on_recorder_failure: str = "fail_closed",
    ) -> None:
        self.recorder = recorder
        self.policy = policy
        self.mode = mode
        self.on_recorder_failure = on_recorder_failure

    # -- execution -----------------------------------------------------------

    async def run(
        self,
        *,
        llm: BaseChatModel,
        prompt: str,
        intent: str = DEFAULT_INTENT,
        scope: GovernanceScope | None = None,
    ) -> RunResult:
        from langchain_core.messages import HumanMessage

        run_id = self._new_run_id()
        context = build_governance_context(
            run_id=run_id, intent=intent, scope=scope
        )

        await self.recorder.add_run(
            run_id=context.run_id,
            agent_id=context.agent_id,
            intent=context.intent,
            scope=self._scope_payload(context),
            policy_version=context.policy_version,
        )

        governor = RuntimeGovernor(
            recorder=self.recorder,
            mode=self.mode,
            on_recorder_failure=self.on_recorder_failure,
        )
        tools = build_governed_tools(governor, context, self.policy, RAW_TOOLS)
        graph = build_governed_graph(llm, tools)

        status = "completed"
        try:
            result = await graph.ainvoke({"messages": [HumanMessage(prompt)]})
            final_message = str(result["messages"][-1].content)
        except Exception:
            await self.recorder.finalize_run(run_id=run_id, status="failed")
            raise

        await self.recorder.finalize_run(run_id=run_id, status=status)

        events = await self.get_events(run_id)
        blocked = [e.tool_name for e in events if e.decision != "ALLOW"]
        return RunResult(
            run_id=run_id,
            status=status,
            final_message=final_message,
            blocked=blocked,
        )

    # -- reads ---------------------------------------------------------------

    async def get_run(self, run_id: str) -> GovernanceRunRow | None:
        return await self.recorder.get_run(run_id)

    async def get_events(self, run_id: str) -> list[GovernanceEventRow]:
        return await self.recorder.list_events(run_id)

    async def get_evidence(self, run_id: str) -> dict[str, Any]:
        """The core governance artifact for a run (spec section 17)."""

        row = await self.recorder.get_run(run_id)
        if row is None:
            raise KeyError(run_id)

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
            for e in await self.recorder.list_events(run_id)
        ]

        payload = row.scope or {}
        context = GovernanceContext(
            run_id=row.run_id,
            agent_id=row.agent_id,
            intent=row.intent,
            scope=GovernanceScope(
                allowed_tools=set(payload.get("allowed_tools", [])),
                allowed_actions=set(payload.get("allowed_actions", [])),
                allowed_resources=set(payload.get("allowed_resources", [])),
            ),
            policy_version=row.policy_version,
        )
        return build_evidence_record(context, events)

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _new_run_id() -> str:
        return f"run_{uuid4().hex[:16]}"

    @staticmethod
    def _scope_payload(context: GovernanceContext) -> dict[str, Any]:
        return {
            "allowed_tools": sorted(context.scope.allowed_tools),
            "allowed_actions": sorted(context.scope.allowed_actions),
            "allowed_resources": sorted(context.scope.allowed_resources),
        }
