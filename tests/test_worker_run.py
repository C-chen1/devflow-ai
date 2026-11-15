"""The bounded worker entry: it claims one *named* Task's Run and drives the stages.

These tests use the real PostgreSQL schema (isolated per test by the shared fixtures) and
model stubs. No test calls a provider: the gateway callable is injected, so every token
counted here is a fixture value, not a real request.
"""
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

import devflow_ai.worker_run as worker_run_module
from devflow_ai.benchmark_run import BenchmarkLimits
from devflow_ai.config import HeartbeatConfig
from devflow_ai.coordinator import ModelTokenUsage, RunCoordinator, RunExecutionDeniedError
from devflow_ai.enums import AgentExecutionStatus, AgentRole, RunStatus, TaskStatus
from devflow_ai.model_gateway import ModelGateway, ModelReply, PlannerModelGateway
from devflow_ai.model_gateways import ModelGateways, model_gateways_from_env
from devflow_ai.models import AgentExecution, Run, Task, Verification
from devflow_ai.policy import build_default_policy
from devflow_ai.services import AdmissionRecord, TaskService
from devflow_ai.worker import Worker
from devflow_ai.worker_run import (
    WorkerRunError, WorkerRunExecutionError, WorkerRunRequest, main as worker_run_main,
    prepare_worker_run, run_worker_once,
)

HEARTBEAT = HeartbeatConfig(statement_timeout_seconds=5, shutdown_timeout_seconds=5)
BLOCKED_DECISION = {
    "outcome": "BLOCKED",
    "reason": "The Issue needs a decision before exploration can start",
    "decision_required": "Name the affected module",
}
EXPLORE_DECISION = {
    "outcome": "EXPLORE",
    "reason": "The Issue needs repository evidence",
    "exploration_focus": "Locate the affected facts",
}


def _pack(tmp_path, *, base_commit, target="scenarios::test_placeholder", write_tests=True):
    """A minimal but real task pack: the public check needs an actual test file."""
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir(exist_ok=True)
    if write_tests:
        (pack_dir / "scenarios.py").write_text(
            "def test_placeholder():\n    assert True\n", encoding="utf-8")
    manifest = {
        "pack_id": "worker-entry-pack", "schema_version": 1, "title": "Worker entry pack",
        "source": {"issue_url": "https://example.invalid/issues/1",
                   "repository_url": "https://example.invalid/repo",
                   "license": "MIT", "issue_reference": "demo#1"},
        "base_commit": base_commit,
        "environment": {"python_requires": ">=3.11", "dependencies": ["pytest"],
                        "platform_notes": "CI"},
        "issue": {"sanitized_problem": "A sanitized problem.",
                  "observable_error": "An error.",
                  "expected_behavior": "The expected behaviour."},
        "scope": {"allowed_paths": ["src/"]},
        "public_checks": {"command": ["python", "-m", "pytest"], "command_name": "pytest",
                          "target_tests": [target]},
        "baseline": {"status": "verified_here"},
        "external_connections": "none",
        "independent_suite": None,
        "unverified": [
            {"field": "policy", "reason": "Not pinned for this fixture."},
            {"field": "independent_suite", "reason": "Held by the trusted side."},
        ],
    }
    (pack_dir / "task.json").write_text(json.dumps(manifest), encoding="utf-8")
    return pack_dir


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          check=True, timeout=15, text=True).stdout.strip()


def _repo(tmp_path, monkeypatch):
    """A real repository: the Run's workspace is a real clone checked out at its commit."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    repo = tmp_path / "source-repo"
    repo.mkdir()
    (repo / "README.md").write_bytes(b"Repository facts\n")
    _git(repo, "init", "--quiet")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "initial")
    return repo, _git(repo, "rev-parse", "HEAD")


def _policy():
    policy = build_default_policy()
    policy.policy_version = "worker-run-test-v1"
    policy.permissions.allowed_tools_by_role[AgentRole.EXPLORER] = ["read_file"]
    return policy


def _task(session_factory, repo, base_commit, *, admit=True, policy=None):
    policy = policy if policy is not None else _policy()
    with session_factory() as session:
        task, created = TaskService(session, policy_factory=lambda: policy).create_task(
            title="Drive one named Task", description="The worker entry drives this Run",
            repo=str(repo), idempotency_key=uuid4())
        assert created
        task_id = task.id
    if admit:
        with session_factory() as session:
            TaskService(session).apply_admission_decision(
                task_id, TaskStatus.ELIGIBLE, base_commit=base_commit,
                admission_record=AdmissionRecord(reason="test decision",
                                                 evidence=["test fixture"]))
    return task_id


def _policy_file(tmp_path, session_factory, task_id, **overrides):
    with session_factory() as session:
        snapshot = dict(session.scalar(select(Task.policy_snapshot).where(Task.id == task_id)))
    snapshot.update(overrides)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    return path


class _Setup:
    """One admitted Task with a matching pack and the Task's own Policy on disk."""

    def __init__(self, task_id, pack, policy_path, base_commit):
        self.task_id = task_id
        self.pack = pack
        self.policy_path = policy_path
        self.base_commit = base_commit


def _setup(session_factory, tmp_path, monkeypatch, *, admit=True, pack_commit=None,
           pack_tests=True, policy_overrides=None):
    repo, head = _repo(tmp_path, monkeypatch)
    pack = _pack(tmp_path, base_commit=pack_commit or head, write_tests=pack_tests)
    task_id = _task(session_factory, repo, head, admit=admit)
    policy_path = _policy_file(tmp_path, session_factory, task_id, **(policy_overrides or {}))
    return _Setup(task_id, pack, policy_path, head)


def _request(setup, tmp_path, *, attempt_kind="rehearsal", **over):
    for name in ("workspaces", "artifacts"):
        (tmp_path / name).mkdir(exist_ok=True)
    fields = dict(
        pack_dir=setup.pack, policy_path=setup.policy_path, task_id=setup.task_id,
        operator="operator-a", python_executable=sys.executable,
        workspaces_root=tmp_path / "workspaces", artifacts_root=tmp_path / "artifacts",
        attempt_kind=attempt_kind)
    fields.update(over)
    return WorkerRunRequest(**fields)


@contextmanager
def _gateways(session_factory, worker_id, *, output=None, error=None):
    def generate(prompt, limit):
        if error is not None:
            raise error
        return ModelReply(output, ModelTokenUsage(input_tokens=40, output_tokens=20))

    with model_gateways_from_env(session_factory, worker_id, generate=generate) as gateways:
        yield gateways


def _scripted_gateways(session_factory, worker_id, *, supervisor, explorer):
    """Role-specific stubs: an Explorer stage needs its own script and its own read."""
    def gateway(generate):
        return ModelGateway(session_factory=session_factory, worker_id=worker_id,
                            estimate_input_tokens=lambda _: 100, generate=generate,
                            max_output_tokens=100)

    def unused(*_args):
        pytest.fail("this role is not part of the stage under test")

    return ModelGateways(
        supervisor=gateway(supervisor), explorer=gateway(explorer),
        planner=PlannerModelGateway(session_factory=session_factory, worker_id=worker_id,
                                    estimate_input_tokens=lambda _: 100, generate=unused,
                                    max_output_tokens=100),
        coder=gateway(unused), verifier=gateway(unused))


def _explorer_after_read(*, inside_last_call=None):
    """Read the README, then summarize; ``inside_last_call`` runs in the final model call."""
    calls = []

    def explore(prompt, limit):
        calls.append(prompt)
        if len(calls) == 1:
            return ModelReply({"path": "README.md"}, ModelTokenUsage(input_tokens=40, output_tokens=20))
        if inside_last_call is not None:
            inside_last_call()
        data = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
        return ModelReply(
            {"summary": "Read repository facts",
             "read_evidence": {"path": "README.md",
                               "tool_call_id": data["read"]["tool_call_id"]}},
            ModelTokenUsage(input_tokens=40, output_tokens=20))

    return explore


def _runs(session_factory, task_id):
    with session_factory() as session:
        return session.scalars(select(Run).where(Run.task_id == task_id)).all()


def _executions(session_factory, run_id):
    with session_factory() as session:
        return session.execute(select(
            AgentExecution.agent_role, AgentExecution.status, AgentExecution.output,
        ).where(AgentExecution.run_id == run_id)
            .order_by(AgentExecution.created_at, AgentExecution.id)).all()


# --- checks that run before anything is claimed -------------------------------------

def test_a_pack_that_cannot_supply_the_public_tests_is_refused(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    setup = _setup(factory, tmp_path, monkeypatch, pack_tests=False)

    with pytest.raises(WorkerRunError, match="public tests"):
        prepare_worker_run(_request(setup, tmp_path), session_factory=factory)

    assert _runs(factory, setup.task_id) == []


def test_a_task_that_is_not_eligible_is_refused_before_a_run_exists(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    setup = _setup(factory, tmp_path, monkeypatch, admit=False)

    with pytest.raises(WorkerRunError, match="PENDING"):
        prepare_worker_run(_request(setup, tmp_path), session_factory=factory)

    assert _runs(factory, setup.task_id) == []


def test_a_pack_for_another_commit_is_refused_before_a_run_exists(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    setup = _setup(factory, tmp_path, monkeypatch, pack_commit="b" * 40)

    with pytest.raises(WorkerRunError, match="base commit"):
        prepare_worker_run(_request(setup, tmp_path), session_factory=factory)

    assert _runs(factory, setup.task_id) == []


def test_a_policy_file_that_differs_from_the_pinned_snapshot_is_refused(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    setup = _setup(factory, tmp_path, monkeypatch)
    payload = json.loads(setup.policy_path.read_text(encoding="utf-8"))
    payload["budget"]["max_tool_calls_per_run"] = 59
    setup.policy_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(WorkerRunError, match="differs from the prepared one"):
        prepare_worker_run(_request(setup, tmp_path), session_factory=factory)

    assert _runs(factory, setup.task_id) == []


def test_a_task_that_already_started_is_refused_rather_than_claimed_again(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    setup = _setup(factory, tmp_path, monkeypatch)
    with factory() as session:
        run, created = RunCoordinator(session).start_initial_run(
            setup.task_id, "someone-else", f"other:{setup.task_id}:1")
        assert created and run.status is RunStatus.RUNNING

    with pytest.raises(WorkerRunError, match="RUNNING"):
        prepare_worker_run(_request(setup, tmp_path), session_factory=factory)

    assert len(_runs(factory, setup.task_id)) == 1


def test_a_formal_attempt_is_refused_where_the_trusted_baseline_cannot_be_recorded(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    setup = _setup(factory, tmp_path, monkeypatch)
    monkeypatch.setattr(worker_run_module, "trusted_baseline_platform_supported",
                        lambda: False)

    with pytest.raises(WorkerRunError, match="Windows Job"):
        prepare_worker_run(_request(setup, tmp_path, attempt_kind="formal"),
                           session_factory=factory)

    assert _runs(factory, setup.task_id) == []


def test_a_rehearsal_is_allowed_on_a_platform_without_the_trusted_baseline(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    setup = _setup(factory, tmp_path, monkeypatch)
    monkeypatch.setattr(worker_run_module, "trusted_baseline_platform_supported",
                        lambda: False)

    prepared = prepare_worker_run(_request(setup, tmp_path), session_factory=factory)

    assert prepared.trusted_baseline_supported is False
    assert prepared.claim_key == f"worker:{setup.task_id}:1"


def test_the_policy_budget_may_not_exceed_the_callers_limit(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    setup = _setup(factory, tmp_path, monkeypatch)

    with pytest.raises(WorkerRunError, match="more tokens"):
        prepare_worker_run(
            _request(setup, tmp_path, limits=BenchmarkLimits(max_model_tokens=1000)),
            session_factory=factory)


def test_a_relative_interpreter_is_refused(worker_session_factory, tmp_path, monkeypatch):
    factory = worker_session_factory
    setup = _setup(factory, tmp_path, monkeypatch)

    with pytest.raises(WorkerRunError, match="absolute path"):
        prepare_worker_run(_request(setup, tmp_path, python_executable="python"),
                           session_factory=factory)


def test_a_missing_workspaces_root_is_refused(worker_session_factory, tmp_path, monkeypatch):
    factory = worker_session_factory
    setup = _setup(factory, tmp_path, monkeypatch)

    with pytest.raises(WorkerRunError, match="workspaces-root"):
        prepare_worker_run(_request(setup, tmp_path, workspaces_root=tmp_path / "absent"),
                           session_factory=factory)


# --- one real claim, one real stage --------------------------------------------------

def test_one_call_claims_the_task_and_drives_the_first_supervisor_stage(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    setup = _setup(factory, tmp_path, monkeypatch)
    worker = Worker(factory, "worker-run-entry")
    with _gateways(factory, worker.worker_id, output=BLOCKED_DECISION) as gateways:
        result = run_worker_once(
            _request(setup, tmp_path), session_factory=factory, worker=worker,
            gateways=gateways, heartbeat_config=HEARTBEAT)

    assert result["claimed"] is True
    assert result["outcome"] == "stopped"
    assert result["reason"] == "supervisor_did_not_route"
    assert result["stages"] == 1
    # The driver does not end the Run: it keeps the state the database recorded.
    assert result["run_status"] == RunStatus.RUNNING.value
    assert result["writes_record"] is False
    assert result["formal_acceptance"] == "not_performed"
    assert result["attempt_kind"] == "rehearsal"

    runs = _runs(factory, setup.task_id)
    assert len(runs) == 1 and runs[0].worker_id == worker.worker_id
    executions = _executions(factory, runs[0].id)
    assert [(role, status) for role, status, _ in executions] == [
        (AgentRole.SUPERVISOR.value.lower(), AgentExecutionStatus.SUCCEEDED)]
    assert dict(executions[0][2])["outcome"] == "BLOCKED"
    with factory() as session:
        assert session.get(Task, setup.task_id).status is TaskStatus.RUNNING
        assert session.scalars(select(Verification).where(
            Verification.run_id == runs[0].id)).all() == []


def test_the_claim_is_bound_to_the_calling_worker(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    setup = _setup(factory, tmp_path, monkeypatch)
    worker = Worker(factory, "worker-run-entry")
    with _gateways(factory, worker.worker_id, output=BLOCKED_DECISION) as gateways:
        result = run_worker_once(
            _request(setup, tmp_path), session_factory=factory, worker=worker,
            gateways=gateways, heartbeat_config=HEARTBEAT)
    run_id = UUID(result["run_id"])

    with factory() as session, session.begin():
        RunCoordinator(session).assert_execution_right(run_id, worker.worker_id)
        with pytest.raises(RunExecutionDeniedError):
            RunCoordinator(session).assert_execution_right(run_id, "another-worker")


def test_a_repeated_call_reports_the_existing_run_without_executing_again(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    setup = _setup(factory, tmp_path, monkeypatch)
    worker = Worker(factory, "worker-run-entry")
    request = _request(setup, tmp_path)
    prepared = prepare_worker_run(request, session_factory=factory)
    with _gateways(factory, worker.worker_id, output=BLOCKED_DECISION) as gateways:
        first = run_worker_once(
            request, session_factory=factory, worker=worker, gateways=gateways,
            heartbeat_config=HEARTBEAT, prepared=prepared)
        second = run_worker_once(
            request, session_factory=factory, worker=worker, gateways=gateways,
            heartbeat_config=HEARTBEAT, prepared=prepared)

    assert first["claimed"] is True
    assert second["outcome"] == "existing_run" and second["claimed"] is False
    assert second["run_id"] == first["run_id"]
    assert len(_runs(factory, setup.task_id)) == 1
    assert len(_executions(factory, first["run_id"])) == 1


def test_a_caller_that_already_stopped_claims_nothing(
    worker_session_factory, tmp_path, monkeypatch,
):
    """A stop raised before the call must not leave a claimed Run behind."""
    factory = worker_session_factory
    setup = _setup(factory, tmp_path, monkeypatch)
    worker = Worker(factory, "worker-run-entry")
    stop = Event()
    stop.set()
    with _gateways(factory, worker.worker_id, output=BLOCKED_DECISION) as gateways:
        result = run_worker_once(
            _request(setup, tmp_path), session_factory=factory, worker=worker,
            gateways=gateways, heartbeat_config=HEARTBEAT, stop_event=stop)

    assert result["outcome"] == "stopped" and result["reason"] == "stop_requested"
    assert result["claimed"] is False and result["run_id"] is None
    assert result["stop_requested"] is True and result["stages"] == 0
    assert _runs(factory, setup.task_id) == []
    with factory() as session:
        assert session.get(Task, setup.task_id).status is TaskStatus.ELIGIBLE


def test_a_stop_raised_inside_a_stage_reaches_the_driver(
    worker_session_factory, tmp_path, monkeypatch,
):
    """The stop must be visible to the stage loop, not only before it starts."""
    factory = worker_session_factory
    setup = _setup(factory, tmp_path, monkeypatch)
    worker = Worker(factory, "worker-run-entry")
    stop = Event()
    gateways = _scripted_gateways(
        factory, worker.worker_id,
        supervisor=lambda *_args: ModelReply(EXPLORE_DECISION, ModelTokenUsage(input_tokens=40, output_tokens=20)),
        explorer=_explorer_after_read(inside_last_call=stop.set))

    result = run_worker_once(
        _request(setup, tmp_path), session_factory=factory, worker=worker,
        gateways=gateways, heartbeat_config=HEARTBEAT, stop_event=stop)

    # The Explorer stage really ran, and the drive ended there: no second Supervisor was
    # consulted. Without a stop the scripted EXPLORE would have routed on.
    roles = [role for role, _, _ in _executions(factory, result["run_id"])]
    assert roles == [AgentRole.SUPERVISOR.value.lower(),
                     AgentRole.EXPLORER.value.lower()], result
    assert result["interrupted_mid_stage"] is True
    assert result["outcome"] == "stopped" and result["reason"] == "stop_requested"
    assert result["stages"] is None  # interrupted mid-stage, so no stage count is claimed
    assert result["stop_requested"] is True and result["deadline_fired"] is False
    assert result["run_status"] == RunStatus.RUNNING.value


class _ManualTimer:
    """A timer this test fires itself, so the deadline lands inside a stage on purpose.

    Waiting for a real one-second timer would race the workspace initialization: the
    deadline could expire before any stage started, which is the case already covered
    elsewhere. Firing it from inside the second model call tests exactly the wiring in
    question — deadline callback to stop event to stage driver.
    """

    started = []

    def __init__(self, interval, function):
        self.interval = interval
        self.function = function
        self.cancelled = False
        _ManualTimer.started.append(self)

    def start(self):
        return None

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.function()


def test_a_deadline_raised_inside_a_stage_stops_the_driver(
    worker_session_factory, tmp_path, monkeypatch,
):
    """The attempt deadline must reach the stages too, not just the initialization."""
    _ManualTimer.started = []
    monkeypatch.setattr(worker_run_module, "Timer", _ManualTimer)
    factory = worker_session_factory
    setup = _setup(factory, tmp_path, monkeypatch)
    worker = Worker(factory, "worker-run-entry")
    gateways = _scripted_gateways(
        factory, worker.worker_id,
        supervisor=lambda *_args: ModelReply(EXPLORE_DECISION, ModelTokenUsage(input_tokens=40, output_tokens=20)),
        explorer=_explorer_after_read(
            inside_last_call=lambda: _ManualTimer.started[0].fire()))

    result = run_worker_once(
        _request(setup, tmp_path), session_factory=factory, worker=worker,
        gateways=gateways, heartbeat_config=HEARTBEAT)

    # The deadline expired while a stage was running, not during initialization.
    assert [role for role, _, _ in _executions(factory, result["run_id"])] == [
        AgentRole.SUPERVISOR.value.lower(), AgentRole.EXPLORER.value.lower()]
    assert result["interrupted_mid_stage"] is True
    assert result["deadline_fired"] is True
    assert result["outcome"] == "stopped" and result["reason"] == "deadline_reached"
    assert result["run_status"] == RunStatus.RUNNING.value
    # The attempt's timer is released even when the deadline fired.
    assert _ManualTimer.started[0].cancelled is True


def test_a_task_changed_after_preparation_is_refused_at_the_claim(
    worker_session_factory, tmp_path, monkeypatch,
):
    """The commit is re-checked under the Task row lock, not just during preparation."""
    factory = worker_session_factory
    setup = _setup(factory, tmp_path, monkeypatch)
    request = _request(setup, tmp_path)
    prepared = prepare_worker_run(request, session_factory=factory)
    with factory() as session, session.begin():
        # Stands in for a concurrent edit between preparation and the claim.
        session.get(Task, setup.task_id).base_commit = "b" * 40

    with _gateways(factory, "worker-run-entry", output=BLOCKED_DECISION) as gateways:
        with pytest.raises(WorkerRunError, match="base commit changed"):
            run_worker_once(
                request, session_factory=factory, worker=Worker(factory, "worker-run-entry"),
                gateways=gateways, heartbeat_config=HEARTBEAT, prepared=prepared)

    assert _runs(factory, setup.task_id) == []


def test_a_policy_changed_after_preparation_is_refused_at_the_claim(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    setup = _setup(factory, tmp_path, monkeypatch)
    request = _request(setup, tmp_path)
    prepared = prepare_worker_run(request, session_factory=factory)
    with factory() as session, session.begin():
        task = session.get(Task, setup.task_id)
        snapshot = dict(task.policy_snapshot)
        snapshot["budget"] = {**snapshot["budget"], "max_tool_calls_per_run": 59}
        task.policy_snapshot = snapshot

    with _gateways(factory, "worker-run-entry", output=BLOCKED_DECISION) as gateways:
        with pytest.raises(WorkerRunError, match="Policy snapshot changed"):
            run_worker_once(
                request, session_factory=factory, worker=Worker(factory, "worker-run-entry"),
                gateways=gateways, heartbeat_config=HEARTBEAT, prepared=prepared)

    assert _runs(factory, setup.task_id) == []


def test_two_calls_at_once_create_one_run(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    setup = _setup(factory, tmp_path, monkeypatch)
    request = _request(setup, tmp_path)
    prepared = prepare_worker_run(request, session_factory=factory)
    barrier = Barrier(2, timeout=15)
    results = []

    def call(worker_id):
        worker = Worker(factory, worker_id)
        barrier.wait()
        with _gateways(factory, worker.worker_id, output=BLOCKED_DECISION) as gateways:
            try:
                results.append(run_worker_once(
                    request, session_factory=factory, worker=worker, gateways=gateways,
                    heartbeat_config=HEARTBEAT, prepared=prepared))
            except WorkerRunError as error:
                results.append({"outcome": "refused", "detail": str(error), "claimed": False})

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(call, ("worker-run-a", "worker-run-b")))

    assert len(_runs(factory, setup.task_id)) == 1
    assert sum(1 for result in results if result["claimed"]) == 1
    assert all(result["outcome"] in ("stopped", "existing_run", "refused")
               for result in results)


def test_a_failing_stage_leaves_the_run_as_it_was(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    setup = _setup(factory, tmp_path, monkeypatch)
    worker = Worker(factory, "worker-run-entry")
    with _gateways(factory, worker.worker_id,
                   error=RuntimeError("provider unavailable")) as gateways:
        with pytest.raises(WorkerRunExecutionError) as raised:
            run_worker_once(
                _request(setup, tmp_path), session_factory=factory, worker=worker,
                gateways=gateways, heartbeat_config=HEARTBEAT)

    runs = _runs(factory, setup.task_id)
    assert len(runs) == 1 and runs[0].status is RunStatus.RUNNING
    assert raised.value.run_id == runs[0].id
    # The provider error is what stopped the call; it is not re-labelled as a verdict.
    assert isinstance(raised.value.error, RuntimeError)
    with factory() as session:
        assert session.scalars(select(Verification).where(
            Verification.run_id == runs[0].id)).all() == []


# --- the CLI boundary ---------------------------------------------------------------

def _cli_args(setup, tmp_path, *, extra=()):
    for name in ("workspaces", "artifacts"):
        (tmp_path / name).mkdir(exist_ok=True)
    return [
        "--pack", str(setup.pack), "--task-id", str(setup.task_id),
        "--policy", str(setup.policy_path), "--operator", "operator-a",
        "--python-executable", sys.executable,
        "--workspaces-root", str(tmp_path / "workspaces"),
        "--artifacts-root", str(tmp_path / "artifacts"),
        "--attempt-kind", "rehearsal", "--worker-id", "worker-run-cli", *extra,
    ]


def test_the_cli_needs_explicit_confirmation(worker_session_factory, tmp_path, monkeypatch,
                                             capsys):
    factory = worker_session_factory
    setup = _setup(factory, tmp_path, monkeypatch)

    exit_code = worker_run_main(_cli_args(setup, tmp_path), session_factory=factory,
                                gateways_factory=_gateways)

    assert exit_code == 2
    assert "confirm-run" in capsys.readouterr().err
    assert _runs(factory, setup.task_id) == []


def test_the_cli_runs_one_rehearsal_and_reports_where_it_stopped(
    worker_session_factory, tmp_path, monkeypatch, capsys,
):
    factory = worker_session_factory
    setup = _setup(factory, tmp_path, monkeypatch)

    @contextmanager
    def gateways(session_factory, worker_id):
        with _gateways(session_factory, worker_id, output=BLOCKED_DECISION) as built:
            yield built

    exit_code = worker_run_main(_cli_args(setup, tmp_path, extra=["--confirm-run"]),
                                session_factory=factory, gateways_factory=gateways)

    assert exit_code == 0
    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert event["event"] == "worker_run_finished"
    assert event["claimed"] is True and event["writes_record"] is False
    assert event["attempt_kind"] == "rehearsal"
    assert event["reason"] == "supervisor_did_not_route"
    assert len(_runs(factory, setup.task_id)) == 1


def test_the_cli_reports_the_existing_run_on_a_second_invocation(
    worker_session_factory, tmp_path, monkeypatch, capsys,
):
    """The command line must answer the same way the API path does: no second attempt."""
    factory = worker_session_factory
    setup = _setup(factory, tmp_path, monkeypatch)
    args = _cli_args(setup, tmp_path, extra=["--confirm-run"])

    @contextmanager
    def gateways(session_factory, worker_id):
        with _gateways(session_factory, worker_id, output=BLOCKED_DECISION) as built:
            yield built

    first_exit = worker_run_main(args, session_factory=factory, gateways_factory=gateways)
    first_event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    second_exit = worker_run_main(args, session_factory=factory, gateways_factory=gateways)
    second_event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert (first_exit, second_exit) == (0, 0)
    assert first_event["claimed"] is True
    assert second_event["outcome"] == "existing_run"
    assert second_event["claimed"] is False
    assert second_event["run_id"] == first_event["run_id"]
    assert len(_runs(factory, setup.task_id)) == 1
    assert len(_executions(factory, first_event["run_id"])) == 1


def test_the_cli_reports_an_execution_failure_and_keeps_the_run(
    worker_session_factory, tmp_path, monkeypatch, capsys,
):
    factory = worker_session_factory
    setup = _setup(factory, tmp_path, monkeypatch)

    @contextmanager
    def gateways(session_factory, worker_id):
        with _gateways(session_factory, worker_id,
                       error=RuntimeError("provider unavailable")) as built:
            yield built

    exit_code = worker_run_main(_cli_args(setup, tmp_path, extra=["--confirm-run"]),
                                session_factory=factory, gateways_factory=gateways)

    runs = _runs(factory, setup.task_id)
    assert exit_code == 1
    failure = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert failure["event"] == "worker_run_failed"
    assert failure["error_type"] == "RuntimeError"
    assert failure["run_id"] == str(runs[0].id)
    assert runs[0].status is RunStatus.RUNNING
