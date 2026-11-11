from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import NotRequired, TypedDict
from uuid import UUID
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from devflow_ai.coordinator import RunCoordinator
from devflow_ai.enums import AgentExecutionStatus, AgentRole
from devflow_ai.models import AgentExecution, Handoff


class HandoffRoutingError(RuntimeError):
    """The Supervisor result cannot be routed in this execution cycle."""


@dataclass(frozen=True)
class CommittedHandoff:
    id: UUID
    from_agent_execution_id: UUID
    to_agent_role: AgentRole
    context: dict


class HandoffGraphState(TypedDict):
    run_id: str
    supervisor_execution_id: NotRequired[str]
    handoff_id: NotRequired[str]
    receiver_node: NotRequired[str]


def run_handoff_cycle(
    session_factory: sessionmaker[Session], run_id: UUID, worker_id: str, *,
    supervise: Callable[[UUID], UUID | None],
    receivers: Mapping[AgentRole, Callable[[CommittedHandoff], None]],
) -> UUID | None:
    """Run one Supervisor decision and dispatch one committed Handoff."""
    receiver_nodes = {role: f"receive_{role.value.lower()}" for role in receivers}

    def load_approval(supervisor_id: UUID) -> CommittedHandoff:
        with session_factory() as session, session.begin():
            RunCoordinator(session).assert_execution_right(run_id, worker_id)
            output = session.scalar(select(AgentExecution.output).where(
                AgentExecution.id == supervisor_id,
                AgentExecution.run_id == run_id,
                AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower(),
                AgentExecution.status == AgentExecutionStatus.SUCCEEDED,
            ))
            try:
                handoff_id = UUID((output or {})["approved_handoff_id"])
            except (KeyError, TypeError, ValueError, AttributeError):
                raise HandoffRoutingError("Supervisor has no committed Handoff approval for this Run") from None
            handoff = session.scalar(
                select(Handoff).join(AgentExecution, Handoff.from_agent_execution_id == AgentExecution.id)
                .where(Handoff.id == handoff_id, AgentExecution.run_id == run_id)
            )
            if handoff is None:
                raise HandoffRoutingError("Approved Handoff does not belong to this Run")
            return CommittedHandoff(
                id=handoff.id, from_agent_execution_id=handoff.from_agent_execution_id,
                to_agent_role=AgentRole(handoff.to_agent_role.upper()), context=handoff.context,
            )

    def supervisor_node(state: HandoffGraphState) -> dict[str, str]:
        with session_factory() as session, session.begin():
            RunCoordinator(session).assert_execution_right(run_id, worker_id)
        supervisor_id = supervise(UUID(state["run_id"]))
        if supervisor_id is None:
            return {}
        approval = load_approval(supervisor_id)
        receiver = receiver_nodes.get(approval.to_agent_role)
        if receiver is None:
            raise HandoffRoutingError(f"No receiver is registered for {approval.to_agent_role.value}")
        return {
            "supervisor_execution_id": str(supervisor_id),
            "handoff_id": str(approval.id),
            "receiver_node": receiver,
        }

    def choose_receiver(state: HandoffGraphState) -> str:
        return state.get("receiver_node", END)

    def receiver_node(state: HandoffGraphState) -> dict:
        approval = load_approval(UUID(state["supervisor_execution_id"]))
        if str(approval.id) != state["handoff_id"] or receiver_nodes.get(approval.to_agent_role) != state["receiver_node"]:
            raise HandoffRoutingError("Committed Handoff does not match the selected route")
        receivers[approval.to_agent_role](approval)
        return {}

    graph = StateGraph(HandoffGraphState)
    graph.add_node("supervisor", supervisor_node)
    graph.add_edge(START, "supervisor")
    for name in receiver_nodes.values():
        graph.add_node(name, receiver_node)
        graph.add_edge(name, END)
    graph.add_conditional_edges(
        "supervisor", choose_receiver, {name: name for name in (*receiver_nodes.values(), END)},
    )
    compiled = graph.compile(checkpointer=InMemorySaver())
    final = compiled.invoke({"run_id": str(run_id)}, {"configurable": {"thread_id": str(run_id)}})
    return UUID(final["handoff_id"]) if "handoff_id" in final else None
