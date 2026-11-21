from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import NotRequired, TypedDict
from uuid import UUID
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from devflow_ai.coordinator import RunCoordinator
from devflow_ai.enums import AgentExecutionStatus, AgentRole, RunStatus
from devflow_ai.models import AgentExecution, Handoff, Run


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


def run_task_stages(
    session_factory: sessionmaker[Session], run_id: UUID, worker_id: str, *,
    directories, gateways, receivers_factory, initial_supervise,
    post_explorer_supervise=None, post_planner_supervise=None, post_coder_supervise=None,
    max_stages: int = 32, stop_event=None,
) -> dict:
    """Drive one Run stage by stage until a stated stopping condition is reached.

    Each pass reads the **latest** non-Supervisor execution and decides from its role,
    status, output and committed Handoff — never from a role name alone and never from an
    older successful row. A stage that is still running or failed stops the drive; a
    Coder's PATCH_READY and BLOCKED both go to the Coder's own Supervisor; a recorded
    baseline authorises one further route for the Planner that asked for it; a blocked
    Verifier keeps its reason — including the case where it is deliberately still RUNNING,
    because a checkpointed draft waits for the formal evaluation to settle it; a formal
    pass is a terminal result, not a route.

    It never decides that a task succeeded and never retries: every stop names its reason,
    and an unanswerable stage is reported as such rather than treated as success.

    ``max_stages`` is a hard stop for a routing loop that keeps producing handoffs; the
    graph-step budget normally stops it first.
    """
    if max_stages <= 0:
        raise ValueError("max_stages must be positive")
    supervise_by_role = {
        AgentRole.EXPLORER: post_explorer_supervise,
        AgentRole.PLANNER: post_planner_supervise,
        AgentRole.CODER: post_coder_supervise,
    }
    receivers = receivers_factory()
    stages = 0
    while stages < max_stages:
        if stop_event is not None and stop_event.is_set():
            return {"outcome": "stopped", "reason": "stop_requested", "stages": stages}
        with session_factory() as session, session.begin():
            run_status = session.scalar(select(Run.status).where(Run.id == run_id))
        if run_status != RunStatus.RUNNING:
            # A Run that left RUNNING is a stated stop, not an execution-denied error:
            # the caller asked to drive it, and the honest answer is that it is over.
            return {"outcome": "stopped", "reason": "run_not_running", "stages": stages,
                    "run_status": run_status.value if run_status is not None else None}
        with session_factory() as session, session.begin():
            policy = RunCoordinator(session).assert_execution_right(run_id, worker_id)
            steps = session.scalar(select(func.count()).select_from(AgentExecution).where(
                AgentExecution.run_id == run_id))
            latest = session.execute(select(
                AgentExecution.id, AgentExecution.agent_role, AgentExecution.status,
                AgentExecution.output, AgentExecution.handoff_id).where(
                    AgentExecution.run_id == run_id,
                    AgentExecution.agent_role != AgentRole.SUPERVISOR.value.lower(),
                ).order_by(AgentExecution.created_at.desc(), AgentExecution.id.desc()).limit(1)
            ).first()
            stage = None
            if latest is not None:
                execution_id, role_value, status, output, handoff_id = latest
                output = dict(output or {})
                role = AgentRole(role_value.upper())
                outcome = output.get("outcome")
                if status == AgentExecutionStatus.RUNNING and role is AgentRole.VERIFIER \
                        and outcome == "VERIFICATION_BLOCKED":
                    # A blocked Verifier is checkpointed, not half-finished: the execution is
                    # deliberately left RUNNING so the formal evaluation can settle it, and the
                    # draft is already committed. Reporting this as an incomplete stage would
                    # hide a draft that is ready to be finalized.
                    stage = {"reason": "verification_blocked",
                             "missing_evidence": output.get("missing_evidence"),
                             "draft_pending_finalization": True}
                elif status == AgentExecutionStatus.RUNNING:
                    stage = {"reason": "last_stage_incomplete"}
                elif status != AgentExecutionStatus.SUCCEEDED:
                    stage = {"reason": "last_stage_not_succeeded"}
                elif role is AgentRole.VERIFIER:
                    if outcome == "BASELINE_RECORDED":
                        # A recorded baseline authorises exactly one further route for the
                        # Planner that asked for it; the Verifier itself routes nowhere.
                        source_id = session.scalar(select(Handoff.from_agent_execution_id).where(
                            Handoff.id == handoff_id))
                        source_role = session.scalar(select(AgentExecution.agent_role).where(
                            AgentExecution.id == source_id,
                            AgentExecution.run_id == run_id)) if source_id else None
                        if source_role != AgentRole.PLANNER.value.lower():
                            stage = {"reason": "baseline_source_unknown"}
                        elif post_planner_supervise is None:
                            stage = {"reason": "no_route_after_planner"}
                        else:
                            stage = {"supervise": post_planner_supervise(source_id)}
                    elif outcome == "VERIFICATION_BLOCKED":
                        stage = {"reason": "verification_blocked",
                                 "missing_evidence": output.get("missing_evidence")}
                    elif outcome == "VERIFICATION_PASSED":
                        stage = {"reason": "formal_pass_recorded"}
                    else:
                        stage = {"reason": "verifier_outcome_unsupported"}
                elif role is AgentRole.CODER:
                    # PATCH_READY and BLOCKED both go to the Coder's own Supervisor; the
                    # Supervisor decides, this driver does not read a role name as a verdict.
                    if outcome not in ("PATCH_READY", "BLOCKED"):
                        stage = {"reason": "coder_outcome_unsupported"}
                    elif post_coder_supervise is None:
                        stage = {"reason": "no_route_after_coder"}
                    else:
                        stage = {"supervise": post_coder_supervise(execution_id)}
                else:
                    builder = supervise_by_role.get(role)
                    if builder is None:
                        stage = {"reason": "no_route_after_" + role.value.lower()}
                    else:
                        stage = {"supervise": builder(execution_id)}
        if steps >= policy.budget.max_graph_steps_per_run:
            return {"outcome": "stopped", "reason": "graph_step_budget_exhausted",
                    "stages": stages}
        if stage is None:
            supervise = initial_supervise
        elif "supervise" in stage:
            supervise = stage["supervise"]
        else:
            return {"outcome": "stopped", "stages": stages, **stage}
        handoff_id = run_handoff_cycle(
            session_factory, run_id, worker_id, supervise=supervise, receivers=receivers)
        stages += 1
        if handoff_id is None:
            return {"outcome": "stopped", "reason": "supervisor_did_not_route",
                    "stages": stages}
    return {"outcome": "stopped", "reason": "stage_limit_reached", "stages": stages}

def drive_run_stages(
    session_factory: sessionmaker[Session], run_id: UUID, *, worker, gateways,
    directories, stop_event, verification_config, max_stages: int = 32,
    max_read_bytes: int = 65536, max_patch_bytes: int | None = None,
    verifier_max_diff_bytes: int | None = None, memory_recall=None,
) -> dict:
    """Assemble the receivers and supervision builders for one Run, then drive it.

    Both the benchmark entry and the worker entry execute the same stages, so the wiring
    lives here once. The guards that decide whether a Coder or a semantic Verifier takes
    part belong to that single copy: two entry points with slightly different wiring would
    be two different systems reported under one name.
    """
    coder_enabled = max_patch_bytes is not None and max_patch_bytes > 0
    verifier_enabled = verifier_max_diff_bytes is not None and verifier_max_diff_bytes > 0
    return run_task_stages(
        session_factory, run_id, worker.worker_id,
        directories=directories, gateways=gateways,
        receivers_factory=lambda: worker.build_handoff_receivers(
            run_id, directories=directories, max_bytes=max_read_bytes,
            stop_event=stop_event, explorer_gateway=gateways.explorer,
            planner_gateway=gateways.planner, verification_config=verification_config,
            **({"memory_recall": memory_recall} if memory_recall is not None else {}),
            **({"coder_gateway": gateways.coder,
                "max_patch_bytes": max_patch_bytes} if coder_enabled else {}),
            **({"verifier_gateway": gateways.verifier,
                "verifier_max_diff_bytes": verifier_max_diff_bytes} if verifier_enabled else {})),
        initial_supervise=worker.build_initial_supervise(
            run_id, model_gateway=gateways.supervisor, stop_event=stop_event),
        post_explorer_supervise=lambda execution_id: worker.build_post_explorer_supervise(
            run_id, explorer_execution_id=execution_id,
            model_gateway=gateways.supervisor, stop_event=stop_event),
        post_planner_supervise=lambda execution_id: worker.build_post_planner_supervise(
            run_id, planner_execution_id=execution_id,
            model_gateway=gateways.supervisor, stop_event=stop_event),
        post_coder_supervise=(
            None if not coder_enabled
            else lambda execution_id: worker.build_post_coder_supervise(
                run_id, coder_execution_id=execution_id, directories=directories,
                model_gateway=gateways.supervisor,
                max_patch_bytes=max_patch_bytes, stop_event=stop_event)),
        stop_event=stop_event, max_stages=max_stages)


def finalize_benchmark_run(
    worker, run_id: UUID, *, directories, verification_config=None, material=None,
    max_diff_bytes: int | None = None, max_patch_bytes: int | None = None, stop_event=None,
) -> dict:
    """Run the trusted independent evaluation for a Run that stopped at a Verifier draft.

    This adds no acceptance rule of its own: it locates the committed Verifier draft that
    may proceed and hands it to the existing finalize path, which runs the calibrated
    suite and commits the formal result inside its own gates. Missing material, an
    unpinned suite version, absent evidence limits, a draft whose public checks did not
    pass, or a missing draft all return a stated reason and commit nothing.
    """
    if material is None:
        return {"outcome": "not_finalized", "reason": "independent_evaluation_material_missing"}
    if verification_config is None or verification_config.independent_suite_version is None:
        return {"outcome": "not_finalized", "reason": "independent_suite_version_missing"}
    if (max_diff_bytes is None or max_diff_bytes <= 0
            or max_patch_bytes is None or max_patch_bytes <= 0):
        return {"outcome": "not_finalized", "reason": "evidence_limits_missing"}
    with worker.session_factory() as session:
        # A checkpointed draft lives on a *RUNNING* execution: the verifier stays resumable
        # until the formal evaluation settles it, so filtering on SUCCEEDED here would make
        # every reachable draft look absent.
        latest = session.execute(select(
            AgentExecution.id, AgentExecution.output, AgentExecution.status).where(
                AgentExecution.run_id == run_id,
                AgentExecution.agent_role == AgentRole.VERIFIER.value.lower(),
            ).order_by(AgentExecution.created_at.desc(), AgentExecution.id.desc()).limit(1)
        ).first()
    if latest is None:
        return {"outcome": "not_finalized", "reason": "no_verifier_execution"}
    execution_id, output, status = latest
    output = dict(output or {})
    if output.get("outcome") != "VERIFICATION_BLOCKED":
        reason = ("verifier_still_running"
                  if status == AgentExecutionStatus.RUNNING and not output
                  else "no_blocked_verifier_draft")
        return {"outcome": "not_finalized", "reason": reason,
                "verifier_execution_id": str(execution_id),
                "verifier_status": status.value}
    if output.get("preliminary_public_outcome") != "passed":
        return {"outcome": "not_finalized", "reason": "public_checks_not_passed"}
    try:
        draft_artifact_id = UUID(str(output.get("draft_artifact_id")))
    except (TypeError, ValueError):
        return {"outcome": "not_finalized", "reason": "draft_artifact_missing"}
    result = worker.finalize_with_independent_evaluation(
        run_id, agent_execution_id=execution_id, draft_artifact_id=draft_artifact_id,
        directories=directories, verification_config=verification_config,
        image=material.image, command=material.command, cases=material.cases,
        calibration_workspaces=material.calibration_workspaces,
        max_diff_bytes=max_diff_bytes, max_patch_bytes=max_patch_bytes,
        timeout_seconds=material.timeout_seconds, stop_event=stop_event)
    return {"outcome": "finalized", "independent_outcome": result.get("outcome"),
            **{key: value for key, value in result.items() if key != "outcome"}}


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
