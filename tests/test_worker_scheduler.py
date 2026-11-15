"""The resident scheduler: worked through a trusted manifest, nothing else.

Real PostgreSQL schema (isolated per test by the shared fixtures), real git repositories as
the controlled workspace, and model stubs. No test calls a provider.
"""
import json
import subprocess
import sys
from contextlib import contextmanager
from threading import Barrier, Event, Thread
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from devflow_ai.config import HeartbeatConfig
from devflow_ai.coordinator import ModelTokenUsage
from devflow_ai.enums import AgentExecutionStatus, AgentRole, RunStatus, TaskStatus
from devflow_ai.model_gateway import ModelReply
from devflow_ai.model_gateways import model_gateways_from_env
from devflow_ai.models import AgentExecution, Run, Task, Verification
from devflow_ai.policy import build_default_policy
from devflow_ai.services import AdmissionRecord, TaskService
from devflow_ai.worker import Worker
from devflow_ai.worker_run import WorkerRunBusyError
from devflow_ai.worker_scheduler import (
    SchedulerManifestError, SchedulerRequest, load_scheduler_manifest,
    main as scheduler_main, run_scheduler, _wait_for_next_round,
)

HEARTBEAT = HeartbeatConfig(statement_timeout_seconds=5, shutdown_timeout_seconds=5)
BLOCKED_DECISION = {
    "outcome": "BLOCKED",
    "reason": "The Issue needs a decision before exploration can start",
    "decision_required": "Name the affected module",
}


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          check=True, timeout=15, text=True).stdout.strip()


def _repo(directory, monkeypatch):
    """A real repository per registered Task: the Run's workspace is a real clone."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    repo = directory / "source-repo"
    repo.mkdir()
    (repo / "README.md").write_bytes(b"Repository facts\n")
    _git(repo, "init", "--quiet")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "initial")
    return repo, _git(repo, "rev-parse", "HEAD")


def _pack(directory, name, *, base_commit):
    pack_dir = directory / f"pack-{name}"
    pack_dir.mkdir()
    (pack_dir / "scenarios.py").write_text(
        "def test_placeholder():\n    assert True\n", encoding="utf-8")
    manifest = {
        "pack_id": f"scheduler-pack-{name}", "schema_version": 1,
        "title": "Scheduler pack",
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
                          "target_tests": ["scenarios::test_placeholder"]},
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


def _task(session_factory, repo, base_commit):
    policy = build_default_policy()
    policy.policy_version = "worker-scheduler-test-v1"
    policy.permissions.allowed_tools_by_role[AgentRole.EXPLORER] = ["read_file"]
    with session_factory() as session:
        task, created = TaskService(session, policy_factory=lambda: policy).create_task(
            title="Scheduled Task", description="The scheduler drives this Run",
            repo=str(repo), idempotency_key=uuid4())
        assert created
        task_id = task.id
    with session_factory() as session:
        TaskService(session).apply_admission_decision(
            task_id, TaskStatus.ELIGIBLE, base_commit=base_commit,
            admission_record=AdmissionRecord(reason="test decision", evidence=["fixture"]))
    return task_id


def _policy_file(directory, session_factory, task_id, name):
    with session_factory() as session:
        snapshot = dict(session.scalar(select(Task.policy_snapshot).where(Task.id == task_id)))
    path = directory / f"policy-{name}.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    return path


def _manifest(tmp_path, entries, *, name="manifest.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"schema_version": 1, "tasks": [
        {"task_id": str(task_id), "pack": str(pack), "policy": str(policy)}
        for task_id, pack, policy in entries]}), encoding="utf-8")
    return path


class _Registered:
    """One admitted Task with its own repository, pack, Policy file and manifest entry."""

    def __init__(self, tmp_path, session_factory, monkeypatch, name="a"):
        self.directory = tmp_path / name
        self.directory.mkdir()
        self.repo, head = _repo(self.directory, monkeypatch)
        self.head = head
        self.pack = _pack(self.directory, name, base_commit=head)
        self.task_id = _task(session_factory, self.repo, head)
        self.policy = _policy_file(self.directory, session_factory, self.task_id, name)

    def entry(self):
        return (self.task_id, self.pack, self.policy)


def _request(tmp_path, manifest, *, attempt_kind="rehearsal", poll_interval=0.01,
             resident=False):
    for name in ("workspaces", "artifacts"):
        (tmp_path / name).mkdir(exist_ok=True)
    return SchedulerRequest(
        manifest_path=manifest, operator="operator-a", python_executable=sys.executable,
        workspaces_root=tmp_path / "workspaces", artifacts_root=tmp_path / "artifacts",
        attempt_kind=attempt_kind, poll_interval_seconds=poll_interval, resident=resident)


@contextmanager
def _gateways(session_factory, worker_id, *, output=None, error=None):
    def generate(prompt, limit):
        if error is not None:
            raise error
        return ModelReply(output, ModelTokenUsage(input_tokens=40, output_tokens=20))

    with model_gateways_from_env(session_factory, worker_id, generate=generate) as gateways:
        yield gateways


def _runs(session_factory, task_id):
    with session_factory() as session:
        return session.scalars(select(Run).where(Run.task_id == task_id)).all()


def _executions(session_factory, run_id):
    with session_factory() as session:
        return [tuple(row) for row in session.execute(
            select(AgentExecution.agent_role, AgentExecution.status)
            .where(AgentExecution.run_id == run_id)).all()]


def _schedule(request, session_factory, gateways, *, worker_id="scheduler-a",
              stop_event=None, run_once=None, wait=None, prepare=None, report=None):
    return run_scheduler(
        request, session_factory=session_factory, worker_id=worker_id, gateways=gateways,
        heartbeat_config=HEARTBEAT, stop_event=stop_event, run_once=run_once, wait=wait,
        prepare=prepare, report=report)


def test_scheduler_passes_configured_byte_limits_to_worker(tmp_path):
    from dataclasses import replace

    manifest = _manifest(tmp_path, [(uuid4(), tmp_path / "pack", tmp_path / "policy")])
    request = replace(_request(tmp_path, manifest), max_read_bytes=1024,
                      max_patch_bytes=2048, max_diff_bytes=4096)
    captured = {}

    def run_once(_request, **kwargs):
        captured.update(kwargs)
        return {"outcome": "stopped", "reason": "fixture"}

    result = _schedule(request, None, None, prepare=lambda _: object(), run_once=run_once)
    assert result["outcome"] == "idle_exit"
    assert (captured["max_read_bytes"], captured["max_patch_bytes"],
            captured["verifier_max_diff_bytes"]) == (1024, 2048, 4096)


# --- the manifest itself ---------------------------------------------------------------

def test_the_manifest_rejects_unknown_fields_and_duplicate_entries(tmp_path):
    task_id = uuid4()
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"schema_version": 1, "tasks": [
        {"task_id": str(task_id), "pack": "p", "policy": "q", "api_key": "secret"}]}),
        encoding="utf-8")
    with pytest.raises(SchedulerManifestError, match="check: tasks"):
        load_scheduler_manifest(path)

    path.write_text(json.dumps({"schema_version": 1, "tasks": [
        {"task_id": str(task_id), "pack": "p", "policy": "q"},
        {"task_id": str(task_id), "pack": "p", "policy": "q"}]}), encoding="utf-8")
    with pytest.raises(SchedulerManifestError):
        load_scheduler_manifest(path)


def test_an_empty_or_unreadable_manifest_is_refused(tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"schema_version": 1, "tasks": []}), encoding="utf-8")
    with pytest.raises(SchedulerManifestError):
        load_scheduler_manifest(empty)
    with pytest.raises(SchedulerManifestError, match="unreadable"):
        load_scheduler_manifest(tmp_path / "absent.json")


def test_relative_entry_paths_resolve_against_the_manifest(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"schema_version": 1, "tasks": [
        {"task_id": str(uuid4()), "pack": "pack", "policy": "pack/policy.json"}]}),
        encoding="utf-8")

    manifest = load_scheduler_manifest(path)

    assert manifest.tasks[0].pack == str(tmp_path / "pack")
    assert manifest.tasks[0].policy == str(tmp_path / "pack" / "policy.json")


# --- claiming, ordering and the entries that must not block others ----------------------

def test_only_registered_tasks_are_claimed(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    registered = _Registered(tmp_path, factory, monkeypatch, "a")
    other = tmp_path / "unregistered"
    other.mkdir()
    other_repo, other_head = _repo(other, monkeypatch)
    unregistered = _task(factory, other_repo, other_head)
    manifest = _manifest(tmp_path, [registered.entry()])

    with _gateways(factory, "scheduler-a", output=BLOCKED_DECISION) as gateways:
        summary = _schedule(_request(tmp_path, manifest), factory, gateways)

    assert summary["outcome"] == "idle_exit" and summary["stop_requested"] is False
    assert summary["counts"] == {"stopped": 1}
    assert len(_runs(factory, registered.task_id)) == 1
    assert _runs(factory, unregistered) == []


def test_an_unusable_entry_does_not_block_the_next_one(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    broken = _Registered(tmp_path, factory, monkeypatch, "a")
    working = _Registered(tmp_path, factory, monkeypatch, "b")
    # A pack for a different commit makes the first entry unacceptable; it must not stop the
    # second one from being claimed.
    wrong_pack = _pack(broken.directory, "wrong", base_commit="b" * 40)
    manifest = _manifest(tmp_path, [(broken.task_id, wrong_pack, broken.policy),
                                    working.entry()])

    with _gateways(factory, "scheduler-a", output=BLOCKED_DECISION) as gateways:
        summary = _schedule(_request(tmp_path, manifest), factory, gateways)

    entries = {entry["task_id"]: entry for entry in summary["entries"]}
    assert entries[str(broken.task_id)]["outcome"] == "refused"
    assert "base commit" in entries[str(broken.task_id)]["detail"]
    assert entries[str(working.task_id)]["outcome"] == "stopped"
    assert _runs(factory, broken.task_id) == []
    assert len(_runs(factory, working.task_id)) == 1
    assert summary["outcome"] == "idle_exit"


def test_a_locked_task_waits_instead_of_spinning(
    worker_session_factory, tmp_path,
):
    factory = worker_session_factory
    manifest = _manifest(tmp_path, [(uuid4(), tmp_path / "pack", tmp_path / "policy")])
    waits = []
    stop = Event()

    def wait(event, seconds):
        waits.append(seconds)
        if len(waits) == 3:
            event.set()
        return event.is_set()

    def busy(_request, **_kwargs):
        raise WorkerRunBusyError("Task is locked; retry the same creation request")

    with _gateways(factory, "scheduler-a", output=BLOCKED_DECISION) as gateways:
        summary = _schedule(_request(tmp_path, manifest), factory, gateways,
                            stop_event=stop, run_once=busy,
                            prepare=lambda _request: object(), wait=wait)

    assert summary["outcome"] == "stopped" and summary["stop_requested"] is True
    assert len(waits) == 3
    assert summary["rounds"] == 3 and summary["waited_rounds"] == 3
    assert summary["entries"][0]["outcome"] == "waiting"
    assert summary["entries"][0]["state"] == "waiting"


def test_a_stop_before_the_first_round_claims_nothing(
    worker_session_factory, tmp_path,
):
    factory = worker_session_factory
    manifest = _manifest(tmp_path, [(uuid4(), tmp_path / "pack", tmp_path / "policy")])
    stop = Event()
    stop.set()
    calls = []

    with _gateways(factory, "scheduler-a", output=BLOCKED_DECISION) as gateways:
        summary = _schedule(_request(tmp_path, manifest), factory, gateways,
                            stop_event=stop, run_once=lambda *a, **k: calls.append(a))

    assert summary["outcome"] == "stopped"
    assert summary["rounds"] == 0 and calls == []
    assert summary["entries"][0]["state"] == "pending"


def test_the_loop_does_not_claim_the_next_task_after_a_stop(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    first = _Registered(tmp_path, factory, monkeypatch, "a")
    second = _Registered(tmp_path, factory, monkeypatch, "b")
    manifest = _manifest(tmp_path, [first.entry(), second.entry()])
    stop = Event()
    from devflow_ai.worker_run import prepare_worker_run, run_worker_once

    def prepare(request):
        return prepare_worker_run(request, session_factory=factory)

    def run_once(request, **kwargs):
        result = run_worker_once(request, **kwargs)
        stop.set()  # the operator asked to stop while the first Task was running
        return result

    with _gateways(factory, "scheduler-a", output=BLOCKED_DECISION) as gateways:
        summary = _schedule(_request(tmp_path, manifest), factory, gateways,
                            stop_event=stop, run_once=run_once, prepare=prepare)

    assert summary["outcome"] == "stopped"
    assert len(_runs(factory, first.task_id)) == 1
    assert _runs(factory, second.task_id) == []
    assert summary["entries"][1]["state"] == "pending"


# --- restart, competition and losing the execution right -------------------------------

def test_a_restart_reports_the_existing_run_and_executes_nothing(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    registered = _Registered(tmp_path, factory, monkeypatch, "a")
    manifest = _manifest(tmp_path, [registered.entry()])
    request = _request(tmp_path, manifest)

    with _gateways(factory, "scheduler-a", output=BLOCKED_DECISION) as gateways:
        first = _schedule(request, factory, gateways, worker_id="scheduler-a")
        runs_after_first = _runs(factory, registered.task_id)
        executions_after_first = _executions(factory, runs_after_first[0].id)
        second = _schedule(request, factory, gateways, worker_id="scheduler-b")

    assert first["counts"] == {"stopped": 1}
    assert second["counts"] == {"existing_run": 1}
    assert second["entries"][0]["run_id"] == str(runs_after_first[0].id)
    assert second["entries"][0]["run_status"] == RunStatus.RUNNING.value
    assert len(_runs(factory, registered.task_id)) == 1
    assert _executions(factory, runs_after_first[0].id) == executions_after_first


def test_two_schedulers_racing_create_one_run(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    registered = _Registered(tmp_path, factory, monkeypatch, "a")
    manifest = _manifest(tmp_path, [registered.entry()])
    request = _request(tmp_path, manifest)
    barrier = Barrier(2, timeout=20)
    summaries = []

    def schedule(worker_id):
        barrier.wait()
        stop = Event()
        waits = {"count": 0}

        def wait(event, seconds):
            # The instance that lost the claim race would otherwise keep waiting for a Task
            # another worker owns; a resident scheduler is meant to be stopped by its
            # operator, so this test stops it after a bounded number of idle waits.
            waits["count"] += 1
            if waits["count"] >= 40:
                event.set()
            return event.is_set()

        with _gateways(factory, worker_id, output=BLOCKED_DECISION) as gateways:
            summaries.append(_schedule(request, factory, gateways, worker_id=worker_id,
                                       stop_event=stop, wait=wait))

    threads = [Thread(target=schedule, args=(name,)) for name in ("scheduler-a", "scheduler-b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(summaries) == 2
    runs = _runs(factory, registered.task_id)
    assert len(runs) == 1
    claimed = sum(1 for summary in summaries
                  for entry in summary["entries"] if entry["outcome"] == "stopped")
    assert claimed == 1
    assert {entry["outcome"] for summary in summaries for entry in summary["entries"]} <= {
        "stopped", "existing_run", "waiting", "refused", "failed"}


def test_a_lost_execution_right_is_reported_and_left_alone(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    registered = _Registered(tmp_path, factory, monkeypatch, "a")
    manifest = _manifest(tmp_path, [registered.entry()])

    def expire_lease(prompt, limit):
        # Stands in for a lease that ran out (or a takeover) while the stage was running.
        with factory() as session, session.begin():
            run = session.scalars(select(Run).where(
                Run.task_id == registered.task_id)).one()
            run.lease_expires_at = func.clock_timestamp()
        return ModelReply(BLOCKED_DECISION, ModelTokenUsage(input_tokens=40,
                                                            output_tokens=20))

    with model_gateways_from_env(factory, "scheduler-a", generate=expire_lease) as gateways:
        summary = _schedule(_request(tmp_path, manifest), factory, gateways)

    entry = summary["entries"][0]
    assert entry["outcome"] == "failed" and entry["reason"] == "execution_error"
    assert entry["detail"] == "RunExecutionDeniedError"
    assert summary["outcome"] == "idle_exit"
    runs = _runs(factory, registered.task_id)
    assert len(runs) == 1 and runs[0].status is RunStatus.RUNNING
    with factory() as session:
        assert session.scalars(select(Verification).where(
            Verification.run_id == runs[0].id)).all() == []
    # Nothing was retried: the entry is concluded for this process.
    assert entry["state"] == "handled"


def test_the_idle_wait_returns_immediately_when_stopping():
    stop = Event()
    stop.set()
    assert _wait_for_next_round(stop, 30) is True


# --- the CLI boundary -------------------------------------------------------------------

def _cli_args(tmp_path, manifest, *, extra=()):
    for name in ("workspaces", "artifacts"):
        (tmp_path / name).mkdir(exist_ok=True)
    return [
        "--manifest", str(manifest), "--operator", "operator-a",
        "--python-executable", sys.executable,
        "--workspaces-root", str(tmp_path / "workspaces"),
        "--artifacts-root", str(tmp_path / "artifacts"),
        "--attempt-kind", "rehearsal", "--poll-interval-seconds", "0.01",
        "--worker-id", "scheduler-cli", *extra,
    ]


def test_the_cli_refuses_without_confirmation(
    worker_session_factory, tmp_path, monkeypatch, capsys,
):
    factory = worker_session_factory
    registered = _Registered(tmp_path, factory, monkeypatch, "a")
    manifest = _manifest(tmp_path, [registered.entry()])

    exit_code = scheduler_main(_cli_args(tmp_path, manifest), session_factory=factory,
                               gateways_factory=_gateways)

    assert exit_code == 2
    assert "confirm-run" in capsys.readouterr().err
    assert _runs(factory, registered.task_id) == []


def test_the_cli_runs_the_manifest_and_reports_the_summary(
    worker_session_factory, tmp_path, monkeypatch, capsys,
):
    factory = worker_session_factory
    registered = _Registered(tmp_path, factory, monkeypatch, "a")
    manifest = _manifest(tmp_path, [registered.entry()])
    import devflow_ai.worker_scheduler as scheduler_module
    actual_run_once = scheduler_module.run_worker_once
    passed_limits = []

    def observe_limits(*args, **kwargs):
        passed_limits.append((kwargs["max_read_bytes"], kwargs["max_patch_bytes"],
                              kwargs["verifier_max_diff_bytes"]))
        return actual_run_once(*args, **kwargs)

    monkeypatch.setattr(scheduler_module, "run_worker_once", observe_limits)

    @contextmanager
    def gateways(session_factory, worker_id):
        with _gateways(session_factory, worker_id, output=BLOCKED_DECISION) as built:
            yield built

    exit_code = scheduler_main(_cli_args(tmp_path, manifest, extra=[
        "--confirm-run", "--max-read-bytes", "1024", "--max-patch-bytes", "2048",
        "--max-diff-bytes", "4096"]),
                               session_factory=factory, gateways_factory=gateways)

    assert exit_code == 0
    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert event["event"] == "worker_scheduler_finished"
    assert event["registered_tasks_at_start"] == 1
    assert event["mode"] == "batch" and event["resident"] is False
    assert event["outcome"] == "idle_exit"
    assert event["formal_acceptance"] == "not_performed"
    assert event["writes_record"] is False
    assert passed_limits == [(1024, 2048, 4096)]
    assert len(_runs(factory, registered.task_id)) == 1


def test_the_cli_reports_an_unusable_manifest(
    worker_session_factory, tmp_path, capsys,
):
    factory = worker_session_factory

    exit_code = scheduler_main(
        _cli_args(tmp_path, tmp_path / "absent.json", extra=["--confirm-run"]),
        session_factory=factory, gateways_factory=_gateways)

    assert exit_code == 2
    assert "manifest" in capsys.readouterr().err


# --- resident mode: manifest re-reading, pausing and repair ----------------------------

def test_the_default_is_the_bounded_batch_mode(tmp_path):
    request = _request(tmp_path, tmp_path / "manifest.json")
    assert request.resident is False


def test_an_empty_manifest_is_only_allowed_in_resident_mode(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"schema_version": 1, "tasks": []}), encoding="utf-8")

    with pytest.raises(SchedulerManifestError):
        load_scheduler_manifest(path)
    assert load_scheduler_manifest(path, allow_empty=True).tasks == ()


def test_a_non_finite_poll_interval_is_refused(tmp_path):
    for value in (float("nan"), float("inf"), float("-inf"), 0, -1):
        with pytest.raises(ValueError, match="finite positive"):
            _request(tmp_path, tmp_path / "manifest.json", poll_interval=value)


def test_resident_mode_starts_empty_and_picks_up_a_task_added_later(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    registered = _Registered(tmp_path, factory, monkeypatch, "a")
    manifest = _manifest(tmp_path, [])
    request = _request(tmp_path, manifest, resident=True)
    stop = Event()
    waits = {"count": 0}

    def wait(event, seconds):
        waits["count"] += 1
        if waits["count"] == 1:
            # The operator registers a Task while the scheduler is idling.
            _manifest(tmp_path, [registered.entry()])
        elif waits["count"] >= 3:
            event.set()
        return event.is_set()

    with _gateways(factory, "scheduler-a", output=BLOCKED_DECISION) as gateways:
        summary = _schedule(request, factory, gateways, stop_event=stop, wait=wait)

    assert summary["mode"] == "resident" and summary["outcome"] == "stopped"
    assert summary["rounds"] >= 3
    assert len(_runs(factory, registered.task_id)) == 1
    assert summary["counts"] == {"stopped": 1}


def test_an_invalid_manifest_pauses_claiming_and_resumes_after_repair(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    registered = _Registered(tmp_path, factory, monkeypatch, "a")
    manifest = _manifest(tmp_path, [registered.entry()])
    request = _request(tmp_path, manifest, resident=True)
    stop = Event()
    events = []
    waits = {"count": 0}
    prepares = []

    from devflow_ai.worker_run import prepare_worker_run

    def counting_prepare(entry_request):
        prepares.append(entry_request.task_id)
        return prepare_worker_run(entry_request, session_factory=factory)

    def busy(_request, **_kwargs):
        raise WorkerRunBusyError("Task is locked; retry the same creation request")

    def wait(event, seconds):
        waits["count"] += 1
        if waits["count"] == 1:
            # A half-written file: the last good list must not stay in force.
            manifest.write_text('{"schema_version": 1, "tasks": [', encoding="utf-8")
        elif waits["count"] == 2:
            _manifest(tmp_path, [registered.entry()])
        else:
            event.set()
        return event.is_set()

    with _gateways(factory, "scheduler-a", output=BLOCKED_DECISION) as gateways:
        summary = _schedule(request, factory, gateways, stop_event=stop, wait=wait,
                            run_once=busy, prepare=counting_prepare,
                            report=events.append)

    assert summary["manifest_errors"] >= 1
    assert summary["manifest_paused_rounds"] >= 1
    assert [event["event"] for event in events].count("worker_scheduler_paused") == 1
    assert "worker_scheduler_resumed" in [event["event"] for event in events]
    # Round 1 tried the entry; the paused round did not, so the old list was not reused.
    assert len(prepares) == 2
    assert _runs(factory, registered.task_id) == []


def test_a_task_removed_while_waiting_is_not_claimed_from_memory(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    registered = _Registered(tmp_path, factory, monkeypatch, "a")
    manifest = _manifest(tmp_path, [registered.entry()])
    request = _request(tmp_path, manifest, resident=True)
    stop = Event()
    waits = {"count": 0}
    prepares = []

    from devflow_ai.worker_run import prepare_worker_run

    def counting_prepare(entry_request):
        prepares.append(entry_request.task_id)
        return prepare_worker_run(entry_request, session_factory=factory)

    def busy(_request, **_kwargs):
        raise WorkerRunBusyError("Task is locked; retry the same creation request")

    def wait(event, seconds):
        waits["count"] += 1
        if waits["count"] == 1:
            _manifest(tmp_path, [])  # the Task is withdrawn before it was ever claimed
        else:
            event.set()
        return event.is_set()

    with _gateways(factory, "scheduler-a", output=BLOCKED_DECISION) as gateways:
        summary = _schedule(request, factory, gateways, stop_event=stop, wait=wait,
                            run_once=busy, prepare=counting_prepare)

    assert len(prepares) == 1  # only the round in which it was still registered
    assert summary["entries"][0]["state"] == "removed"
    assert summary["entries"][0]["registered"] is False
    assert _runs(factory, registered.task_id) == []


def test_a_refused_entry_is_rechecked_only_after_its_inputs_change(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    registered = _Registered(tmp_path, factory, monkeypatch, "a")
    # The pack claims a different commit, so the entry is refused while it stays unchanged.
    wrong_pack = _pack(registered.directory, "wrong", base_commit="b" * 40)
    manifest = _manifest(tmp_path, [(registered.task_id, wrong_pack, registered.policy)])
    request = _request(tmp_path, manifest, resident=True)
    stop = Event()
    waits = {"count": 0}
    prepares = []

    from devflow_ai.worker_run import prepare_worker_run

    def counting_prepare(entry_request):
        prepares.append(entry_request.task_id)
        return prepare_worker_run(entry_request, session_factory=factory)

    def wait(event, seconds):
        waits["count"] += 1
        if waits["count"] == 2:
            # The operator fixes the pack file in place: same path, different bytes.
            fixed = _pack(registered.directory, "fixed", base_commit=registered.head)
            (wrong_pack / "task.json").write_bytes((fixed / "task.json").read_bytes())
        elif waits["count"] >= 4:
            event.set()
        return event.is_set()

    with _gateways(factory, "scheduler-a", output=BLOCKED_DECISION) as gateways:
        summary = _schedule(request, factory, gateways, stop_event=stop, wait=wait,
                            prepare=counting_prepare)

    # Round 1 refused it, round 2 did not re-check it (no change), round 3 did.
    assert len(prepares) == 2
    assert len(_runs(factory, registered.task_id)) == 1
    assert summary["counts"] == {"stopped": 1}


def test_resident_mode_exits_when_stopped_during_the_idle_wait(
    worker_session_factory, tmp_path,
):
    factory = worker_session_factory
    manifest = _manifest(tmp_path, [])
    request = _request(tmp_path, manifest, resident=True)
    stop = Event()

    def wait(event, seconds):
        event.set()  # the operator stops the scheduler while it is idle
        return True

    with _gateways(factory, "scheduler-a", output=BLOCKED_DECISION) as gateways:
        summary = _schedule(request, factory, gateways, stop_event=stop, wait=wait)

    assert summary["outcome"] == "stopped" and summary["stop_requested"] is True
    assert summary["rounds"] == 1 and summary["waited_rounds"] == 1
    assert summary["entries"] == []


def test_two_resident_schedulers_racing_create_one_run(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    registered = _Registered(tmp_path, factory, monkeypatch, "a")
    manifest = _manifest(tmp_path, [registered.entry()])
    request = _request(tmp_path, manifest, resident=True)
    barrier = Barrier(2, timeout=20)
    summaries = []

    def schedule(worker_id):
        barrier.wait()
        stop = Event()
        waits = {"count": 0}

        def wait(event, seconds):
            waits["count"] += 1
            if waits["count"] >= 40:
                event.set()
            return event.is_set()

        with _gateways(factory, worker_id, output=BLOCKED_DECISION) as gateways:
            summaries.append(_schedule(request, factory, gateways, worker_id=worker_id,
                                       stop_event=stop, wait=wait))

    threads = [Thread(target=schedule, args=(name,)) for name in ("scheduler-a", "scheduler-b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(summaries) == 2
    assert len(_runs(factory, registered.task_id)) == 1
    claimed = sum(1 for summary in summaries
                  for entry in summary["entries"] if entry["outcome"] == "stopped")
    assert claimed == 1


def test_the_cli_rejects_a_non_finite_interval(
    worker_session_factory, tmp_path, monkeypatch, capsys,
):
    factory = worker_session_factory
    registered = _Registered(tmp_path, factory, monkeypatch, "a")
    manifest = _manifest(tmp_path, [registered.entry()])

    for value in ("nan", "inf"):
        exit_code = scheduler_main(
            _cli_args(tmp_path, manifest, extra=["--confirm-run",
                                                 "--poll-interval-seconds", value]),
            session_factory=factory, gateways_factory=_gateways)
        assert exit_code == 2
        assert "finite positive" in capsys.readouterr().err
    assert _runs(factory, registered.task_id) == []


def test_the_cli_watch_mode_reports_a_stopped_resident_run(
    worker_session_factory, tmp_path, monkeypatch, capsys,
):
    import devflow_ai.worker_scheduler as scheduler_module

    factory = worker_session_factory
    registered = _Registered(tmp_path, factory, monkeypatch, "a")
    manifest = _manifest(tmp_path, [])

    @contextmanager
    def already_stopped():
        event = Event()
        event.set()
        yield event

    monkeypatch.setattr(scheduler_module, "stop_on_termination_signal", already_stopped)

    @contextmanager
    def gateways(session_factory, worker_id):
        with _gateways(session_factory, worker_id, output=BLOCKED_DECISION) as built:
            yield built

    exit_code = scheduler_main(
        _cli_args(tmp_path, manifest, extra=["--confirm-run", "--watch"]),
        session_factory=factory, gateways_factory=gateways)

    assert exit_code == 0
    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert event["mode"] == "resident" and event["resident"] is True
    assert event["outcome"] == "stopped"
    assert event["formal_acceptance"] == "not_performed"
    assert _runs(factory, registered.task_id) == []


# --- revocation while an earlier entry of the same round is running --------------------

def _revoke_after_first(real_run_once, first_task_id, hook):
    """Run through the entry, then let the operator change the manifest mid-round."""
    fired = {"done": False}

    def run_once(request, **kwargs):
        result = real_run_once(request, **kwargs)
        if request.task_id == first_task_id and not fired["done"]:
            fired["done"] = True
            hook()
        return result

    return run_once


def test_a_task_revoked_while_the_previous_entry_runs_is_not_claimed(
    worker_session_factory, tmp_path, monkeypatch,
):
    """The next claim must use the newest list, not the round's opening snapshot."""
    factory = worker_session_factory
    first = _Registered(tmp_path, factory, monkeypatch, "a")
    second = _Registered(tmp_path, factory, monkeypatch, "b")
    manifest = _manifest(tmp_path, [first.entry(), second.entry()])
    request = _request(tmp_path, manifest, resident=True)
    stop = Event()
    waits = {"count": 0}

    def wait(event, seconds):
        waits["count"] += 1
        if waits["count"] >= 2:
            event.set()
        return event.is_set()

    from devflow_ai.worker_run import run_worker_once

    run_once = _revoke_after_first(
        run_worker_once, first.task_id,
        lambda: _manifest(tmp_path, [first.entry()]))

    with _gateways(factory, "scheduler-a", output=BLOCKED_DECISION) as gateways:
        summary = _schedule(request, factory, gateways, stop_event=stop, wait=wait,
                            run_once=run_once)

    assert len(_runs(factory, first.task_id)) == 1
    assert _runs(factory, second.task_id) == []
    by_task = {entry["task_id"]: entry for entry in summary["entries"]}
    assert by_task[str(second.task_id)]["state"] == "removed"
    assert by_task[str(second.task_id)]["registered"] is False
    # The Run claimed before the revocation is left exactly as it was.
    assert _runs(factory, first.task_id)[0].status is RunStatus.RUNNING


def test_a_manifest_broken_while_the_previous_entry_runs_pauses_the_next_claim(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    first = _Registered(tmp_path, factory, monkeypatch, "a")
    second = _Registered(tmp_path, factory, monkeypatch, "b")
    manifest = _manifest(tmp_path, [first.entry(), second.entry()])
    request = _request(tmp_path, manifest, resident=True)
    stop = Event()
    events = []
    prepares = []
    waits = {"count": 0}

    from devflow_ai.worker_run import prepare_worker_run, run_worker_once

    def counting_prepare(entry_request):
        prepares.append(entry_request.task_id)
        return prepare_worker_run(entry_request, session_factory=factory)

    def wait(event, seconds):
        waits["count"] += 1
        if waits["count"] == 1:
            _manifest(tmp_path, [first.entry(), second.entry()])  # repaired
        elif waits["count"] >= 3:
            event.set()
        return event.is_set()

    run_once = _revoke_after_first(
        run_worker_once, first.task_id,
        lambda: manifest.write_text('{"schema_version": 1, "tasks": [', encoding="utf-8"))

    with _gateways(factory, "scheduler-a", output=BLOCKED_DECISION) as gateways:
        summary = _schedule(request, factory, gateways, stop_event=stop, wait=wait,
                            run_once=run_once, prepare=counting_prepare,
                            report=events.append)

    # The broken round did not even prepare the second entry...
    assert prepares.count(second.task_id) == 1
    assert summary["manifest_errors"] >= 1
    assert [event["event"] for event in events].count("worker_scheduler_paused") == 1
    assert "worker_scheduler_resumed" in [event["event"] for event in events]
    # ...and it was claimed once the file was repaired.
    assert len(_runs(factory, first.task_id)) == 1
    assert len(_runs(factory, second.task_id)) == 1


def test_an_entry_rewritten_while_the_previous_entry_runs_uses_the_new_paths(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    first = _Registered(tmp_path, factory, monkeypatch, "a")
    second = _Registered(tmp_path, factory, monkeypatch, "b")
    stale_pack = _pack(second.directory, "stale", base_commit="b" * 40)
    fixed_pack = _pack(second.directory, "fixed", base_commit=second.head)
    manifest = _manifest(tmp_path, [first.entry(), (second.task_id, stale_pack, second.policy)])
    request = _request(tmp_path, manifest, resident=True)
    stop = Event()
    prepared_packs = []
    waits = {"count": 0}

    from devflow_ai.worker_run import prepare_worker_run, run_worker_once

    def counting_prepare(entry_request):
        prepared_packs.append((entry_request.task_id, entry_request.pack_dir))
        return prepare_worker_run(entry_request, session_factory=factory)

    def wait(event, seconds):
        waits["count"] += 1
        if waits["count"] >= 2:
            event.set()
        return event.is_set()

    run_once = _revoke_after_first(
        run_worker_once, first.task_id,
        lambda: _manifest(tmp_path, [first.entry(), (second.task_id, fixed_pack, second.policy)]))

    with _gateways(factory, "scheduler-a", output=BLOCKED_DECISION) as gateways:
        summary = _schedule(request, factory, gateways, stop_event=stop, wait=wait,
                            run_once=run_once, prepare=counting_prepare)

    second_prepares = [pack for task_id, pack in prepared_packs if task_id == second.task_id]
    assert len(second_prepares) == 1
    assert second_prepares[0] == fixed_pack  # the stale entry was never used
    assert len(_runs(factory, second.task_id)) == 1
    by_task = {entry["task_id"]: entry for entry in summary["entries"]}
    assert by_task[str(second.task_id)]["outcome"] == "stopped"


def test_a_stop_after_a_revocation_leaves_the_claimed_run_alone(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    first = _Registered(tmp_path, factory, monkeypatch, "a")
    second = _Registered(tmp_path, factory, monkeypatch, "b")
    manifest = _manifest(tmp_path, [first.entry(), second.entry()])
    request = _request(tmp_path, manifest, resident=True)
    stop = Event()

    from devflow_ai.worker_run import run_worker_once

    def revoke_and_stop():
        _manifest(tmp_path, [])
        stop.set()

    run_once = _revoke_after_first(run_worker_once, first.task_id, revoke_and_stop)

    with _gateways(factory, "scheduler-a", output=BLOCKED_DECISION) as gateways:
        summary = _schedule(request, factory, gateways, stop_event=stop, run_once=run_once)

    assert summary["outcome"] == "stopped" and summary["stop_requested"] is True
    assert len(_runs(factory, first.task_id)) == 1
    assert _runs(factory, second.task_id) == []
    assert _runs(factory, first.task_id)[0].status is RunStatus.RUNNING


def test_batch_mode_keeps_its_single_snapshot_semantics(
    worker_session_factory, tmp_path, monkeypatch,
):
    """Batch mode reads the manifest once by design; the re-check loop is resident only."""
    factory = worker_session_factory
    first = _Registered(tmp_path, factory, monkeypatch, "a")
    second = _Registered(tmp_path, factory, monkeypatch, "b")
    manifest = _manifest(tmp_path, [first.entry(), second.entry()])
    request = _request(tmp_path, manifest)

    with _gateways(factory, "scheduler-a", output=BLOCKED_DECISION) as gateways:
        summary = _schedule(request, factory, gateways)

    assert summary["mode"] == "batch" and summary["outcome"] == "idle_exit"
    assert summary["counts"] == {"stopped": 2}
    assert len(_runs(factory, first.task_id)) == 1
    assert len(_runs(factory, second.task_id)) == 1
