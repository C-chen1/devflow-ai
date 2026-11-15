"""Scheduler → a real Verifier draft → the real Docker evaluation → the formal transaction.

This is the whole chain once, with nothing about the trusted step stubbed:

* the model is stubbed (deterministic decisions, no provider calls);
* the Git repositories, the baseline run, the patched public check and the semantic review
  draft are **real** — the draft is produced by the existing Verifier stage;
* the independent evaluation runs the constructed cases in a real container and the formal
  transaction is the existing one.

Two marks are required and both are honoured: the trusted baseline needs a Windows Job
report capture, and the evaluation needs an explicitly enabled Docker probe. When either is
absent the test is skipped — a skip is not a pass, and none of this is evidence that a real
task was solved.
"""
import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from devflow_ai.benchmark_run import load_independent_material
from devflow_ai.config import HeartbeatConfig
from devflow_ai.coordinator import ModelTokenUsage
from devflow_ai.enums import AgentExecutionStatus, AgentRole, RunStatus, TaskStatus, VerificationResult
from devflow_ai.model_gateway import ModelGateway, ModelReply, PlannerModelGateway
from devflow_ai.models import AgentExecution, Run, Task, Verification
from devflow_ai.orchestration import run_handoff_cycle
from devflow_ai.policy import build_default_policy
from devflow_ai.services import AdmissionRecord, TaskService
from devflow_ai.worker_run import WorkerRunExecutionError, run_worker_once
from devflow_ai.worker_scheduler import SchedulerRequest, run_scheduler


IMAGE = "python:3.11-slim"
RECORD_NAME = "records/final.json"
SUITE_VERSION = "scheduler-chain-v1"
BASE_ANSWER = "old\n"
FIXED_ANSWER = "new\n"
BASE_NOTES = "notes\n"
PATCHED_NOTES = "notes patched\n"
# The candidate adapter prints the behavioural file; the public test only looks at notes.txt,
# so the failing case below can pass the public check while the trusted suite still fails.
ADAPTER = "import pathlib, sys; sys.stdout.write(pathlib.Path('/candidate/answer.txt').read_text())"
PUBLIC_TEST = """from pathlib import Path


def test_notes_are_patched():
    assert "patched" in Path("notes.txt").read_text(encoding="utf-8")


def test_control_has_an_answer():
    assert Path("answer.txt").read_text(encoding="utf-8").strip()
"""
HEARTBEAT = HeartbeatConfig(statement_timeout_seconds=5, shutdown_timeout_seconds=5)


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                          check=True, timeout=20, text=True).stdout.strip()


def _commit_workspace(root):
    _git(root, "init", "--quiet")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "fixture")
    return _git(root, "rev-parse", "HEAD")


def _controlled_repositories(tmp_path):
    """A base commit with the defect and a corrected commit used for calibration."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "answer.txt").write_bytes(BASE_ANSWER.encode("utf-8"))
    (source / "notes.txt").write_bytes(BASE_NOTES.encode("utf-8"))
    base_commit = _commit_workspace(source)
    (source / "answer.txt").write_bytes(FIXED_ANSWER.encode("utf-8"))
    (source / "notes.txt").write_bytes(PATCHED_NOTES.encode("utf-8"))
    corrected_commit = _commit_workspace(source)

    base_checkout = tmp_path / "calibration" / "base"
    corrected_checkout = tmp_path / "calibration" / "corrected"
    subprocess.run(["git", "clone", "--quiet", str(source), str(base_checkout)],
                   check=True, capture_output=True, timeout=60)
    subprocess.run(["git", "clone", "--quiet", str(source), str(corrected_checkout)],
                   check=True, capture_output=True, timeout=60)
    _git(base_checkout, "checkout", "--quiet", base_commit)
    _git(corrected_checkout, "checkout", "--quiet", corrected_commit)
    return source, base_commit, corrected_commit, base_checkout, corrected_checkout


def _material_file(directory, *, base_checkout, corrected_checkout, corrected_commit):
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "suite_version": SUITE_VERSION,
        "image": IMAGE,
        "command": ["python", "-B", "-c", ADAPTER],
        "cases": [{"stdin_base64": "", "stdout_base64": _b64(FIXED_ANSWER.encode())}],
        "calibration": {
            "baseline_workspace": str(base_checkout),
            "corrected_workspace": str(corrected_checkout),
            "expected_commit": corrected_commit,
        },
        "timeout_seconds": 30,
    }
    path = directory / "material.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode("ascii")


def _pack(tmp_path, base_commit, material):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "scenarios.py").write_text(PUBLIC_TEST, encoding="utf-8")
    manifest = {
        "pack_id": "scheduler-chain-pack", "schema_version": 1, "title": "Scheduler chain",
        "source": {"issue_url": "https://example.invalid/issues/1",
                   "repository_url": "https://example.invalid/repo",
                   "license": "MIT", "issue_reference": "demo#1"},
        "base_commit": base_commit,
        "environment": {"python_requires": ">=3.11", "dependencies": ["pytest"],
                        "platform_notes": "Windows Job report capture"},
        "issue": {"sanitized_problem": "The answer is stale.",
                  "observable_error": "answer.txt still holds the old value.",
                  "expected_behavior": "answer.txt holds the new value."},
        "scope": {"allowed_paths": ["answer.txt", "notes.txt"]},
        "public_checks": {"command": ["python", "-m", "pytest", "scenarios.py"], "command_name": "pytest",
                          "target_tests": ["scenarios::test_notes_are_patched"]},
        "baseline": {"status": "verified_here"},
        "external_connections": "none",
        "independent_suite": {
            "suite_version": SUITE_VERSION,
            "materials_sha256": sha256(material.read_bytes()).hexdigest(),
            "digest_scope": "bytes as delivered",
            "digest_method": "sha256 over those bytes",
            "provided_by": "trusted_control_plane",
            "declaration": "Provided by the trusted control plane for this test only.",
        },
        "unverified": [{"field": "policy", "reason": "Not pinned for this fixture."}],
    }
    (pack / "task.json").write_text(json.dumps(manifest), encoding="utf-8")
    return pack


def _task(session_factory, repo, base_commit):
    policy = build_default_policy()
    policy.policy_version = "scheduler-chain-v1"
    policy.budget.max_llm_tokens_per_run = 80_000
    policy.permissions.allowed_tools_by_role[AgentRole.EXPLORER] = ["read_file"]
    policy.permissions.allowed_tools_by_role[AgentRole.CODER] = ["read_file", "apply_patch"]
    policy.permissions.allowed_tools_by_role[AgentRole.VERIFIER] = [
        "run_command", "git_diff", "read_file"]
    with session_factory() as session:
        task, created = TaskService(session, policy_factory=lambda: policy).create_task(
            title="Refresh the answer", description="Make the answer match the notes",
            repo=str(repo), idempotency_key=uuid4())
        assert created
        task_id = task.id
    with session_factory() as session:
        TaskService(session).apply_admission_decision(
            task_id, TaskStatus.ELIGIBLE, base_commit=base_commit,
            admission_record=AdmissionRecord(reason="controlled fixture",
                                             evidence=["local_git_head"]))
    return task_id


def _policy_file(tmp_path, session_factory, task_id):
    with session_factory() as session:
        snapshot = dict(session.scalar(select(Task.policy_snapshot).where(Task.id == task_id)))
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    return path


def _request(tmp_path, manifest):
    for name in ("workspaces", "artifacts"):
        (tmp_path / name).mkdir(exist_ok=True)
    return SchedulerRequest(
        manifest_path=manifest, operator="operator-a", python_executable=sys.executable,
        workspaces_root=tmp_path / "workspaces", artifacts_root=tmp_path / "artifacts",
        attempt_kind="formal", poll_interval_seconds=0.05)


class _Stubs:
    """Deterministic decisions for every role; no provider is contacted."""

    def __init__(self, *, patch):
        self.patch = patch

    @staticmethod
    def _data(prompt):
        return json.loads(prompt.split("\nInput data JSON:\n", 1)[1])

    @staticmethod
    def _usage():
        return ModelTokenUsage(input_tokens=40, output_tokens=20)

    def model(self):
        def generate(prompt, _limit):
            if "InitialSupervisorDecision" in prompt:
                return ModelReply({"outcome": "EXPLORE", "reason": "Read the repository",
                                   "exploration_focus": "Locate the stale answer"}, self._usage())
            if "PostExplorerDecision" in prompt:
                return ModelReply({"outcome": "PLAN", "reason": "Evidence is available",
                                   "next_goal": "Plan the change"}, self._usage())
            if "PostPlannerDecision" in prompt:
                data = self._data(prompt)
                if not data.get("baseline_available"):
                    return ModelReply({"outcome": "BASELINE", "reason": "Record the baseline",
                                       "next_goal": "Run the public checks before the patch"},
                                      self._usage())
                return ModelReply({"outcome": "CODE", "reason": "The plan is in scope",
                                   "next_goal": "Apply the approved change"}, self._usage())
            if "PostCoderDecision" in prompt:
                return ModelReply({"outcome": "VERIFY", "reason": "The patch needs checks",
                                   "next_goal": "Verify the patch"}, self._usage())
            raise AssertionError("unexpected supervisor prompt")

        return generate

    def gateway(self, session_factory, worker_id):
        return ModelGateway(session_factory=session_factory, worker_id=worker_id,
                            estimate_input_tokens=lambda _: 100,
                            generate=self.model(), max_output_tokens=200)

    def explorer(self, session_factory, worker_id):
        def generate(prompt, _limit):
            if '"title": "ExplorerReadRequest"' in prompt:
                return ModelReply({"path": "answer.txt"}, self._usage())
            if '"title": "ExplorerReport"' in prompt:
                read = self._data(prompt)["read"]
                return ModelReply({"summary": "The answer requires a change",
                                   "read_evidence": {"path": read["path"],
                                                     "tool_call_id": read["tool_call_id"]}},
                                  self._usage())
            raise AssertionError("unexpected explorer prompt")

        return ModelGateway(session_factory=session_factory, worker_id=worker_id,
                            estimate_input_tokens=lambda _: 100,
                            generate=generate, max_output_tokens=200)

    def planner(self, session_factory, worker_id):
        def generate(_prompt, _limit):
            return ModelReply({"outcome": "PLAN_READY", "plan": {
                "approach": "Rewrite the answer and notes",
                "target_paths": ["answer.txt", "notes.txt"],
                "steps": ["Update answer.txt", "Update notes.txt"],
                "scope_limits": "Only the two files in scope",
                "verification": ["Run the public checks"]}}, self._usage())

        return PlannerModelGateway(session_factory=session_factory, worker_id=worker_id,
                                   estimate_input_tokens=lambda _: 100, generate=generate,
                                   max_output_tokens=200)

    def coder(self, session_factory, worker_id):
        def generate(_prompt, _limit):
            return ModelReply({"outcome": "PATCH_DRAFT", "patch": self.patch,
                               "summary": "Apply the approved change"}, self._usage())

        return ModelGateway(session_factory=session_factory, worker_id=worker_id,
                            estimate_input_tokens=lambda _: 100,
                            generate=generate, max_output_tokens=200)

    def verifier(self, session_factory, worker_id):
        def generate(prompt, _limit):
            data = self._data(prompt)
            diff = data["context"]["workspace_diff"]
            citation_path, snippet = (("answer.txt", "new") if "a/answer.txt" in self.patch
                                      else ("notes.txt", "notes patched"))
            return ModelReply({"outcome": "REVIEW", "result": "passed",
                               "reason": "The approved change is present",
                               "citations": [{
                                   "source": "workspace_diff", "path": citation_path,
                                   "snippet": snippet, "tool_call_id": diff["tool_call_id"],
                                   "diff_side": "added",
                                   "reason": "The requested value is added"}]}, self._usage())

        return ModelGateway(session_factory=session_factory, worker_id=worker_id,
                            estimate_input_tokens=lambda _: 100,
                            generate=generate, max_output_tokens=200)


def _drive_chain(factory, task_id, worker, verification_config, directories, stop, stubs):
    """Run the stages the way the driver would, and stop at the Verifier draft.

    The draft is produced by the existing Verifier stage: the baseline run, the patched
    public check and the semantic review are all real; only the model is stubbed.
    """
    def receivers(**kwargs):
        return worker.build_handoff_receivers(
            run_id, directories=directories, max_bytes=65536, stop_event=stop,
            explorer_gateway=kwargs["explorer"], planner_gateway=kwargs["planner"],
            coder_gateway=kwargs["coder"], verifier_gateway=kwargs["verifier"],
            verification_config=verification_config, max_patch_bytes=65536,
            verifier_max_diff_bytes=262144)

    with factory() as session:
        run_id = session.scalar(select(Run.id).where(Run.task_id == task_id))
    assert run_id is not None
    gateways = {"explorer": stubs.explorer(factory, worker.worker_id),
                "planner": stubs.planner(factory, worker.worker_id),
                "coder": stubs.coder(factory, worker.worker_id),
                "verifier": stubs.verifier(factory, worker.worker_id)}
    dispatch = receivers(**gateways)

    def latest(handoff_id):
        with factory() as session:
            entry = session.scalars(select(AgentExecution).where(
                AgentExecution.handoff_id == handoff_id)).one()
            return entry.id, entry.agent_role, entry.status, dict(entry.output or {})

    cycle = lambda supervise: run_handoff_cycle(
        factory, run_id, worker.worker_id, supervise=supervise, receivers=dispatch)

    explorer = latest(cycle(worker.build_initial_supervise(
        run_id, model_gateway=stubs.gateway(factory, worker.worker_id), stop_event=stop)))
    assert explorer[1] == AgentRole.EXPLORER.value.lower(), explorer
    planner = latest(cycle(worker.build_post_explorer_supervise(
        run_id, explorer_execution_id=explorer[0],
        model_gateway=stubs.gateway(factory, worker.worker_id), stop_event=stop)))
    assert planner[1] == AgentRole.PLANNER.value.lower(), planner
    baseline = latest(cycle(worker.build_post_planner_supervise(
        run_id, planner_execution_id=planner[0],
        model_gateway=stubs.gateway(factory, worker.worker_id), stop_event=stop)))
    assert baseline[3].get("outcome") == "BASELINE_RECORDED", baseline
    coder = latest(cycle(worker.build_post_planner_supervise(
        run_id, planner_execution_id=planner[0],
        model_gateway=stubs.gateway(factory, worker.worker_id), stop_event=stop)))
    assert coder[1] == AgentRole.CODER.value.lower() and coder[3].get("outcome") == "PATCH_READY", coder
    verifier = latest(cycle(worker.build_post_coder_supervise(
        run_id, coder_execution_id=coder[0], directories=directories,
        model_gateway=stubs.gateway(factory, worker.worker_id), max_patch_bytes=65536,
        stop_event=stop)))
    assert verifier[1] == AgentRole.VERIFIER.value.lower(), verifier
    assert verifier[3].get("preliminary_public_outcome") == "passed", json.dumps(verifier[3])
    return verifier


def _scheduler_run_once(factory, task_id, *, patch):
    """Drive the entry through the real stages, then let the entry finalize normally."""
    def run_once(request, **kwargs):
        prepared = kwargs["prepared"]
        stub = _Stubs(patch=patch)
        holder = {}

        def drive(directories, stop):
            holder["verifier"] = _drive_chain(
                factory, task_id, kwargs["worker"], prepared.config, directories, stop, stub)

        try:
            result = run_worker_once(request, drive=drive, **kwargs)
        except WorkerRunExecutionError as error:
            raise error.error from error
        result["verifier_output"] = holder.get("verifier", (None, None, None, {}))[3]
        return result

    return run_once


def _verifications(factory, run_id):
    with factory() as session:
        return session.scalars(select(Verification).where(
            Verification.run_id == UUID(str(run_id)))).all()


def _schedule(request, factory, *, run_once):
    def gateways(_factory, _worker_id):  # the stubs are built inside run_once
        from contextlib import nullcontext
        return nullcontext(object())

    return run_scheduler(request, session_factory=factory, worker_id="scheduler-chain",
                         gateways=gateways(factory, "unused"), heartbeat_config=HEARTBEAT,
                         run_once=run_once)


def _prepare_fixture(factory, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    _, base_commit, corrected_commit, base_checkout, corrected_checkout = \
        _controlled_repositories(tmp_path)
    material = _material_file(tmp_path / "trusted-control", base_checkout=base_checkout,
                              corrected_checkout=corrected_checkout,
                              corrected_commit=corrected_commit)
    pack = _pack(tmp_path, base_commit, material)
    task_id = _task(factory, tmp_path / "source", base_commit)
    request = _request(tmp_path, tmp_path / "manifest.json")
    (tmp_path / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "tasks": [{"task_id": str(task_id), "pack": str(pack),
                                        "policy": str(_policy_file(tmp_path, factory, task_id)),
                                        "material": str(material),
                                        "record": str(tmp_path / RECORD_NAME)}]}),
        encoding="utf-8")
    return task_id, request, material


POSITIVE_PATCH = (
    "diff --git a/answer.txt b/answer.txt\n--- a/answer.txt\n+++ b/answer.txt\n"
    "@@ -1 +1 @@\n-old\n+new\n"
    "diff --git a/notes.txt b/notes.txt\n--- a/notes.txt\n+++ b/notes.txt\n"
    "@@ -1 +1 @@\n-notes\n+notes patched\n")
# Passes the public check (notes.txt is patched) but leaves the behaviour unchanged.
COSMETIC_PATCH = (
    "diff --git a/notes.txt b/notes.txt\n--- a/notes.txt\n+++ b/notes.txt\n"
    "@@ -1 +1 @@\n-notes\n+notes patched\n")


@pytest.mark.skipif(os.environ.get("DEVFLOW_TEST_EVALUATION_CONTAINER") != "1",
                    reason="requires an explicitly enabled Docker probe")
@pytest.mark.skipif(os.name != "nt",
                    reason="trusted pytest baseline and public check need a Windows Job")
def test_the_scheduler_reaches_a_formal_pass_through_the_real_evaluation(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    task_id, request, material = _prepare_fixture(factory, tmp_path, monkeypatch)
    loaded, _ = load_independent_material(
        material, expected_suite_version=SUITE_VERSION,
        expected_sha256=sha256(material.read_bytes()).hexdigest())
    assert loaded.image == IMAGE  # the fixture is what the trusted entry will receive

    summary = _schedule(request, factory, run_once=_scheduler_run_once(
        factory, task_id, patch=POSITIVE_PATCH))

    entry = summary["entries"][0]
    assert entry["finalization"] is not None, json.dumps(entry)
    assert entry["finalization"]["outcome"] == "finalized", (
        entry["finalization"], entry["detail"], entry["run_status"])
    assert entry["finalization"]["independent_outcome"] == "passed"
    assert entry["run_status"] == RunStatus.PASSED.value
    assert summary["formal_acceptance"] == "committed"

    with factory() as session:
        run = session.scalars(select(Run).where(Run.task_id == task_id)).one()
        task = session.get(Task, task_id)
        verifications = _verifications(factory, run.id)
    assert len(verifications) == 1
    verification = verifications[0]
    assert verification.result is VerificationResult.PASSED
    assert verification.evidence["scope"] == "trusted_v1"
    assert verification.evidence["suite_version"] == SUITE_VERSION
    assert task.status is TaskStatus.AWAITING_REVIEW

    # The attempt also leaves a reviewable record that cites the committed Verification.
    from devflow_ai.benchmark_record import load_run_record
    record_path = tmp_path / RECORD_NAME
    record = load_run_record(record_path)
    assert record.outcome.value == "formal_pass"
    assert record.formal is not None
    assert record.formal.verification_id == str(verification.id)
    assert record.task_id == str(task_id) and record.run_id == str(run.id)
    assert record.attempt_kind.value == "formal"
    assert record.human_review.conclusion == "pending"  # a person still has to decide
    original_record = record_path.read_bytes()

    # Reconstruct independently from the committed rows and actual Docker artifacts.
    from devflow_ai.record_backfill import BackfillRequest, backfill_record
    pack_dir = Path(json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
                    ["tasks"][0]["pack"])
    backfilled_path = tmp_path / "records" / "backfilled.json"
    backfill_record(BackfillRequest(task_id=task_id, run_id=run.id, pack_dir=pack_dir,
                    artifacts_root=tmp_path / "artifacts", record_path=backfilled_path,
                    operator="test-operator"), session_factory=factory)
    backfilled = load_run_record(backfilled_path)
    assert backfilled.outcome.value == "formal_pass"
    assert backfilled.formal.verification_id == str(verification.id)
    assert backfilled.attempt_kind.value == "unknown"
    assert backfilled.human_review.conclusion == "pending"
    assert record_path.read_bytes() == original_record

    # A second scheduler call must not evaluate again or write a second result.
    again = _schedule(request, factory, run_once=_scheduler_run_once(
        factory, task_id, patch=POSITIVE_PATCH))
    assert again["entries"][0]["outcome"] == "existing_run"
    assert again["entries"][0]["record_written"] is False
    assert record_path.read_bytes() == original_record
    assert len(_verifications(factory, run.id)) == 1
    with factory() as session:
        assert len(session.scalars(select(Run).where(Run.task_id == task_id)).all()) == 1


@pytest.mark.skipif(os.environ.get("DEVFLOW_TEST_EVALUATION_CONTAINER") != "1",
                    reason="requires an explicitly enabled Docker probe")
@pytest.mark.skipif(os.name != "nt",
                    reason="trusted pytest baseline and public check need a Windows Job")
def test_an_independent_failure_does_not_commit_a_formal_result(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    task_id, request, _ = _prepare_fixture(factory, tmp_path, monkeypatch)

    summary = _schedule(request, factory, run_once=_scheduler_run_once(
        factory, task_id, patch=COSMETIC_PATCH))

    entry = summary["entries"][0]
    assert entry["finalization"] is not None, json.dumps(entry)
    assert entry["finalization"]["outcome"] == "finalized", (
        entry["finalization"], entry["detail"], entry["run_status"])
    assert entry["finalization"]["independent_outcome"] != "passed"
    assert summary["formal_acceptance"] == "not_performed"
    with factory() as session:
        run = session.scalars(select(Run).where(Run.task_id == task_id)).one()
        task = session.get(Task, task_id)
    assert run.status is RunStatus.RUNNING
    assert _verifications(factory, run.id) == []
    assert task.status is TaskStatus.RUNNING

    # The record is written, and it does not claim a formal pass.
    from devflow_ai.benchmark_record import load_run_record
    record = load_run_record(tmp_path / RECORD_NAME)
    assert record.outcome.value != "formal_pass"
    assert record.formal is None
    assert record.human_review.conclusion == "pending"


@pytest.mark.skipif(os.environ.get("DEVFLOW_TEST_EVALUATION_CONTAINER") != "1",
                    reason="requires an explicitly enabled Docker probe")
@pytest.mark.skipif(os.name != "nt",
                    reason="trusted pytest baseline and public check need a Windows Job")
@pytest.mark.parametrize("patch,expected", [
    (POSITIVE_PATCH, "passed"),
    (COSMETIC_PATCH, "failed"),
])
def test_default_scheduler_driver_handles_independent_outcomes(
    worker_session_factory, tmp_path, monkeypatch, patch, expected,
):
    """Drive real stages and evaluate both valid and public-only patches."""
    from devflow_ai.model_gateways import ModelGateways

    factory = worker_session_factory
    task_id, request, _ = _prepare_fixture(factory, tmp_path, monkeypatch)
    worker_id = "scheduler-default-driver"
    stubs = _Stubs(patch=patch)
    gateways = ModelGateways(
        supervisor=stubs.gateway(factory, worker_id),
        explorer=stubs.explorer(factory, worker_id),
        planner=stubs.planner(factory, worker_id),
        coder=stubs.coder(factory, worker_id),
        verifier=stubs.verifier(factory, worker_id),
    )

    summary = run_scheduler(request, session_factory=factory, worker_id=worker_id,
                            gateways=gateways, heartbeat_config=HEARTBEAT)
    entry = summary["entries"][0]
    assert entry["finalization"] is not None, json.dumps(entry)
    assert entry["finalization"]["independent_outcome"] == expected
    assert summary["formal_acceptance"] == ("committed" if expected == "passed" else "not_performed")
    with factory() as session:
        run = session.scalars(select(Run).where(Run.task_id == task_id)).one()
        assert run.status is (RunStatus.PASSED if expected == "passed" else RunStatus.RUNNING)
        assert session.get(Task, task_id).status is (
            TaskStatus.AWAITING_REVIEW if expected == "passed" else TaskStatus.RUNNING)
        assert len(_verifications(factory, run.id)) == (1 if expected == "passed" else 0)
        run_id = run.id

    replay = run_scheduler(request, session_factory=factory, worker_id=worker_id,
                           gateways=gateways, heartbeat_config=HEARTBEAT)
    assert replay["entries"][0]["outcome"] == "existing_run"
    with factory() as session:
        assert len(session.scalars(select(Run).where(Run.task_id == task_id)).all()) == 1
        assert len(_verifications(factory, run_id)) == (1 if expected == "passed" else 0)

    if expected == "passed":
        # Install only the review table into this test's isolated schema, then exercise the
        # actual review CLI with a synthetic human decision referring to the real Verification.
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from alembic.script import ScriptDirectory
        from sqlalchemy import text
        from devflow_ai.benchmark_record import BenchmarkRunRecord, write_run_record
        from devflow_ai.task_review import main as review_main

        engine = factory.kw["bind"]
        schema = engine.get_execution_options()["schema_translate_map"][None]
        scripts = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations"))
        with engine.begin() as connection:
            connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            with Operations.context(MigrationContext.configure(connection)):
                scripts.get_revision("b7f2c31d8a90").module.upgrade()
        with factory() as session:
            verification_id = _verifications(factory, run_id)[0].id
            task = session.get(Task, task_id)
            base_commit, policy_version = task.base_commit, task.policy_version
        record = BenchmarkRunRecord.model_validate({
            "pack_id": "scheduler-chain-pack", "base_commit": base_commit,
            "task_id": str(task_id), "run_id": str(run_id), "attempt_index": 1,
            "attempt_kind": "formal", "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:01:00Z", "outcome": "formal_pass",
            "model_id": "test-stub", "policy_version": policy_version, "operator": "test",
            "formal": {"verification_id": str(verification_id),
                       "independent_suite_version": SUITE_VERSION},
            "human_review": {"conclusion": "accepted", "reviewer": "test-reviewer",
                             "reviewed_at": "2026-01-01T00:02:00Z"},
            "artifacts": [str(tmp_path / "artifacts")],
        })
        review_path = tmp_path / "synthetic-reviewed-record.json"
        write_run_record(record, review_path)
        argv = ["--record", str(review_path), "--reviewer", "test-reviewer"]
        assert review_main(argv, session_factory=factory) == 0
        assert review_main(argv, session_factory=factory) == 0
        with factory() as session:
            assert session.get(Task, task_id).status is TaskStatus.COMPLETED


def test_the_stub_answers_every_prompt_it_will_receive():
    """Local guard for the dispatch table: the real prompt builders must select the stubs.

    This part needs no Windows Job and no Docker, so a broken stub table fails here instead
    of inside the gated chain above.
    """
    from devflow_ai.planner import build_planner_prompt
    from devflow_ai.explorer import ExplorerReadRequest, ExplorerReport
    from devflow_ai.supervisor import (
        build_initial_supervisor_prompt, build_post_coder_supervisor_prompt,
        build_post_explorer_supervisor_prompt, build_post_planner_supervisor_prompt,
    )
    from devflow_ai.verifier import build_semantic_review_prompt

    stubs = _Stubs(patch="")
    generate = stubs.model()

    def outcome(prompt):
        return generate(prompt, 100).output["outcome"]

    assert outcome(build_initial_supervisor_prompt({"repo": "repo"})) == "EXPLORE"
    assert outcome(build_post_explorer_supervisor_prompt({"read_evidence": []})) == "PLAN"
    assert outcome(build_post_planner_supervisor_prompt(
        {"baseline_available": False})) == "BASELINE"
    assert outcome(build_post_planner_supervisor_prompt(
        {"baseline_available": True})) == "CODE"
    assert outcome(build_post_coder_supervisor_prompt({"patch_artifact_id": "x"})) == "VERIFY"

    explorer = stubs.explorer(None, "stub")
    choice_prompt = "schema: " + json.dumps(ExplorerReadRequest.model_json_schema())
    assert explorer.generate(choice_prompt, 100).output["path"] == "answer.txt"
    call_id = str(uuid4())
    report_prompt = ("schema: " + json.dumps(ExplorerReport.model_json_schema())
                     + "\nInput data JSON:\n" + json.dumps({
                         "read": {"path": "answer.txt", "tool_call_id": call_id}}))
    assert explorer.generate(report_prompt, 100).output["read_evidence"]["tool_call_id"] == call_id

    planner = stubs.planner(None, "stub")
    planned = planner.generate(build_planner_prompt({"issue": "issue"}), 100).output
    assert planned["outcome"] == "PLAN_READY"
    assert planned["plan"]["target_paths"] == ["answer.txt", "notes.txt"]

    review = stubs.verifier(None, "stub").generate(build_semantic_review_prompt(
        {"workspace_diff": {"tool_call_id": "call-1"}}), 100).output
    assert review["outcome"] == "REVIEW" and review["result"] == "passed"
    assert review["citations"][0]["tool_call_id"] == "call-1"
