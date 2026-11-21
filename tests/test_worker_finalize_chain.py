"""Scheduler → trusted entry: the draft is handed over inside the execution, or not at all.

Every test here uses the real isolated schema, a real controlled git repository and model
stubs. The trusted evaluation itself (a container run plus the formal commit transaction) is
**stubbed**: these tests show the scheduler reaches the trusted entry with the caller's
bound material while the Run's heartbeat, lease, execution right and stop event are live.
They are not evidence that a real Docker evaluation or a real task passed.
"""
import base64
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from devflow_ai.config import HeartbeatConfig
from devflow_ai.coordinator import ModelTokenUsage, RunCoordinator, RunExecutionDeniedError
from devflow_ai.enums import AgentExecutionStatus, AgentRole, RunStatus, TaskStatus
from devflow_ai.model_gateway import ModelReply
from devflow_ai.model_gateways import model_gateways_from_env
from devflow_ai.models import AgentExecution, Run, Task, Verification
from devflow_ai.policy import build_default_policy
from devflow_ai.services import AdmissionRecord, TaskService
from devflow_ai.worker import Worker
from devflow_ai.worker_run import prepare_worker_run, run_worker_once
from devflow_ai.worker_scheduler import SchedulerRequest, run_scheduler

SUITE_VERSION = "worker-chain-suite-v1"
BLOCKED_DECISION = {
    "outcome": "BLOCKED",
    "reason": "The Issue needs a decision before exploration can start",
    "decision_required": "Name the affected module",
}
MATERIAL_MARKER = b"chain-expected-output"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          check=True, timeout=15, text=True).stdout.strip()


def _repo(directory, monkeypatch):
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


def _material_file(directory, *, suite_version=SUITE_VERSION, marker=MATERIAL_MARKER):
    """A material file in the documented shape; the caller keeps it outside the repository."""
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "suite_version": suite_version,
        "image": "python:3.11-slim",
        "command": ["python", "-B", "/opt/devflow/adapter.py"],
        "cases": [{"stdin_base64": base64.b64encode(b"input").decode("ascii"),
                   "stdout_base64": base64.b64encode(marker).decode("ascii")}],
        "calibration": {"baseline_workspace": str(directory / "calibration" / "base"),
                        "corrected_workspace": str(directory / "calibration" / "fix"),
                        "expected_commit": "c" * 40},
        "timeout_seconds": 15,
    }
    path = directory / "material.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _pack(directory, name, *, base_commit, material=None, suite_version=SUITE_VERSION):
    pack_dir = directory / f"pack-{name}"
    pack_dir.mkdir()
    (pack_dir / "scenarios.py").write_text(
        "def test_placeholder():\n    assert True\n", encoding="utf-8")
    suite = None
    if material is not None:
        suite = {
            "suite_version": suite_version,
            "materials_sha256": sha256(material.read_bytes()).hexdigest(),
            "digest_scope": "bytes as delivered",
            "digest_method": "sha256 over those bytes",
            "provided_by": "trusted_control_plane",
            "declaration": "Provided by the trusted control plane.",
        }
    manifest = {
        "pack_id": f"chain-pack-{name}", "schema_version": 1, "title": "Chain pack",
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
        "independent_suite": suite,
        "unverified": ([{"field": "policy", "reason": "Not pinned for this fixture."}]
                       + ([] if suite is not None
                          else [{"field": "independent_suite", "reason": "Held elsewhere."}])),
    }
    (pack_dir / "task.json").write_text(json.dumps(manifest), encoding="utf-8")
    return pack_dir


def _task(session_factory, repo, base_commit):
    policy = build_default_policy()
    policy.policy_version = "worker-chain-test-v1"
    policy.permissions.allowed_tools_by_role[AgentRole.EXPLORER] = ["read_file"]
    with session_factory() as session:
        task, created = TaskService(session, policy_factory=lambda: policy).create_task(
            title="Chained Task", description="The scheduler drives this Run",
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
    path.write_text(json.dumps({"schema_version": 1, "tasks": entries}), encoding="utf-8")
    return path


class _Registered:
    """One admitted Task with a pack, a Policy file and (optionally) trusted material."""

    def __init__(self, tmp_path, session_factory, monkeypatch, name="a", *,
                 material=None, pack_material=None):
        self.directory = tmp_path / name
        self.directory.mkdir()
        self.repo, head = _repo(self.directory, monkeypatch)
        self.head = head
        self.material = material
        self.pack = _pack(self.directory, name, base_commit=head,
                          material=pack_material if pack_material is not None else material)
        self.task_id = _task(session_factory, self.repo, head)
        self.policy = _policy_file(self.directory, session_factory, self.task_id, name)

    def entry(self, *, material=True):
        payload = {"task_id": str(self.task_id), "pack": str(self.pack),
                   "policy": str(self.policy)}
        if material and self.material is not None:
            payload["material"] = str(self.material)
        return payload


def _request(tmp_path, manifest, *, attempt_kind="formal"):
    for name in ("workspaces", "artifacts"):
        (tmp_path / name).mkdir(exist_ok=True)
    return SchedulerRequest(
        manifest_path=manifest, operator="operator-a", python_executable=sys.executable,
        workspaces_root=tmp_path / "workspaces", artifacts_root=tmp_path / "artifacts",
        attempt_kind=attempt_kind, poll_interval_seconds=0.01)


@contextmanager
def _gateways(session_factory, worker_id):
    def generate(prompt, limit):
        return ModelReply(BLOCKED_DECISION, ModelTokenUsage(input_tokens=40,
                                                            output_tokens=20))

    with model_gateways_from_env(session_factory, worker_id, generate=generate) as gateways:
        yield gateways


def _write_draft(session_factory, task_id, *, preliminary="passed"):
    """Insert the review checkpoint the Verifier would have committed.

    Constructed evidence: it stands in for a Verifier that ran the public checks, which this
    platform cannot do (the JUnit capture needs a Windows Job).
    """
    with session_factory() as session, session.begin():
        run_id = session.scalar(select(Run.id).where(Run.task_id == task_id))
        assert run_id is not None
        session.add(AgentExecution(
            run_id=run_id, agent_role=AgentRole.VERIFIER.value.lower(),
            status=AgentExecutionStatus.RUNNING,
            input={"source": "constructed draft"},
            output={"outcome": "VERIFICATION_BLOCKED",
                    "preliminary_public_outcome": preliminary,
                    "draft_artifact_id": str(uuid4()),
                    "missing_evidence": ["test_integrity"]}))
        return run_id


def _drafting_run_once(session_factory, task_id, *, preliminary="passed", after=None):
    """Drive the entry by writing its review draft instead of running the stages."""
    def run_once(request, **kwargs):
        def drive(directories, stop):
            _write_draft(session_factory, task_id, preliminary=preliminary)
            if after is not None:
                after(directories, stop)
        return run_worker_once(request, drive=drive, **kwargs)
    return run_once


class _TrustedEntry:
    """Stand-in for the container evaluation plus the formal commit transaction."""

    def __init__(self, session_factory, *, outcome="passed", error=None,
                 mark_passed=True, settle_seconds=0.0):
        self.session_factory = session_factory
        self.outcome = outcome
        self.error = error
        self.mark_passed = mark_passed
        # How long to keep working after the commit, so a heartbeat tick can land in that
        # window. Used by the race test; zero for every other test.
        self.settle_seconds = settle_seconds
        self.calls = []

    def install(self, monkeypatch):
        """Bind as a method: a bare callable would arrive without the worker instance."""
        def fake(worker_self, run_id, **kwargs):
            return self(worker_self, run_id, **kwargs)

        monkeypatch.setattr(Worker, "finalize_with_independent_evaluation", fake)

    def __call__(self, worker_self, run_id, **kwargs):
        self.calls.append({"run_id": run_id, **kwargs})
        if self.error is not None:
            raise self.error
        # The trusted entry only runs while the Run is still ours and the attempt is live.
        with self.session_factory() as session, session.begin():
            RunCoordinator(session).assert_execution_right(run_id, worker_self.worker_id)
            assert not kwargs["stop_event"].is_set()
            if self.mark_passed and self.outcome == "passed":
                session.get(Run, run_id).status = RunStatus.PASSED
        if self.settle_seconds:
            # The trusted entry has committed; the attempt keeps running long enough for the
            # heartbeat to try (and fail) to renew a Run that no longer needs renewing.
            time.sleep(self.settle_seconds)
        return {"outcome": self.outcome,
                "independent_artifact_id": str(uuid4()),
                "verification_id": str(uuid4()) if self.outcome == "passed" else None}


def _verifications(session_factory, run_id):
    with session_factory() as session:
        return session.scalars(select(Verification).where(
            Verification.run_id == UUID(str(run_id)))).all()


# --- the chain ------------------------------------------------------------------------

def test_the_scheduler_hands_a_draft_to_the_trusted_entry_inside_the_execution(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    material = _material_file(tmp_path)
    registered = _Registered(tmp_path, factory, monkeypatch, "a", material=material)
    manifest = _manifest(tmp_path, [registered.entry()])
    trusted = _TrustedEntry(factory)
    trusted.install(monkeypatch)

    with _gateways(factory, "scheduler-a") as gateways:
        summary = run_scheduler(
            _request(tmp_path, manifest), session_factory=factory, worker_id="scheduler-a",
            gateways=gateways,
            heartbeat_config=_heartbeat(),
            prepare=_formal_prepare(factory),
            run_once=_drafting_run_once(factory, registered.task_id))

    entry = summary["entries"][0]
    assert entry["finalization"]["outcome"] == "finalized"
    assert entry["finalization"]["independent_outcome"] == "passed"
    assert entry["run_status"] == RunStatus.PASSED.value
    assert summary["formal_acceptance"] == "committed"
    assert len(trusted.calls) == 1
    call = trusted.calls[0]
    # The material the caller named is what reached the trusted entry, bound to this Task.
    assert call["verification_config"].task_id == registered.task_id
    assert call["verification_config"].independent_suite_version == SUITE_VERSION
    assert call["image"] == "python:3.11-slim"
    assert call["max_diff_bytes"] == 262144 and call["max_patch_bytes"] == 65536
    # ... and the scheduled summary never carries the material's contents.
    assert MATERIAL_MARKER.decode() not in json.dumps(summary, default=str)


def _formal_prepare(session_factory):
    """Prepare with Linux's platform limit explicitly bypassed for the test.

    The real gate is unchanged: a formal attempt is refused where the trusted baseline
    cannot be recorded. These tests only inject the capability through the seam
    ``prepare_worker_run`` already exposes, so they can exercise the formal path here.
    """
    def prepare(entry_request):
        return prepare_worker_run(entry_request, session_factory=session_factory,
                                  platform_supports_trusted_baseline=True)
    return prepare


def _heartbeat():
    return HeartbeatConfig(statement_timeout_seconds=5, shutdown_timeout_seconds=5)


def test_without_material_the_attempt_stops_at_the_draft(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    material = _material_file(tmp_path)
    registered = _Registered(tmp_path, factory, monkeypatch, "a", material=material)
    manifest = _manifest(tmp_path, [registered.entry(material=False)])
    trusted = _TrustedEntry(factory)
    trusted.install(monkeypatch)

    with _gateways(factory, "scheduler-a") as gateways:
        summary = run_scheduler(
            _request(tmp_path, manifest), session_factory=factory, worker_id="scheduler-a",
            gateways=gateways, heartbeat_config=_heartbeat(),
            prepare=_formal_prepare(factory), run_once=_drafting_run_once(factory, registered.task_id))

    entry = summary["entries"][0]
    assert entry["finalization"] == {"outcome": "skipped",
                                     "reason": "no_trusted_material"}
    assert entry["run_status"] == RunStatus.RUNNING.value
    assert summary["formal_acceptance"] == "not_performed"
    assert trusted.calls == []
    assert _verifications(factory, entry["run_id"]) == []


def test_material_for_another_pack_is_refused_before_the_claim(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    own_material = _material_file(tmp_path, marker=b"pack-a-output")
    other_material = _material_file(tmp_path / "other-material", marker=b"other-output")
    registered = _Registered(tmp_path, factory, monkeypatch, "a", material=own_material)
    manifest = _manifest(tmp_path, [dict(registered.entry(), material=str(other_material))])
    trusted = _TrustedEntry(factory)
    trusted.install(monkeypatch)

    with _gateways(factory, "scheduler-a") as gateways:
        summary = run_scheduler(
            _request(tmp_path, manifest), session_factory=factory, worker_id="scheduler-a",
            gateways=gateways, heartbeat_config=_heartbeat(),
                               prepare=_formal_prepare(factory))

    entry = summary["entries"][0]
    assert entry["outcome"] == "refused"
    assert "material" in entry["detail"]
    with factory() as session:
        assert session.scalars(select(Run).where(
            Run.task_id == registered.task_id)).all() == []
    assert trusted.calls == []


def test_two_entries_keep_their_own_material(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    first_material = _material_file(tmp_path / "first-material", marker=b"first-output")
    second_material = _material_file(tmp_path / "second-material", marker=b"second-output")
    first = _Registered(tmp_path, factory, monkeypatch, "a", material=first_material)
    second = _Registered(tmp_path, factory, monkeypatch, "b", material=second_material)
    # The second entry names material that belongs to the first pack: that must not be used.
    manifest = _manifest(tmp_path, [first.entry(),
                                    dict(second.entry(material=False),
                                         material=str(first_material))])
    trusted = _TrustedEntry(factory)
    trusted.install(monkeypatch)

    with _gateways(factory, "scheduler-a") as gateways:
        summary = run_scheduler(
            _request(tmp_path, manifest), session_factory=factory, worker_id="scheduler-a",
            gateways=gateways, heartbeat_config=_heartbeat(),
            prepare=_formal_prepare(factory), run_once=_drafting_run_once(factory, first.task_id))

    entries = {entry["task_id"]: entry for entry in summary["entries"]}
    assert entries[str(first.task_id)]["finalization"]["outcome"] == "finalized"
    assert entries[str(second.task_id)]["outcome"] == "refused"
    assert len(trusted.calls) == 1
    assert trusted.calls[0]["verification_config"].task_id == first.task_id
    with factory() as session:
        assert session.scalars(select(Run).where(
            Run.task_id == second.task_id)).all() == []


def test_a_draft_that_did_not_pass_the_public_checks_is_not_evaluated(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    material = _material_file(tmp_path)
    registered = _Registered(tmp_path, factory, monkeypatch, "a", material=material)
    manifest = _manifest(tmp_path, [registered.entry()])
    trusted = _TrustedEntry(factory)
    trusted.install(monkeypatch)

    with _gateways(factory, "scheduler-a") as gateways:
        summary = run_scheduler(
            _request(tmp_path, manifest), session_factory=factory, worker_id="scheduler-a",
            gateways=gateways, heartbeat_config=_heartbeat(),
            prepare=_formal_prepare(factory), run_once=_drafting_run_once(factory, registered.task_id, preliminary="failed"))

    entry = summary["entries"][0]
    assert entry["finalization"] == {"outcome": "not_finalized",
                                     "reason": "public_checks_not_passed"}
    assert trusted.calls == []
    assert summary["formal_acceptance"] == "not_performed"


def test_a_stop_before_the_trusted_entry_leaves_the_draft_alone(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    material = _material_file(tmp_path)
    registered = _Registered(tmp_path, factory, monkeypatch, "a", material=material)
    manifest = _manifest(tmp_path, [registered.entry()])
    trusted = _TrustedEntry(factory)
    trusted.install(monkeypatch)
    stop = Event()

    def stop_after_draft(directories, _stage_stop):
        stop.set()

    with _gateways(factory, "scheduler-a") as gateways:
        summary = run_scheduler(
            _request(tmp_path, manifest), session_factory=factory, worker_id="scheduler-a",
            gateways=gateways, heartbeat_config=_heartbeat(),
            prepare=_formal_prepare(factory), stop_event=stop,
            run_once=_drafting_run_once(factory, registered.task_id,
                                        after=stop_after_draft))

    entry = summary["entries"][0]
    assert entry["finalization"] == {"outcome": "not_finalized",
                                     "reason": "stop_requested"}
    assert trusted.calls == []
    assert summary["stop_requested"] is True
    assert entry["run_status"] == RunStatus.RUNNING.value


def test_a_denied_execution_right_during_finalization_is_reported(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    material = _material_file(tmp_path)
    registered = _Registered(tmp_path, factory, monkeypatch, "a", material=material)
    manifest = _manifest(tmp_path, [registered.entry()])
    trusted = _TrustedEntry(factory, error=RunExecutionDeniedError("lease expired"))
    trusted.install(monkeypatch)

    with _gateways(factory, "scheduler-a") as gateways:
        summary = run_scheduler(
            _request(tmp_path, manifest), session_factory=factory, worker_id="scheduler-a",
            gateways=gateways, heartbeat_config=_heartbeat(),
            prepare=_formal_prepare(factory), run_once=_drafting_run_once(factory, registered.task_id))

    entry = summary["entries"][0]
    assert entry["outcome"] == "failed" and entry["reason"] == "execution_error"
    assert entry["detail"] == "RunExecutionDeniedError"
    assert entry["run_status"] == RunStatus.RUNNING.value
    assert len(trusted.calls) == 1
    assert _verifications(factory, entry["run_id"]) == []
    assert summary["formal_acceptance"] == "not_performed"


def test_an_unavailable_evaluation_is_reported_as_unavailable(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    material = _material_file(tmp_path)
    registered = _Registered(tmp_path, factory, monkeypatch, "a", material=material)
    manifest = _manifest(tmp_path, [registered.entry()])
    trusted = _TrustedEntry(factory, outcome="unavailable")
    trusted.install(monkeypatch)

    with _gateways(factory, "scheduler-a") as gateways:
        summary = run_scheduler(
            _request(tmp_path, manifest), session_factory=factory, worker_id="scheduler-a",
            gateways=gateways, heartbeat_config=_heartbeat(),
            prepare=_formal_prepare(factory), run_once=_drafting_run_once(factory, registered.task_id))

    entry = summary["entries"][0]
    assert entry["finalization"]["outcome"] == "finalized"
    assert entry["finalization"]["independent_outcome"] == "unavailable"
    assert entry["run_status"] == RunStatus.RUNNING.value
    assert summary["formal_acceptance"] == "not_performed"


def test_a_replay_does_not_evaluate_a_second_time(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    material = _material_file(tmp_path)
    registered = _Registered(tmp_path, factory, monkeypatch, "a", material=material)
    manifest = _manifest(tmp_path, [registered.entry()])
    trusted = _TrustedEntry(factory)
    trusted.install(monkeypatch)
    request = _request(tmp_path, manifest)

    with _gateways(factory, "scheduler-a") as gateways:
        first = run_scheduler(request, session_factory=factory, worker_id="scheduler-a",
                              gateways=gateways, heartbeat_config=_heartbeat(),
            prepare=_formal_prepare(factory),
                              run_once=_drafting_run_once(factory, registered.task_id))
        calls_after_first = len(trusted.calls)
        second = run_scheduler(request, session_factory=factory, worker_id="scheduler-b",
                               gateways=gateways, heartbeat_config=_heartbeat(),
                               prepare=_formal_prepare(factory))

    assert first["entries"][0]["finalization"]["outcome"] == "finalized"
    assert calls_after_first == 1
    assert second["entries"][0]["outcome"] == "existing_run"
    assert len(trusted.calls) == 1
    with factory() as session:
        assert len(session.scalars(select(Run).where(
            Run.task_id == registered.task_id)).all()) == 1


# --- only a formal attempt may reach the trusted transaction ---------------------------

def test_a_rehearsal_with_material_never_commits_a_formal_result(
    worker_session_factory, tmp_path, monkeypatch,
):
    """A rehearsal may run the same stages, but its draft stays a draft."""
    factory = worker_session_factory
    material = _material_file(tmp_path)
    registered = _Registered(tmp_path, factory, monkeypatch, "a", material=material)
    manifest = _manifest(tmp_path, [registered.entry()])
    trusted = _TrustedEntry(factory)
    trusted.install(monkeypatch)

    with _gateways(factory, "scheduler-a") as gateways:
        summary = run_scheduler(
            _request(tmp_path, manifest, attempt_kind="rehearsal"),
            session_factory=factory, worker_id="scheduler-a", gateways=gateways,
            heartbeat_config=_heartbeat(),
            run_once=_drafting_run_once(factory, registered.task_id))

    entry = summary["entries"][0]
    assert entry["finalization"] == {"outcome": "skipped",
                                     "reason": "attempt_kind_not_formal"}
    assert trusted.calls == []
    assert entry["run_status"] == RunStatus.RUNNING.value
    assert summary["formal_acceptance"] == "not_performed"
    assert _verifications(factory, entry["run_id"]) == []


def test_an_unknown_attempt_kind_with_material_never_commits_a_formal_result(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    material = _material_file(tmp_path)
    registered = _Registered(tmp_path, factory, monkeypatch, "a", material=material)
    manifest = _manifest(tmp_path, [registered.entry()])
    trusted = _TrustedEntry(factory)
    trusted.install(monkeypatch)

    with _gateways(factory, "scheduler-a") as gateways:
        summary = run_scheduler(
            _request(tmp_path, manifest, attempt_kind="unknown"),
            session_factory=factory, worker_id="scheduler-a", gateways=gateways,
            heartbeat_config=_heartbeat(),
            run_once=_drafting_run_once(factory, registered.task_id))

    entry = summary["entries"][0]
    assert entry["finalization"]["outcome"] == "skipped"
    assert entry["finalization"]["reason"] == "attempt_kind_not_formal"
    assert trusted.calls == []
    assert entry["run_status"] == RunStatus.RUNNING.value
    assert _verifications(factory, entry["run_id"]) == []


# --- where the material may live -------------------------------------------------------

def test_material_inside_the_task_pack_is_refused(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    material = _material_file(tmp_path)
    registered = _Registered(tmp_path, factory, monkeypatch, "a", material=material)
    inside_pack = registered.pack / "material.json"
    inside_pack.write_bytes(material.read_bytes())
    manifest = _manifest(tmp_path, [dict(registered.entry(), material=str(inside_pack))])

    with _gateways(factory, "scheduler-a") as gateways:
        summary = run_scheduler(
            _request(tmp_path, manifest, attempt_kind="rehearsal"),
            session_factory=factory, worker_id="scheduler-a", gateways=gateways,
            heartbeat_config=_heartbeat())

    entry = summary["entries"][0]
    assert entry["outcome"] == "refused"
    assert "outside the task pack" in entry["detail"]
    with factory() as session:
        assert session.scalars(select(Run).where(
            Run.task_id == registered.task_id)).all() == []


def test_material_inside_the_artifacts_root_is_refused(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    material = _material_file(tmp_path)
    registered = _Registered(tmp_path, factory, monkeypatch, "a", material=material)
    request = _request(tmp_path, _manifest(tmp_path, [registered.entry()]))
    inside_artifacts = tmp_path / "artifacts" / "material.json"
    inside_artifacts.write_bytes(material.read_bytes())
    manifest = _manifest(tmp_path, [dict(registered.entry(),
                                         material=str(inside_artifacts))])

    with _gateways(factory, "scheduler-a") as gateways:
        summary = run_scheduler(
            _request(tmp_path, manifest, attempt_kind="rehearsal"),
            session_factory=factory, worker_id="scheduler-a", gateways=gateways,
            heartbeat_config=_heartbeat())

    entry = summary["entries"][0]
    assert entry["outcome"] == "refused"
    assert "outside the artifacts root" in entry["detail"]


def test_a_public_task_pack_cannot_carry_a_material_path(tmp_path):
    """The pack is public: material is named by the trusted manifest, never inside the pack."""
    from devflow_ai.task_pack import load_manifest

    registered_pack = _pack(tmp_path, "pack", base_commit="a" * 40,
                            material=_material_file(tmp_path))
    payload = json.loads((registered_pack / "task.json").read_text(encoding="utf-8"))
    payload["material"] = "/trusted/elsewhere/material.json"
    (registered_pack / "task.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        load_manifest(registered_pack)


def test_the_material_never_lands_in_the_workspace_or_artifacts(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    material = _material_file(tmp_path)
    registered = _Registered(tmp_path, factory, monkeypatch, "a", material=material)
    manifest = _manifest(tmp_path, [registered.entry()])
    trusted = _TrustedEntry(factory)
    trusted.install(monkeypatch)

    with _gateways(factory, "scheduler-a") as gateways:
        summary = run_scheduler(
            _request(tmp_path, manifest), session_factory=factory, worker_id="scheduler-a",
            gateways=gateways, heartbeat_config=_heartbeat(),
            prepare=_formal_prepare(factory), run_once=_drafting_run_once(factory, registered.task_id))

    assert summary["entries"][0]["finalization"]["outcome"] == "finalized"
    for name in ("workspaces", "artifacts"):
        root = tmp_path / name
        leaked = [path for path in root.rglob("*")
                  if path.is_file() and MATERIAL_MARKER in path.read_bytes()]
        assert leaked == [], f"{name} contains the material's expected output: {leaked}"
    assert MATERIAL_MARKER.decode() not in json.dumps(summary, default=str)


def test_a_rehearsal_benchmark_attempt_never_finalizes(
    worker_session_factory, tmp_path, monkeypatch,
):
    """The same rule holds for the benchmark entry: a rehearsal never commits a result."""
    from devflow_ai.benchmark_run import BenchmarkRunRequest, run_one_task

    factory = worker_session_factory
    material = _material_file(tmp_path)
    registered = _Registered(tmp_path, factory, monkeypatch, "a", material=material)
    for name in ("workspaces", "artifacts"):
        (tmp_path / name).mkdir(exist_ok=True)
    trusted = _TrustedEntry(factory)
    trusted.install(monkeypatch)
    record = tmp_path / "record.json"
    request = BenchmarkRunRequest(
        pack_dir=registered.pack, task_id=registered.task_id, operator="operator-a",
        python_executable=sys.executable, workspaces_root=tmp_path / "workspaces",
        artifacts_root=tmp_path / "artifacts", record_path=record,
        attempt_kind="rehearsal")

    with _gateways(factory, "benchmark-worker") as gateways:
        result = run_one_task(
            request, session_factory=factory, worker=Worker(factory, "benchmark-worker"),
            gateways=gateways, heartbeat_config=_heartbeat(),
            drive=lambda directories, stop: None,
            independent_evaluation=object())

    assert result["finalization"] == {"outcome": "skipped",
                                      "reason": "attempt_kind_not_formal"}
    assert trusted.calls == []
    written = json.loads(record.read_text(encoding="utf-8"))
    assert written["attempt_kind"] == "rehearsal"
    assert "finalization=skipped" in written["notes"]


def test_benchmark_rejects_changed_pinned_scope_before_claim(worker_session_factory, tmp_path, monkeypatch):
    from devflow_ai.benchmark_run import BenchmarkRunError, BenchmarkRunRequest, run_one_task
    from devflow_ai.task_pack import load_manifest, scope_evidence_sha256

    factory = worker_session_factory
    registered = _Registered(tmp_path, factory, monkeypatch, "a")
    manifest = load_manifest(registered.pack)
    with factory.begin() as session:
        session.get(Task, registered.task_id).admission_record = {"reason": "fixture", "evidence": [
            f"pack:{manifest.pack_id}", f"scope-sha256-v1:{scope_evidence_sha256(manifest.scope)}"]}
    path = registered.pack / "task.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["scope"]["allowed_paths"] = ["elsewhere/"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    for name in ("workspaces", "artifacts"):
        (tmp_path / name).mkdir(exist_ok=True)
    request = BenchmarkRunRequest(pack_dir=registered.pack, task_id=registered.task_id,
        operator="operator-a", python_executable=sys.executable,
        workspaces_root=tmp_path / "workspaces", artifacts_root=tmp_path / "artifacts",
        record_path=tmp_path / "record.json", attempt_kind="rehearsal")
    with pytest.raises(BenchmarkRunError, match="scope binding"):
        run_one_task(request, session_factory=factory, worker=Worker(factory, "benchmark-worker"),
                     gateways=None, heartbeat_config=_heartbeat())
    with factory() as session:
        assert session.scalar(select(Run.id).where(Run.task_id == registered.task_id)) is None


def test_benchmark_rechecks_admission_at_claim(worker_session_factory, tmp_path, monkeypatch):
    from devflow_ai.benchmark_run import BenchmarkRunError, BenchmarkRunRequest, run_one_task
    factory = worker_session_factory
    registered = _Registered(tmp_path, factory, monkeypatch, "a")
    for name in ("workspaces", "artifacts"):
        (tmp_path / name).mkdir(exist_ok=True)
    request = BenchmarkRunRequest(pack_dir=registered.pack, task_id=registered.task_id,
        operator="operator-a", python_executable=sys.executable,
        workspaces_root=tmp_path / "workspaces", artifacts_root=tmp_path / "artifacts",
        record_path=tmp_path / "record.json", attempt_kind="rehearsal")
    original = RunCoordinator.start_initial_run
    def change_before_claim(self, *args, **kwargs):
        with factory.begin() as other:
            other.get(Task, registered.task_id).admission_record = {"reason": "changed", "evidence": []}
        return original(self, *args, **kwargs)
    monkeypatch.setattr(RunCoordinator, "start_initial_run", change_before_claim)
    with pytest.raises(BenchmarkRunError, match="admission record changed"):
        run_one_task(request, session_factory=factory, worker=Worker(factory, "benchmark-worker"),
                     gateways=None, heartbeat_config=_heartbeat())
    with factory() as session:
        assert session.scalar(select(Run.id).where(Run.task_id == registered.task_id)) is None


# --- the same placement rule for both entries ------------------------------------------

def _pin_pack_material(pack, material_path):
    """Point the pack's pinned digest at this material file.

    The placement rule is what these tests are about, so the digest gate must not fire
    first — that would make the refusal ambiguous.
    """
    payload = json.loads((pack / "task.json").read_text(encoding="utf-8"))
    suite = payload.get("independent_suite") or {
        "suite_version": SUITE_VERSION, "digest_scope": "bytes as delivered",
        "digest_method": "sha256 over those bytes",
        "provided_by": "trusted_control_plane",
        "declaration": "Provided by the trusted control plane.",
    }
    suite = dict(suite, materials_sha256=sha256(material_path.read_bytes()).hexdigest())
    payload["independent_suite"] = suite
    payload["unverified"] = [item for item in payload.get("unverified", [])
                             if item["field"] != "independent_suite"]
    (pack / "task.json").write_text(json.dumps(payload), encoding="utf-8")


def _benchmark_args(tmp_path, registered, material, *, record_name="record.json"):
    return ["--pack", str(registered.pack), "--task-id", str(registered.task_id),
            "--operator", "operator-a", "--python-executable", sys.executable,
            "--workspaces-root", str(tmp_path / "workspaces"),
            "--artifacts-root", str(tmp_path / "artifacts"),
            "--record", str(tmp_path / record_name), "--material", str(material),
            "--attempt-kind", "rehearsal", "--confirm-run"]


@pytest.mark.parametrize(("root", "phrase"), [
    ("pack", "outside the task pack"),
    ("workspaces", "outside the workspaces root"),
    ("artifacts", "outside the artifacts root"),
    ("repository", "outside the source repository"),
])
def test_both_entries_refuse_the_same_material_placements(
    root, phrase, worker_session_factory, tmp_path, monkeypatch, capsys,
):
    """One rule, one message: neither entry may read material from a public or run directory."""
    from devflow_ai.benchmark_run import main as benchmark_main

    factory = worker_session_factory
    registered = _Registered(tmp_path, factory, monkeypatch, "a")
    for name in ("workspaces", "artifacts"):
        (tmp_path / name).mkdir(exist_ok=True)
    where = {"pack": registered.pack,
             "workspaces": tmp_path / "workspaces",
             "artifacts": tmp_path / "artifacts",
             "repository": registered.repo}[root]
    misplaced = where / "material.json"
    misplaced.write_bytes(_material_file(tmp_path / "source").read_bytes())
    _pin_pack_material(registered.pack, misplaced)

    manifest = _manifest(tmp_path, [dict(registered.entry(material=False),
                                         material=str(misplaced))])
    with _gateways(factory, "scheduler-a") as gateways:
        summary = run_scheduler(
            _request(tmp_path, manifest, attempt_kind="rehearsal"),
            session_factory=factory, worker_id="scheduler-a", gateways=gateways,
            heartbeat_config=_heartbeat())
    assert summary["entries"][0]["outcome"] == "refused"
    assert phrase in summary["entries"][0]["detail"]

    exit_code = benchmark_main(
        _benchmark_args(tmp_path, registered, misplaced, record_name=f"record-{root}.json"),
        session_factory=factory, gateways_factory=_gateways)

    assert exit_code == 2
    assert phrase in capsys.readouterr().err
    with factory() as session:
        assert session.scalars(select(Run).where(
            Run.task_id == registered.task_id)).all() == []


def test_material_in_an_independent_trusted_directory_is_accepted(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    trusted_dir = tmp_path / "trusted-control"
    material = _material_file(trusted_dir)
    registered = _Registered(tmp_path, factory, monkeypatch, "a", material=material)
    manifest = _manifest(tmp_path, [registered.entry()])
    trusted = _TrustedEntry(factory)
    trusted.install(monkeypatch)

    with _gateways(factory, "scheduler-a") as gateways:
        summary = run_scheduler(
            _request(tmp_path, manifest), session_factory=factory, worker_id="scheduler-a",
            gateways=gateways, heartbeat_config=_heartbeat(),
            prepare=_formal_prepare(factory),
            run_once=_drafting_run_once(factory, registered.task_id))

    assert summary["entries"][0]["finalization"]["outcome"] == "finalized"
    assert len(trusted.calls) == 1


def test_a_remote_repository_cannot_place_the_material():
    """A remote URL says nothing about the local filesystem: the limit is stated, not guessed."""
    from devflow_ai.independent_evaluation import (
        local_repository_path, require_material_placement,
    )

    assert local_repository_path("https://github.com/pallets/click") is None
    assert local_repository_path("ssh://git@example.invalid/repo.git") is None
    assert local_repository_path("git@github.com:pallets/click.git") is None
    assert local_repository_path("/srv/repos/click") == Path("/srv/repos/click")
    # With a remote repository the local placements are still refused...
    with pytest.raises(ValueError, match="outside the task pack"):
        require_material_placement(
            "/srv/packs/click/material.json", pack_dir="/srv/packs/click",
            workspaces_root="/work/workspaces", artifacts_root="/work/artifacts",
            repository="https://github.com/pallets/click")
    # ... but nothing is claimed about a checkout that only exists remotely.
    require_material_placement(
        "/srv/control/material.json", pack_dir="/srv/packs/click",
        workspaces_root="/work/workspaces", artifacts_root="/work/artifacts",
        repository="https://github.com/pallets/click")


def test_a_symlink_into_the_pack_is_still_refused(tmp_path):
    from devflow_ai.independent_evaluation import (
        MaterialPlacementError, require_material_placement,
    )

    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "real-material.json").write_text("{}", encoding="utf-8")
    link = tmp_path / "link-to-material.json"
    try:
        link.symlink_to(pack / "real-material.json")
    except OSError:  # Windows without the symlink privilege
        pytest.skip("symlink creation is not available here")

    with pytest.raises(MaterialPlacementError, match="outside the task pack"):
        require_material_placement(
            link, pack_dir=pack, workspaces_root=tmp_path / "work",
            artifacts_root=tmp_path / "art", repository=None)


@pytest.mark.parametrize(("url", "expected"), [
    # A local POSIX path behind a file URL.
    ("file:///srv/repos/click", "/srv/repos/click"),
    # A Windows drive, with and without an explicit localhost authority.
    ("file:///C:/repos/click", "C:/repos/click"),
    ("file://localhost/C:/repos/click", "C:/repos/click"),
    # Percent-encoded and literal spaces both have to survive.
    ("file:///C:/Program%20Files/my%20repo", "C:/Program Files/my repo"),
    ("file:///srv/my repo", "/srv/my repo"),
    # A network share keeps its server: dropping it would leave drive-relative \\share\\repo.
    ("file://server/share/repo", "\\\\server\\share\\repo"),
    ("file://fileserver.example/team/repo", "\\\\fileserver.example\\team\\repo"),
])
def test_file_urls_name_the_local_path_they_describe(url, expected):
    from devflow_ai.independent_evaluation import local_path_from_file_url

    resolved = local_path_from_file_url(url)

    assert resolved is not None
    # Compare separator-insensitively: the same URL is a UNC path on Windows and a literal
    # string here, and this test is about the parsing, not about the platform's separators.
    assert str(resolved).replace("/", "\\") == expected.replace("/", "\\")


def test_plain_windows_and_unc_paths_are_local():
    from devflow_ai.independent_evaluation import local_repository_path

    assert local_repository_path("C:/repos/click") == Path("C:/repos/click")
    assert local_repository_path("D:\\repos\\click") == Path("D:\\repos\\click")
    assert local_repository_path("relative/repo") == Path("relative/repo")
    assert str(local_repository_path("\\\\server\\share\\repo")) == "\\\\server\\share\\repo"


@pytest.mark.skipif(os.name != "nt", reason="UNC path comparison is Windows behaviour")
def test_material_inside_a_network_share_repository_is_refused():
    """Windows-only: the share path is compared with the platform's own path rules."""
    from devflow_ai.independent_evaluation import (
        MaterialPlacementError, local_repository_path, require_material_placement,
    )

    repository = "file://server/share/repo"
    checkout = local_repository_path(repository)
    assert str(checkout) == "\\\\server\\share\\repo"

    with pytest.raises(MaterialPlacementError, match="outside the source repository"):
        require_material_placement(
            checkout / "material.json", pack_dir="C:/packs/click",
            workspaces_root="C:/work/workspaces", artifacts_root="C:/work/artifacts",
            repository=repository)


@pytest.mark.skipif(os.name == "nt", reason="POSIX behaviour; Windows uses UNC instead")
def test_a_file_url_with_a_host_is_not_silently_flattened_on_posix():
    """On POSIX the share form is still reported as a path rather than dropped to a basename."""
    from devflow_ai.independent_evaluation import local_repository_path

    assert local_repository_path("file://server/share/repo") is not None


# --- the formal commit and the lease renewal race --------------------------------------

def _fast_worker(factory, worker_id="fast-heartbeat"):
    from devflow_ai.config import LeaseConfig
    return Worker(factory, worker_id,
                  LeaseConfig(heartbeat_interval_seconds=1, lease_duration_seconds=30))


def test_a_committed_pass_survives_a_heartbeat_tick_in_the_scheduler_entry(
    worker_session_factory, tmp_path, monkeypatch,
):
    """The trusted entry commits PASSED; the next renewal must not turn that into an error."""
    factory = worker_session_factory
    material = _material_file(tmp_path)
    registered = _Registered(tmp_path, factory, monkeypatch, "a", material=material)
    manifest = _manifest(tmp_path, [registered.entry()])
    trusted = _TrustedEntry(factory, settle_seconds=1.4)
    trusted.install(monkeypatch)

    def run_once(request, **kwargs):
        def drive(_directories, _stop):
            _write_draft(factory, registered.task_id)

        return run_worker_once(
            request, session_factory=kwargs["session_factory"],
            worker=_fast_worker(factory), gateways=kwargs["gateways"],
            heartbeat_config=kwargs["heartbeat_config"], prepared=kwargs["prepared"],
            stop_event=kwargs["stop_event"], drive=drive)

    with _gateways(factory, "scheduler-a") as gateways:
        summary = run_scheduler(
            _request(tmp_path, manifest), session_factory=factory, worker_id="scheduler-a",
            gateways=gateways, heartbeat_config=_heartbeat(),
            prepare=_formal_prepare(factory), run_once=run_once)

    entry = summary["entries"][0]
    assert entry["outcome"] != "failed", entry
    assert entry["detail"] is None
    assert entry["finalization"]["outcome"] == "finalized"
    assert entry["run_status"] == RunStatus.PASSED.value


def test_a_committed_pass_survives_a_heartbeat_tick_in_the_benchmark_entry(
    worker_session_factory, tmp_path, monkeypatch,
):
    from devflow_ai.benchmark_run import BenchmarkRunRequest, load_independent_material, run_one_task

    factory = worker_session_factory
    material_path = _material_file(tmp_path)
    registered = _Registered(tmp_path, factory, monkeypatch, "a", material=material_path)
    trusted = _TrustedEntry(factory, settle_seconds=1.4)
    trusted.install(monkeypatch)
    material, _ = load_independent_material(
        material_path, expected_suite_version=SUITE_VERSION,
        expected_sha256=sha256(material_path.read_bytes()).hexdigest())
    for name in ("workspaces", "artifacts"):
        (tmp_path / name).mkdir(exist_ok=True)
    request = BenchmarkRunRequest(
        pack_dir=registered.pack, task_id=registered.task_id, operator="operator-a",
        python_executable=sys.executable, workspaces_root=tmp_path / "workspaces",
        artifacts_root=tmp_path / "artifacts", record_path=tmp_path / "benchmark-race.json",
        material_path=material_path)

    with _gateways(factory, "fast-heartbeat") as gateways:
        result = run_one_task(
            request, session_factory=factory, worker=_fast_worker(factory), gateways=gateways,
            heartbeat_config=_heartbeat(),
            max_patch_bytes=65536, verifier_max_diff_bytes=262144,
            drive=lambda _directories, _stop: _write_draft(factory, registered.task_id),
            independent_evaluation=material)

    # The call returned instead of raising the heartbeat's denial.
    assert result["finalization"]["outcome"] == "finalized"
    with factory() as session:
        assert session.scalars(select(Run).where(
            Run.task_id == registered.task_id)).one().status is RunStatus.PASSED
