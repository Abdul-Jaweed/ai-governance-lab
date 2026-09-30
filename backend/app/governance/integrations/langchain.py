"""Governed LangChain tool wrapper (spec section 10).

``GovernedTool`` is a thin wrapper around any :class:`BaseTool`. It does
not modify LangChain internals: the authorization check happens inside
``_run``/``_arun``, i.e. immediately before the wrapped tool executes, so
a denied action never reaches the real tool.

The wrapper preserves LangChain's tool interface (name, description, args
schema, sync + async invocation) so governed tools can be handed straight
to a LangGraph ``ToolNode``.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from langchain_core.tools import BaseTool
from pydantic import ConfigDict, model_validator

from app.governance.governor import RuntimeGovernor
from app.governance.models import (
    ActionRequest,
    GovernanceContext,
)
from app.governance.policy import GovernancePolicy

# Reused to run the async governor from a synchronous LangChain code path
# even when an event loop is already running in the current thread.
_WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="governor-sync")


def _run_sync(coro):
    """Run *coro* from sync code, tolerating an already-running event loop."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    return _WORKER.submit(asyncio.run, coro).result()


class GovernedTool(BaseTool):
    """A LangChain tool whose execution is gated by the runtime governor."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    tool: BaseTool
    governor: RuntimeGovernor
    context: GovernanceContext
    policy: GovernancePolicy
    action: str
    resource: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _inherit_identity_from_wrapped_tool(cls, data: Any) -> Any:
        """Adopt the wrapped tool's name/description/schema by default."""

        if isinstance(data, dict) and isinstance(data.get("tool"), BaseTool):
            inner = data["tool"]
            data = dict(data)
            data.setdefault("name", inner.name)
            data.setdefault("description", inner.description)
            if data.get("args_schema") is None:
                data["args_schema"] = inner.args_schema
        return data

    # -- action construction -------------------------------------------------

    def _build_action(self, arguments: dict[str, Any]) -> ActionRequest:
        """Normalize the call into an ActionRequest.

        ``tool_name`` always comes from the wrapped tool, never from the
        caller's arguments, so a caller cannot spoof which tool is being
        governed (spec section 25).
        """

        return ActionRequest(
            tool_name=self.tool.name,
            action=self.action,
            resource=self.resource,
            arguments=arguments,
        )

    def _arguments(self, args: tuple, kwargs: dict) -> dict[str, Any]:
        if args and isinstance(args[0], dict):
            return dict(args[0])
        if kwargs:
            return dict(kwargs)
        if args:
            return {"input": args[0]}
        return {}

    # -- execution ----------------------------------------------------------

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        arguments = self._arguments(args, kwargs)
        action = self._build_action(arguments)
        _run_sync(self.governor.guard(self.context, action, self.policy))
        return self.tool.run(arguments)

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        arguments = self._arguments(args, kwargs)
        action = self._build_action(arguments)
        await self.governor.guard(self.context, action, self.policy)
        return await self.tool.arun(arguments)


__all__ = ["GovernedTool"]
