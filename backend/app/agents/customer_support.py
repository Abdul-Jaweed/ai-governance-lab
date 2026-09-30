"""Sandbox customer-support agent (spec section 21).

Five fake tools that only manipulate an in-memory dict. The point is not
the agent's usefulness: it is to demonstrate that the runtime governor
controls what actually executes, whatever the model asks for.

Tool metadata (``action``/``resource``) is declared next to the tool, not
supplied by the model, so the governor always evaluates a trusted
description of the call.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool, tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import Annotated, TypedDict

from app.governance.governor import RuntimeGovernor
from app.governance.integrations.langchain import GovernedTool
from app.governance.models import GovernanceContext, GovernanceScope
from app.governance.policy import GovernancePolicy

AGENT_ID = "customer-support-agent"

CUSTOMER_SUPPORT_POLICY = GovernancePolicy(
    version="customer-support-v1",
    allowed_tools={"get_customer", "update_customer"},
    denied_tools={"refund_payment", "delete_customer"},
    approval_required_tools={"send_customer_email"},
    allowed_actions={"read", "update"},
    allowed_resources={"customer_profile"},
)

# tool name -> (governance action, resource)
TOOL_METADATA: dict[str, tuple[str, str | None]] = {
    "get_customer": ("read", "customer_profile"),
    "update_customer": ("update", "customer_profile"),
    "send_customer_email": ("update", "customer_profile"),
    "refund_payment": ("update", "payment"),
    "delete_customer": ("delete", "customer_profile"),
}


class CustomerStore(dict):
    """A deliberately fake customer database."""


_STORE = CustomerStore({"123": {"email": "old@example.com"}})


def customer_store() -> CustomerStore:
    return _STORE


def reset_customer_store() -> None:
    """Restore the fake database.

    ``emails_sent``/``refunds`` are deliberately absent: those keys are only
    created when a tool actually runs, so a test can assert the tool did
    not execute.
    """

    _STORE.clear()
    _STORE.update({"123": {"email": "old@example.com"}})


@tool
def get_customer(customer_id: str) -> str:
    """Fetch a customer profile by id."""
    profile = _STORE.get(customer_id)
    if not isinstance(profile, dict):
        return f"{customer_id}:not-found"
    return f"{customer_id}:{profile.get('email')}"


@tool
def update_customer(customer_id: str, email: str) -> str:
    """Update a customer's email address."""
    record = _STORE.setdefault(customer_id, {})
    record["email"] = email
    return f"updated:{customer_id}"


@tool
def send_customer_email(customer_id: str, subject: str) -> str:
    """Email the customer a message. Requires human approval."""
    _STORE.setdefault("emails_sent", []).append(subject)
    return f"emailed:{customer_id}"


@tool
def refund_payment(customer_id: str, amount: str) -> str:
    """Refund a payment to a customer. Blocked by policy."""
    _STORE["refunds"] = amount
    return f"refunded:{customer_id}:{amount}"


@tool
def delete_customer(customer_id: str) -> str:
    """Delete a customer record. Blocked by policy."""
    _STORE.pop(customer_id, None)
    return f"deleted:{customer_id}"


RAW_TOOLS: tuple[BaseTool, ...] = (
    get_customer,
    update_customer,
    send_customer_email,
    refund_payment,
    delete_customer,
)


def build_governance_context(
    *,
    run_id: str,
    intent: str = "Update customer contact information",
    scope: GovernanceScope | None = None,
) -> GovernanceContext:
    """Declare the run's authority. Mirrors the policy for this agent."""

    return GovernanceContext(
        run_id=run_id,
        agent_id=AGENT_ID,
        intent=intent,
        scope=scope
        or GovernanceScope(
            allowed_tools={"get_customer", "update_customer"},
            allowed_actions={"read", "update"},
            allowed_resources={"customer_profile"},
        ),
        policy_version=CUSTOMER_SUPPORT_POLICY.version,
    )


def build_governed_tools(
    governor: RuntimeGovernor,
    context: GovernanceContext,
    policy: GovernancePolicy = CUSTOMER_SUPPORT_POLICY,
    tools: tuple[BaseTool, ...] = RAW_TOOLS,
) -> list[GovernedTool]:
    """Wrap every sandbox tool in the governance boundary."""

    governed = []
    for t in tools:
        action, resource = TOOL_METADATA.get(t.name, ("execute", None))
        governed.append(
            GovernedTool(
                tool=t,
                governor=governor,
                context=context,
                policy=policy,
                action=action,
                resource=resource,
            )
        )
    return governed


class AgentState(TypedDict):
    messages: Annotated[list[Any], add_messages]


def _maybe_bind_tools(llm: BaseChatModel, tools: list[GovernedTool]) -> Any:
    """Bind tool schemas to the model when it supports that.

    Binding only informs a *real* model about the available tools; the tool
    node below resolves tools by name from the message. Scripted fake models
    used in tests do not implement ``bind_tools``, so fall back to the raw
    model instead of failing.
    """

    try:
        return llm.bind_tools(tools)
    except NotImplementedError:
        return llm


def build_governed_graph(
    llm: BaseChatModel,
    tools: list[GovernedTool],
) -> Any:
    """A minimal agent <-> tools loop with the governor at the tool boundary.

    Denials are converted into a tool result so the agent can continue and
    report the block; the governed tool itself is what prevents execution.
    """

    tool_by_name = {t.name: t for t in tools}
    llm_with_tools = _maybe_bind_tools(llm, tools)

    async def call_model(state: AgentState) -> AgentState:
        response = await llm_with_tools.ainvoke(state["messages"])
        return {"messages": [response]}

    async def call_tools(state: AgentState) -> AgentState:
        last = state["messages"][-1]
        outputs = []
        for call in getattr(last, "tool_calls", []) or []:
            tool = tool_by_name.get(call["name"])
            if tool is None:
                outputs.append(
                    AIMessage(
                        content=f"Unknown tool: {call['name']}",
                        tool_call_id=call.get("id"),
                        name=call["name"],
                    )
                )
                continue
            try:
                result = await tool.ainvoke(call["args"])
                outputs.append(
                    AIMessage(
                        content=str(result),
                        tool_call_id=call.get("id"),
                        name=call["name"],
                    )
                )
            except Exception as exc:  # governance denial / tool error
                outputs.append(
                    AIMessage(
                        content=f"Governance blocked '{call['name']}': {exc}",
                        tool_call_id=call.get("id"),
                        name=call["name"],
                    )
                )
        return {"messages": outputs}

    def should_continue(state: AgentState) -> str:
        last = state["messages"][-1]
        return "tools" if getattr(last, "tool_calls", None) else END

    builder = StateGraph(AgentState)
    builder.add_node("agent", call_model)
    builder.add_node("tools", call_tools)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    builder.add_edge("tools", "agent")
    return builder.compile()
