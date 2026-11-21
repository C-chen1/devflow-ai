"""Two budget layers around one model request, against real PostgreSQL rows.

Every model call here is a stub: what is real is the ordering (both reservations before the
call), the compensation when one layer refuses, and how a failed or unmeasured call is recorded.
Nothing in this file proves the provider was reached or that usage is accurate — those numbers
are what the stub reports.

The experiment layer is opt-in, so one case also pins the default path: a gateway built without
a hook must behave exactly as before and must not touch the experiment accounting.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from threading import Barrier
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text

from devflow_ai.admission_budget import AdmissionBudgetService
from devflow_ai.coordinator import ModelTokenUsage, RunCoordinator, RunExecutionDeniedError
from devflow_ai.enums import AgentExecutionStatus, AgentRole, RunStatus, TaskStatus
from devflow_ai.experiment import ExperimentLimitError, ExperimentService
from devflow_ai.experiment_budget import ExperimentBudgetHook
from devflow_ai.model_gateway import AdmissionModelGateway, ModelGateway, ModelReply
from devflow_ai.model_gateways import model_gateways_from_env
from devflow_ai.models import AgentExecution, Experiment, ExperimentReservation, ExperimentTaskResult, Task
from devflow_ai.database import get_database_time
from devflow_ai.services import AdmissionRecord, TaskService

MIGRATION = "d9b3c7e1a250"
BASE = "a" * 40
SCRIPTS = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations"))


@pytest.fixture
def budget_session_factory(worker_session_factory):
    """Add the experiment tables to the isolated schema."""
    engine = worker_session_factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    with engine.begin() as connection:
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        with Operations.context(MigrationContext.configure(connection)):
            SCRIPTS.get_revision(MIGRATION).module.upgrade()
    return worker_session_factory


def _task(factory):
    with factory() as session:
        task, _ = TaskService(session).create_task(
            title="Budget task", description="Fixture", repo="example-repo",
            idempotency_key=uuid4())
        task_id = task.id
    with factory() as session:
        TaskService(session).apply_admission_decision(
            task_id, TaskStatus.ELIGIBLE, base_commit=BASE,
            admission_record=AdmissionRecord(reason="fixture", evidence=["fixture"]))
    return task_id


def _set_run_token_budget(factory, task_id, tokens):
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = dict(task.policy_snapshot)
        snapshot["budget"] = {**snapshot["budget"], "max_llm_tokens_per_run": tokens}
        task.policy_snapshot = snapshot


def _running_attempt(factory, worker_id="budget-worker"):
    task_id = _task(factory)
    with factory() as session:
        # The coordinator commits its own transaction, so this session is used plainly.
        run, _ = RunCoordinator(session).start_initial_run(
            task_id, worker_id, f"key-{uuid4()}")
        run_id = run.id
        execution = AgentExecution(
            run_id=run_id, agent_role=AgentRole.EXPLORER.value.lower(),
            status=AgentExecutionStatus.RUNNING, input={}, output={})
        session.add(execution)
        session.commit()
        return task_id, run_id, execution.id


def _experiment(factory, task_id, *, tokens=1000, candidate="a"):
    with factory.begin() as session:
        experiment_id = ExperimentService(session).open(
            identity_key=f"budget-{uuid4()}", base_execution_sha256="b" * 64,
            task_ids=[task_id],
            limits=__import__("devflow_ai.experiment", fromlist=["ExperimentLimits"])
            .ExperimentLimits(max_candidates=2, max_total_model_tokens=tokens,
                              max_total_seconds=3600, promotion_min_formal_passes=1,
                              promotion_max_failures=0),
            created_by="operator-a")
    if candidate is not None:
        with factory.begin() as session:
            ExperimentService(session).admit_candidate(
                experiment_id=experiment_id, label=candidate, execution_sha256="1" * 64)
    return experiment_id


def _hook(factory, experiment_id, *, candidate="a"):
    return ExperimentBudgetHook(session_factory=factory, experiment_id=experiment_id,
                                candidate_label=candidate, operator="operator-a")


def _gateway(factory, calls, hook, *, usage=(10, 5), estimate=5, max_output=10,
             worker_id="budget-worker", raise_error=None):
    def generate(prompt, max_output_tokens):
        calls.append((prompt, max_output_tokens))
        if raise_error is not None:
            raise raise_error
        if usage is None:
            return ModelReply(output="reply", usage=None)
        return ModelReply(output="reply",
                          usage=ModelTokenUsage(input_tokens=usage[0], output_tokens=usage[1]))

    return ModelGateway(session_factory=factory, worker_id=worker_id,
                        estimate_input_tokens=lambda prompt: estimate,
                        generate=generate, max_output_tokens=max_output,
                        experiment_budget=hook)


def _reservations(factory, experiment_id):
    with factory() as session:
        return session.scalars(select(ExperimentReservation).where(
            ExperimentReservation.experiment_id == experiment_id).order_by(
            ExperimentReservation.created_at)).all()


def _run_usage(factory, execution_id):
    with factory() as session:
        usage = session.get(AgentExecution, execution_id).llm_usage or {}
    return usage.get("model_requests", {})


def test_both_layers_reserve_before_the_call_and_settle_after(
        budget_session_factory):
    factory = budget_session_factory
    task_id, run_id, execution_id = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id)
    calls = []
    gateway = _gateway(factory, calls, _hook(factory, experiment_id))

    assert gateway.request(run_id, agent_execution_id=execution_id, prompt="hello") == "reply"

    assert calls == [("hello", 10)]
    reservations = _reservations(factory, experiment_id)
    assert len(reservations) == 1
    settled = reservations[0]
    assert settled.state == "settled" and settled.actual_tokens == 15
    assert settled.run_id == run_id and settled.agent_execution_id == execution_id
    with factory() as session:
        usage = session.get(AgentExecution, execution_id).llm_usage
    entry = next(iter(usage["model_requests"].values()))
    assert entry["actual_usage"] == {"input_tokens": 10, "output_tokens": 5}


def test_the_experiment_ceiling_stops_the_call_before_the_run_budget(budget_session_factory):
    factory = budget_session_factory
    task_id, run_id, execution_id = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id, tokens=10)
    calls = []
    gateway = _gateway(factory, calls, _hook(factory, experiment_id))

    # One request needs 5 estimated input + 10 output > the whole experiment ceiling.
    assert gateway.request(run_id, agent_execution_id=execution_id, prompt="hello") is None

    assert calls == []
    assert _reservations(factory, experiment_id) == []
    assert _run_usage(factory, execution_id) == {}


def test_a_run_budget_refusal_gives_the_outer_reservation_back(budget_session_factory):
    factory = budget_session_factory
    task_id, run_id, execution_id = _running_attempt(factory)
    _set_run_token_budget(factory, task_id, 1)
    experiment_id = _experiment(factory, task_id)
    calls = []
    gateway = _gateway(factory, calls, _hook(factory, experiment_id))

    assert gateway.request(run_id, agent_execution_id=execution_id, prompt="hello") is None

    assert calls == []
    reservations = _reservations(factory, experiment_id)
    assert len(reservations) == 1
    assert reservations[0].state == "released"
    assert reservations[0].ended_reason
    with factory() as session:
        experiment = ExperimentService(session).read(experiment_id)
        assert experiment.reserved_tokens == 0 and experiment.settled_tokens == 0


def test_a_failed_call_is_unknown_and_blocks_further_requests(budget_session_factory):
    factory = budget_session_factory
    task_id, run_id, execution_id = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id)
    calls = []
    gateway = _gateway(factory, calls, _hook(factory, experiment_id),
                       raise_error=RuntimeError("provider exploded"))

    with pytest.raises(RuntimeError):
        gateway.request(run_id, agent_execution_id=execution_id, prompt="hello")

    reservations = _reservations(factory, experiment_id)
    assert len(reservations) == 1
    # The call may have reached the provider: unknown, not zero, not released.
    assert reservations[0].state == "unknown"
    assert reservations[0].actual_tokens is None
    with factory() as session:
        experiment = ExperimentService(session).read(experiment_id)
        assert experiment.settled_tokens == 0
        assert experiment.reserved_tokens == 0
    # The Run reservation is still committed, with no measured usage.
    entry = next(iter(_run_usage(factory, execution_id).values()))
    assert entry["actual_usage"] is None
    # And the unknown usage stops the next request in this experiment.
    assert gateway.request(run_id, agent_execution_id=execution_id, prompt="again") is None
    assert len(calls) == 1


def test_missing_usage_is_also_recorded_as_unknown(budget_session_factory):
    factory = budget_session_factory
    task_id, run_id, execution_id = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id)
    calls = []
    gateway = _gateway(factory, calls, _hook(factory, experiment_id), usage=None)

    assert gateway.request(run_id, agent_execution_id=execution_id, prompt="hello") == "reply"

    reservations = _reservations(factory, experiment_id)
    assert reservations[0].state == "unknown"
    entry = next(iter(_run_usage(factory, execution_id).values()))
    assert entry["actual_usage"] is None


def test_each_request_gets_its_own_reservation(budget_session_factory):
    factory = budget_session_factory
    task_id, run_id, execution_id = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id)
    calls = []
    gateway = _gateway(factory, calls, _hook(factory, experiment_id))

    for _ in range(2):
        assert gateway.request(run_id, agent_execution_id=execution_id, prompt="hello") == "reply"

    reservations = _reservations(factory, experiment_id)
    assert len(reservations) == 2
    assert {item.request_key for item in reservations} .__len__() == 2
    assert sum(item.actual_tokens for item in reservations) == 30


def test_without_the_hook_the_gateway_is_unchanged(budget_session_factory):
    factory = budget_session_factory
    task_id, run_id, execution_id = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id)
    calls = []
    gateway = _gateway(factory, calls, None)

    assert gateway.request(run_id, agent_execution_id=execution_id, prompt="hello") == "reply"

    assert calls == [("hello", 10)]
    assert _reservations(factory, experiment_id) == []
    entry = next(iter(_run_usage(factory, execution_id).values()))
    assert entry["actual_usage"] == {"input_tokens": 10, "output_tokens": 5}


def test_concurrent_requests_cannot_oversubscribe_the_experiment_ceiling(
        budget_session_factory):
    factory = budget_session_factory
    task_id, run_id, execution_id = _running_attempt(factory)
    # Each request reserves 15 (5 estimated input + 10 output); 100 admits at most six.
    experiment_id = _experiment(factory, task_id, tokens=100)
    calls = []

    def attempt(_index):
        gateway = _gateway(factory, calls, _hook(factory, experiment_id))
        with factory() as session:
            execution = AgentExecution(
                run_id=run_id, agent_role=AgentRole.EXPLORER.value.lower(),
                status=AgentExecutionStatus.RUNNING, input={}, output={})
            session.add(execution)
            session.commit()
            execution_id_for_call = execution.id
        try:
            return gateway.request(run_id, agent_execution_id=execution_id_for_call,
                                   prompt="hello")
        except Exception as error:  # any refusal shape counts as "no call"
            return f"error:{type(error).__name__}"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    succeeded = [item for item in results if item == "reply"]
    assert len(succeeded) == len(calls)
    assert len(succeeded) <= 6
    with factory() as session:
        experiment = ExperimentService(session).read(experiment_id)
        assert experiment.settled_tokens <= 100
        assert experiment.reserved_tokens == 0


class _RecordingHook:
    """Records the order the gateway calls the outer layer in, without a database."""

    def __init__(self, *, refuse=False):
        self.calls = []
        self.refuse = refuse

    def open_request(self, *, estimated_tokens, run_id=None, agent_execution_id=None):
        self.calls.append(("open", estimated_tokens, run_id))
        return None if self.refuse else uuid4()

    def mark_dispatched(self, request_key):
        self.calls.append(("dispatch",))

    def settle(self, request_key, usage):
        self.calls.append(("settle", usage.input_tokens + usage.output_tokens))

    def mark_unknown(self, request_key, reason):
        self.calls.append(("unknown", reason))

    def release(self, request_key, reason):
        self.calls.append(("release", reason))


def test_an_admission_request_takes_the_outer_layer_first(budget_session_factory):
    factory = budget_session_factory
    with factory() as session:
        task, _ = TaskService(session).create_task(
            title="Admission budget", description="Fixture", repo="example-repo",
            idempotency_key=uuid4())
        task_id = task.id
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = dict(task.policy_snapshot)
        snapshot["budget"] = {**snapshot["budget"], "max_admission_llm_tokens_per_task": 1000}
        task.policy_snapshot = snapshot
    calls = []
    hook = _RecordingHook()
    gateway = AdmissionModelGateway(
        session_factory=factory, estimate_input_tokens=lambda prompt: 5,
        generate=lambda prompt, limit: calls.append(prompt) or ModelReply(
            output="admission",
            usage=ModelTokenUsage(input_tokens=10, output_tokens=5)),
        max_output_tokens=10, experiment_budget=hook)

    assert gateway.request(task_id, prompt="hello") == "admission"

    # The outer reservation is taken before the provider call, with no Run attached, and
    # settled with the reported usage afterwards.
    assert hook.calls == [("open", 15, None), ("dispatch",), ("settle", 15)]
    assert calls == ["hello"]


def test_an_admission_refusal_at_the_outer_layer_sends_nothing(budget_session_factory):
    factory = budget_session_factory
    with factory() as session:
        task, _ = TaskService(session).create_task(
            title="Admission refused", description="Fixture", repo="example-repo",
            idempotency_key=uuid4())
        task_id = task.id
    calls = []
    hook = _RecordingHook(refuse=True)
    gateway = AdmissionModelGateway(
        session_factory=factory, estimate_input_tokens=lambda prompt: 5,
        generate=lambda prompt, limit: calls.append(prompt) or ModelReply(output="x", usage=None),
        max_output_tokens=10, experiment_budget=hook)

    assert gateway.request(task_id, prompt="hello") is None

    assert calls == []
    assert [item[0] for item in hook.calls] == ["open"]


def test_the_request_is_marked_dispatched_before_the_call(budget_session_factory):
    factory = budget_session_factory
    task_id, run_id, execution_id = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id)
    observed = []

    def generate(prompt, limit):
        with factory() as session:
            row = session.scalar(select(ExperimentReservation).where(
                ExperimentReservation.experiment_id == experiment_id))
            observed.append((row.state, row.stage, row.dispatched_at is not None))
        return ModelReply(output="reply",
                          usage=ModelTokenUsage(input_tokens=10, output_tokens=5))

    gateway = ModelGateway(session_factory=factory, worker_id="budget-worker",
                           estimate_input_tokens=lambda prompt: 5, generate=generate,
                           max_output_tokens=10,
                           experiment_budget=_hook(factory, experiment_id))
    assert gateway.request(run_id, agent_execution_id=execution_id, prompt="hello") == "reply"

    # Mid-call the reservation is already past the point of no return, and still unsettled.
    assert observed == [("reserved", "dispatched", True)]


def test_a_dispatched_request_cannot_be_released(budget_session_factory):
    factory = budget_session_factory
    task_id, _run_id, _execution_id = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id)
    hook = _hook(factory, experiment_id)
    key = hook.open_request(estimated_tokens=15, run_id=None)
    assert key is not None
    hook.mark_dispatched(key)

    with pytest.raises(Exception) as refusal:
        hook.release(key, "no usage")
    assert "may already have been sent" in str(refusal.value)
    with factory() as session:
        service = ExperimentService(session)
        row = session.scalar(select(ExperimentReservation).where(
            ExperimentReservation.experiment_id == experiment_id))
        assert row.state == "reserved" and row.stage == "dispatched"
        summary = service.summary(experiment_id)
        assert summary["usage"]["dispatched_unsettled"] == 1
        assert any("may have been sent" in gap
                   for gap in summary["usage"]["accounting_gaps"])


@pytest.mark.parametrize("stop", ["unknown", "over_budget", "committed_over_budget", "closed", "deadline"])
def test_predispatch_rechecks_experiment_stop_conditions(budget_session_factory, stop):
    factory = budget_session_factory
    task_id, _, _ = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id, tokens=100)
    hook = _hook(factory, experiment_id)
    first = hook.open_request(estimated_tokens=15)
    second = hook.open_request(estimated_tokens=15)
    hook.mark_dispatched(first)
    if stop == "unknown":
        hook.mark_unknown(first, "usage unavailable")
    elif stop in ("over_budget", "committed_over_budget"):
        amount = 105 if stop == "over_budget" else 90
        hook.settle(first, ModelTokenUsage(input_tokens=amount, output_tokens=0))
    elif stop == "closed":
        with factory.begin() as session:
            ExperimentService(session).close(experiment_id=experiment_id, reason="stop")
        hook.settle(first, ModelTokenUsage(input_tokens=10, output_tokens=0))
    else:
        hook.settle(first, ModelTokenUsage(input_tokens=10, output_tokens=0))
        with factory.begin() as session:
            now = get_database_time(session)
            experiment = session.get(Experiment, experiment_id)
            experiment.created_at = now - timedelta(seconds=2)
            experiment.deadline_at = now - timedelta(seconds=1)
    with pytest.raises(ExperimentLimitError):
        hook.mark_dispatched(second)
    with factory() as session:
        row = session.scalar(select(ExperimentReservation).where(
            ExperimentReservation.experiment_id == experiment_id,
            ExperimentReservation.request_key == second))
        assert row.state == "reserved" and row.stage == "reserved"


def test_a_failed_settlement_leaves_a_request_that_blocks_the_budget(
        budget_session_factory, monkeypatch):
    factory = budget_session_factory
    task_id, run_id, execution_id = _running_attempt(factory)
    # Plenty of budget left on purpose: the block has to come from the unsettled request,
    # not from the ceiling.
    experiment_id = _experiment(factory, task_id, tokens=1000)
    hook = _hook(factory, experiment_id)

    def broken_settle(request_key, usage):
        raise RuntimeError("settlement store unavailable")

    monkeypatch.setattr(hook, "settle", broken_settle)
    calls = []
    gateway = _gateway(factory, calls, hook)

    with pytest.raises(RuntimeError):
        gateway.request(run_id, agent_execution_id=execution_id, prompt="hello")

    assert len(calls) == 1
    with factory() as session:
        row = session.scalar(select(ExperimentReservation).where(
            ExperimentReservation.experiment_id == experiment_id))
        assert row.state == "reserved" and row.stage == "dispatched"
    # The provider was paid; the reservation may not be released, and the experiment admits no
    # second request while this one may still be on its way — even though the budget would fit.
    second = _gateway(factory, calls, hook)
    assert second.request(run_id, agent_execution_id=execution_id, prompt="again") is None
    with factory() as session:
        stuck_key = session.scalar(select(ExperimentReservation.request_key).where(
            ExperimentReservation.experiment_id == experiment_id))
    with pytest.raises(Exception) as refusal:
        hook.release(stuck_key, "give it back")
    assert "may already have been sent" in str(refusal.value)


def test_a_failed_unknown_marking_keeps_the_request_blocking(
        budget_session_factory, monkeypatch):
    factory = budget_session_factory
    task_id, run_id, execution_id = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id, tokens=1000)
    hook = _hook(factory, experiment_id)

    def broken_unknown(request_key, reason):
        raise RuntimeError("unknown store unavailable")

    monkeypatch.setattr(hook, "mark_unknown", broken_unknown)
    calls = []
    gateway = _gateway(factory, calls, hook, raise_error=ValueError("provider failed"))

    # The provider error reaches the caller; the accounting one does not mask it.
    with pytest.raises(ValueError):
        gateway.request(run_id, agent_execution_id=execution_id, prompt="hello")

    assert len(calls) == 1
    with factory() as session:
        row = session.scalar(select(ExperimentReservation).where(
            ExperimentReservation.experiment_id == experiment_id))
        # Not released, not settled, not "unknown": exactly "may have been sent".
        assert row.state == "reserved" and row.stage == "dispatched"
        summary = ExperimentService(session).summary(experiment_id)
        assert summary["usage"]["dispatched_unsettled"] == 1
    # The unknown never got recorded, so the request still blocks: no second model call.
    second_calls = []
    second = _gateway(factory, second_calls, hook)
    assert second.request(run_id, agent_execution_id=execution_id, prompt="again") is None
    assert second_calls == []


def test_a_run_or_execution_outside_the_experiment_cannot_be_attributed(
        budget_session_factory):
    factory = budget_session_factory
    task_id, run_id, execution_id = _running_attempt(factory)
    other_task, other_run, _other_execution = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id)
    hook = _hook(factory, experiment_id)

    # A Run of a Task this experiment never declared.
    with pytest.raises(Exception) as refusal:
        hook.open_request(estimated_tokens=15, run_id=other_run)
    assert "outside the experiment" in str(refusal.value)
    # An execution that belongs to a different Run than the one named.
    with pytest.raises(Exception) as refusal:
        hook.open_request(estimated_tokens=15, run_id=run_id,
                          agent_execution_id=_other_execution)
    assert "does not belong" in str(refusal.value)
    # The declared Task's own Run works, and records both identifiers.
    key = hook.open_request(estimated_tokens=15, run_id=run_id,
                            agent_execution_id=execution_id)
    assert key is not None
    with factory() as session:
        row = session.scalar(select(ExperimentReservation).where(
            ExperimentReservation.experiment_id == experiment_id))
        assert row.run_id == run_id and row.agent_execution_id == execution_id
    assert other_task != task_id and other_run != run_id


def test_actual_usage_above_the_reservation_is_recorded_and_stops_spending(
        budget_session_factory):
    factory = budget_session_factory
    task_id, run_id, execution_id = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id, tokens=20)
    calls = []
    # The provider reports far more than the 15 the request reserved.
    gateway = _gateway(factory, calls, _hook(factory, experiment_id), usage=(30, 10))

    assert gateway.request(run_id, agent_execution_id=execution_id, prompt="hello") == "reply"
    with factory() as session:
        service = ExperimentService(session)
        row = session.scalar(select(ExperimentReservation).where(
            ExperimentReservation.experiment_id == experiment_id))
        assert row.state == "settled" and row.actual_tokens == 40
        assert service.read(experiment_id).over_budget is True
    # Over the ceiling: no further request is admitted.
    assert gateway.request(run_id, agent_execution_id=execution_id, prompt="again") is None
    assert len(calls) == 1


def test_dispatching_and_releasing_the_same_request_race_safely(
        budget_session_factory):
    factory = budget_session_factory
    task_id, _run_id, _execution_id = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id)
    hook = _hook(factory, experiment_id)
    key = hook.open_request(estimated_tokens=15)
    barrier = Barrier(2)

    def dispatch():
        barrier.wait()
        try:
            hook.mark_dispatched(key)
            return "dispatched"
        except Exception as error:
            return f"dispatch:{type(error).__name__}"

    def release():
        barrier.wait()
        try:
            hook.release(key, "no usage")
            return "released"
        except Exception as error:
            return f"release:{type(error).__name__}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = {pool.submit(dispatch), pool.submit(release)}
        results = sorted(future.result() for future in outcomes)

    with factory() as session:
        row = session.scalar(select(ExperimentReservation).where(
            ExperimentReservation.experiment_id == experiment_id))
    if row.state == "released":
        # Release may only win before dispatch: then nothing was sent.
        assert row.stage == "reserved" and "released" in results
    else:
        # Dispatch won: the request may have been sent, so release must have been refused.
        assert row.state == "reserved" and row.stage == "dispatched"
        assert any(item.startswith("release:") for item in results)


def test_a_reliable_settlement_lets_the_next_request_through(budget_session_factory):
    factory = budget_session_factory
    task_id, run_id, execution_id = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id, tokens=1000)
    calls = []
    gateway = _gateway(factory, calls, _hook(factory, experiment_id))

    assert gateway.request(run_id, agent_execution_id=execution_id, prompt="one") == "reply"
    assert gateway.request(run_id, agent_execution_id=execution_id, prompt="two") == "reply"

    assert len(calls) == 2
    with factory() as session:
        reservations = session.scalars(select(ExperimentReservation).where(
            ExperimentReservation.experiment_id == experiment_id)).all()
        assert [row.state for row in reservations] == ["settled", "settled"]
        assert all(row.stage == "dispatched" for row in reservations)


def test_an_unknown_usage_still_stops_the_experiment(budget_session_factory):
    factory = budget_session_factory
    task_id, run_id, execution_id = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id, tokens=1000)
    calls = []
    gateway = _gateway(factory, calls, _hook(factory, experiment_id), usage=None)

    assert gateway.request(run_id, agent_execution_id=execution_id, prompt="one") == "reply"
    assert gateway.request(run_id, agent_execution_id=execution_id, prompt="two") is None

    assert len(calls) == 1
    with factory() as session:
        row = session.scalar(select(ExperimentReservation).where(
            ExperimentReservation.experiment_id == experiment_id))
        assert row.state == "unknown"


def test_only_one_reserved_request_can_reach_the_provider(budget_session_factory):
    factory = budget_session_factory
    task_id, _run_id, _execution_id = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id, tokens=1000)
    hook = _hook(factory, experiment_id)
    # Both requests get their reservation before either is dispatched: exactly the window a
    # reserve-time check cannot cover.
    first = hook.open_request(estimated_tokens=15)
    second = hook.open_request(estimated_tokens=15)
    assert first is not None and second is not None
    barrier = Barrier(2)

    def dispatch(key):
        barrier.wait()
        try:
            hook.mark_dispatched(key)
            return "dispatched"
        except Exception as error:
            return f"refused:{type(error).__name__}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(dispatch, (first, second)))

    assert outcomes.count("dispatched") == 1
    assert outcomes.count("refused:ExperimentLimitError") == 1
    with factory() as session:
        rows = {row.request_key: row for row in session.scalars(
            select(ExperimentReservation).where(
                ExperimentReservation.experiment_id == experiment_id))}
    winner = first if rows[first].stage == "dispatched" else second
    loser = second if winner == first else first
    assert rows[winner].state == "reserved"
    # The loser still provably never left the process, so its reservation can be given back;
    # the winner may not.
    hook.release(loser, "dispatch was refused")
    with factory() as session:
        assert session.scalar(select(ExperimentReservation).where(
            ExperimentReservation.request_key == loser)).state == "released"
    with pytest.raises(Exception) as refusal:
        hook.release(winner, "give it back")
    assert "may already have been sent" in str(refusal.value)


def test_the_gateway_backs_out_when_dispatch_is_refused(
        budget_session_factory, monkeypatch):
    factory = budget_session_factory
    task_id, run_id, execution_id = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id, tokens=1000)
    hook = _hook(factory, experiment_id)

    def refused(request_key):
        raise ExperimentLimitError("another request may already have been sent")

    monkeypatch.setattr(hook, "mark_dispatched", refused)
    calls = []
    gateway = _gateway(factory, calls, hook)

    assert gateway.request(run_id, agent_execution_id=execution_id, prompt="hello") is None

    # Nothing was sent, and the outer reservation went back.
    assert calls == []
    with factory() as session:
        row = session.scalar(select(ExperimentReservation).where(
            ExperimentReservation.experiment_id == experiment_id))
        assert row.state == "released"
        assert row.stage == "reserved"


def test_a_crashed_dispatched_request_blocks_new_reservations(budget_session_factory):
    factory = budget_session_factory
    task_id, _run_id, _execution_id = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id, tokens=1000)
    hook = _hook(factory, experiment_id)
    stuck = hook.open_request(estimated_tokens=15)
    hook.mark_dispatched(stuck)

    # The hook reports "do not call" rather than raising; the service is what refuses.
    assert hook.open_request(estimated_tokens=15) is None
    with factory() as session:
        service = ExperimentService(session)
        with pytest.raises(ExperimentLimitError) as refusal:
            service.reserve(experiment_id=experiment_id, request_key=uuid4(), tokens=15)
        assert "one such request at a time" in str(refusal.value)
    with factory() as session:
        summary = ExperimentService(session).summary(experiment_id)
        assert summary["usage"]["dispatched_unsettled"] == 1
        assert summary["usage"]["accounting_complete"] is False


# -- the five-role gateway assembly -------------------------------------------------------
#
# The classes above show that one gateway honours the opt-in hook. These cases show that the
# assembly entry gives all five model-calling roles the *same* hook, so the roles draw on one
# experiment ledger instead of five independent ones. The model is still a stub and the usage
# numbers are whatever the stub reports; nothing here measures a provider.

ROLE_ORDER = (AgentRole.SUPERVISOR, AgentRole.PLANNER, AgentRole.EXPLORER,
              AgentRole.CODER, AgentRole.VERIFIER)


def _role_executions(factory, run_id):
    """One RUNNING execution per model-calling role, all inside the same Run."""
    ids = {}
    with factory() as session:
        for role in ROLE_ORDER:
            execution = AgentExecution(
                run_id=run_id, agent_role=role.value.lower(),
                status=AgentExecutionStatus.RUNNING, input={}, output={})
            session.add(execution)
            session.flush()
            ids[role] = execution.id
        session.commit()
    return ids


@contextmanager
def _assembly(factory, hook, calls, *, usage=(10, 5), max_output=10,
              worker_id="budget-worker", raise_error=None):
    def generate(prompt, max_output_tokens):
        calls.append((prompt, max_output_tokens))
        if raise_error is not None:
            raise raise_error
        if usage is None:
            return ModelReply(output="reply", usage=None)
        return ModelReply(output="reply",
                          usage=ModelTokenUsage(input_tokens=usage[0], output_tokens=usage[1]))

    with model_gateways_from_env(factory, worker_id, generate=generate,
                                 max_output_tokens=max_output,
                                 experiment_budget=hook) as gateways:
        yield gateways


def _ask(gateways, role, run_id, execution_id, prompt="x"):
    """Send one request through the role's own gateway."""
    if role is AgentRole.PLANNER:
        return gateways.planner.initial(run_id, agent_execution_id=execution_id, agent_input={})
    return getattr(gateways, role.name.lower()).request(
        run_id, agent_execution_id=execution_id, prompt=prompt)


def test_the_assembly_gives_all_five_roles_one_experiment_hook(budget_session_factory):
    factory = budget_session_factory
    task_id, _run_id, _execution_id = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id)
    hook = _hook(factory, experiment_id)
    calls = []

    with _assembly(factory, hook, calls) as gateways:
        for role in ("supervisor", "explorer", "planner", "coder", "verifier"):
            assert getattr(gateways, role).experiment_budget is hook
    # Without a hook the assembly is unchanged V1: no role holds an experiment layer.
    with _assembly(factory, None, calls) as gateways:
        for role in ("supervisor", "explorer", "planner", "coder", "verifier"):
            assert getattr(gateways, role).experiment_budget is None
    # A mis-shaped hook is a configuration error, not a silent V1 fallback.
    with pytest.raises(ValueError, match="experiment_budget must provide"):
        with _assembly(factory, object(), calls):
            pass


def test_every_role_in_the_assembly_spends_the_same_experiment_budget(budget_session_factory):
    factory = budget_session_factory
    task_id, run_id, _execution_id = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id, tokens=1000)
    executions = _role_executions(factory, run_id)
    calls = []

    with _assembly(factory, _hook(factory, experiment_id), calls) as gateways:
        for role, execution_id in executions.items():
            assert _ask(gateways, role, run_id, execution_id) == "reply"

    reservations = _reservations(factory, experiment_id)
    assert len(reservations) == len(ROLE_ORDER) == len(calls)
    assert {row.state for row in reservations} == {"settled"}
    # Every request is attributed to the execution that made it, inside the same experiment.
    assert {str(row.agent_execution_id) for row in reservations} == {
        str(value) for value in executions.values()}
    assert {str(row.run_id) for row in reservations} == {str(run_id)}
    assert sum(row.actual_tokens for row in reservations) == 5 * (10 + 5)
    with factory() as session:
        experiment = ExperimentService(session).read(experiment_id)
        assert experiment.settled_tokens == 5 * (10 + 5)
        assert experiment.reserved_tokens == 0


def test_the_ceiling_refuses_the_next_role_and_sends_nothing(budget_session_factory):
    factory = budget_session_factory
    task_id, run_id, _execution_id = _running_attempt(factory)
    # Each request reserves 1 (estimate) + 10 and settles 2; 14 leaves room for exactly two.
    experiment_id = _experiment(factory, task_id, tokens=14)
    executions = _role_executions(factory, run_id)
    calls = []

    with _assembly(factory, _hook(factory, experiment_id), calls, usage=(1, 1)) as gateways:
        assert _ask(gateways, AgentRole.SUPERVISOR, run_id, executions[AgentRole.SUPERVISOR]) == "reply"
        assert _ask(gateways, AgentRole.EXPLORER, run_id, executions[AgentRole.EXPLORER]) == "reply"
        assert _ask(gateways, AgentRole.CODER, run_id, executions[AgentRole.CODER]) is None
        # The refusal is the experiment's, not the Run's: the Run still has budget left.
        assert _ask(gateways, AgentRole.VERIFIER, run_id, executions[AgentRole.VERIFIER]) is None

    assert len(calls) == 2
    assert len(_reservations(factory, experiment_id)) == 2
    assert _run_usage(factory, executions[AgentRole.CODER]) == {}
    assert _run_usage(factory, executions[AgentRole.VERIFIER]) == {}


def test_an_unknown_usage_from_one_role_stops_the_other_roles(budget_session_factory):
    factory = budget_session_factory
    task_id, run_id, _execution_id = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id, tokens=1000)
    executions = _role_executions(factory, run_id)
    calls = []

    with _assembly(factory, _hook(factory, experiment_id), calls, usage=None) as gateways:
        assert _ask(gateways, AgentRole.EXPLORER, run_id, executions[AgentRole.EXPLORER]) == "reply"
        for role in (AgentRole.SUPERVISOR, AgentRole.PLANNER, AgentRole.CODER,
                     AgentRole.VERIFIER):
            assert _ask(gateways, role, run_id, executions[role]) is None

    assert len(calls) == 1
    reservations = _reservations(factory, experiment_id)
    assert [row.state for row in reservations] == ["unknown"]
    assert reservations[0].actual_tokens is None
    # The Run side keeps the reservation with no measured usage.
    entry = next(iter(_run_usage(factory, executions[AgentRole.EXPLORER]).values()))
    assert entry["actual_usage"] is None
    with factory() as session:
        assert ExperimentService(session).summary(experiment_id)["usage"]["accounting_complete"] is False


def test_a_failed_experiment_settlement_keeps_the_other_roles_out(
        budget_session_factory, monkeypatch):
    factory = budget_session_factory
    task_id, run_id, _execution_id = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id, tokens=1000)
    executions = _role_executions(factory, run_id)
    calls = []

    def failing_settle(self, **kwargs):
        raise RuntimeError("settlement unavailable")

    monkeypatch.setattr(ExperimentService, "settle", failing_settle)
    with _assembly(factory, _hook(factory, experiment_id), calls) as gateways:
        with pytest.raises(RuntimeError, match="settlement unavailable"):
            _ask(gateways, AgentRole.SUPERVISOR, run_id, executions[AgentRole.SUPERVISOR])
        assert _ask(gateways, AgentRole.EXPLORER, run_id, executions[AgentRole.EXPLORER]) is None

    # The call may have happened and was never settled: the request stays dispatched and blocks.
    assert len(calls) == 1
    with factory() as session:
        summary = ExperimentService(session).summary(experiment_id)
    assert summary["usage"]["dispatched_unsettled"] == 1
    assert summary["usage"]["accounting_complete"] is False
    entry = next(iter(_run_usage(factory, executions[AgentRole.SUPERVISOR]).values()))
    assert entry["actual_usage"] is None


def test_only_one_role_passes_dispatch_and_the_loser_keeps_its_run_reservation(
        budget_session_factory):
    factory = budget_session_factory
    task_id, run_id, _execution_id = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id, tokens=1000)
    executions = _role_executions(factory, run_id)
    calls = []
    barrier = Barrier(2)

    class RacingHook:
        """Let both roles reserve, then make them race for the single dispatch slot."""

        def __init__(self, hook):
            self.hook = hook

        def open_request(self, **kwargs):
            return self.hook.open_request(**kwargs)

        def mark_dispatched(self, key):
            barrier.wait(timeout=30)
            return self.hook.mark_dispatched(key)

        def settle(self, *args, **kwargs):
            return self.hook.settle(*args, **kwargs)

        def mark_unknown(self, *args, **kwargs):
            return self.hook.mark_unknown(*args, **kwargs)

        def release(self, *args, **kwargs):
            return self.hook.release(*args, **kwargs)

    hook = RacingHook(_hook(factory, experiment_id))
    roles = (AgentRole.SUPERVISOR, AgentRole.EXPLORER)
    with _assembly(factory, hook, calls) as gateways:
        def send(role):
            return _ask(gateways, role, run_id, executions[role])

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = dict(zip(roles, pool.map(send, roles)))

    assert sorted(str(value) for value in results.values()) == ["None", "reply"]
    assert len(calls) == 1
    winner = next(role for role, value in results.items() if value == "reply")
    loser = next(role for role, value in results.items() if value is None)
    reservations = _reservations(factory, experiment_id)
    assert sorted(row.state for row in reservations) == ["released", "settled"]
    # The loser never sent anything, so its experiment reservation went back...
    assert next(row for row in reservations if row.state == "released").actual_tokens is None
    # ...but its Run reservation stays as a conservative hold, with no measured usage.
    entry = next(iter(_run_usage(factory, executions[loser]).values()))
    assert entry["actual_usage"] is None
    assert _run_usage(factory, executions[winner]) != {}
    with factory() as session:
        assert ExperimentService(session).summary(experiment_id)["usage"]["dispatched_unsettled"] == 0


def test_the_assembly_cannot_bypass_the_run_execution_right(budget_session_factory):
    factory = budget_session_factory
    task_id, run_id, _execution_id = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id, tokens=1000)
    executions = _role_executions(factory, run_id)
    calls = []

    # A gateway assembled for another worker cannot spend this Run's budget, hook or no hook.
    with _assembly(factory, _hook(factory, experiment_id), calls,
                   worker_id="another-worker") as gateways:
        with pytest.raises(RunExecutionDeniedError):
            _ask(gateways, AgentRole.SUPERVISOR, run_id, executions[AgentRole.SUPERVISOR])

    assert calls == []
    reservations = _reservations(factory, experiment_id)
    assert [row.state for row in reservations] == ["released"]
    assert _run_usage(factory, executions[AgentRole.SUPERVISOR]) == {}


def test_the_assembly_still_respects_the_run_policy_budget(budget_session_factory):
    factory = budget_session_factory
    task_id, run_id, _execution_id = _running_attempt(factory)
    _set_run_token_budget(factory, task_id, 1)
    experiment_id = _experiment(factory, task_id, tokens=1000)
    executions = _role_executions(factory, run_id)
    calls = []

    with _assembly(factory, _hook(factory, experiment_id), calls) as gateways:
        assert _ask(gateways, AgentRole.SUPERVISOR, run_id, executions[AgentRole.SUPERVISOR]) is None

    assert calls == []
    # The outer reservation is given back because the Run layer refused before any dispatch.
    reservations = _reservations(factory, experiment_id)
    assert [row.state for row in reservations] == ["released"]
    with factory() as session:
        experiment = ExperimentService(session).read(experiment_id)
        assert experiment.settled_tokens == 0 and experiment.reserved_tokens == 0


def test_a_reliable_settlement_lets_the_next_role_through(budget_session_factory):
    factory = budget_session_factory
    task_id, run_id, _execution_id = _running_attempt(factory)
    experiment_id = _experiment(factory, task_id, tokens=1000)
    executions = _role_executions(factory, run_id)
    calls = []

    with _assembly(factory, _hook(factory, experiment_id), calls) as gateways:
        for role in (AgentRole.SUPERVISOR, AgentRole.EXPLORER, AgentRole.CODER):
            assert _ask(gateways, role, run_id, executions[role]) == "reply"

    assert len(calls) == 3
    assert [row.state for row in _reservations(factory, experiment_id)] == [
        "settled", "settled", "settled"]
    with factory() as session:
        summary = ExperimentService(session).summary(experiment_id)
    assert summary["usage"]["dispatched_unsettled"] == 0
    assert summary["usage"]["accounting_complete"] is True
