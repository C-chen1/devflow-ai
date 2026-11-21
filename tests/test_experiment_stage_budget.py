"""The stage driver honours one explicitly configured experiment budget across the roles.

This drives ``orchestration.drive_run_stages`` with gateways built by
``model_gateways_from_env(..., experiment_budget=hook)``, so what runs is the real stage
leaves and supervision builders, not five direct calls to five gateways. Every model call is a
stub; what is real is the database: each request's experiment reservation, its Run reservation
and the usage recorded on the AgentExecution.

The graph itself decides how far it gets. With a verification configuration the stages go
Supervisor -> Explorer -> Supervisor -> Planner -> Supervisor -> Verifier(baseline) -> Coder ->
Verifier(public check + semantic review). Where the trusted baseline report cannot be captured
(this sandbox is Linux), the baseline Verifier is finished as ``BASELINE_UNAVAILABLE`` and the
driver stops before the Coder, so the Coder and semantic Verifier roles are **not** routed. The
tests say which roles were routed instead of pretending all five ran.

Nothing here measures a provider, proves a candidate's attribution or shows that a real task
succeeded; see ``docs/RSI_READINESS.md`` for those limits.
"""
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import select, text

from devflow_ai.coordinator import ModelTokenUsage
from devflow_ai.enums import TaskStatus
from devflow_ai.experiment import ExperimentService
from devflow_ai.experiment_budget import ExperimentBudgetHook
from devflow_ai.model_gateway import ModelReply
from devflow_ai.model_gateways import model_gateways_from_env
from devflow_ai.models import (
    AgentExecution, ExperimentReservation, Run, Task, ToolCall,
)
from devflow_ai.orchestration import drive_run_stages
from devflow_ai.services import AdmissionRecord, TaskService
from devflow_ai.verification_config import VerificationConfig
from devflow_ai.worker import RunDirectories, Worker

BASE_POLICY_TOOLS = {
    "EXPLORER": ["read_file"],
    "CODER": ["apply_patch"],
    "VERIFIER": ["run_command", "git_diff", "read_file"],
}
SCENARIO = (
    "from app import value\n\n\ndef test_value():\n    assert value() == 1\n"
)
PATCH = ("--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n"
         " def value():\n-    return 0\n+    return 1\n")


@pytest.fixture(autouse=True)
def _deterministic_checkout(monkeypatch):
    """Keep the tracked checkout byte-for-byte stable, on Windows as well as Linux."""
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")


@pytest.fixture
def stage_session_factory(worker_session_factory):
    """Add the experiment tables to this test's isolated schema."""
    engine = worker_session_factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    with engine.begin() as connection:
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        with Operations.context(MigrationContext.configure(connection)):
            ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations")) \
                .get_revision("d9b3c7e1a250").module.upgrade()
    return worker_session_factory


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8", timeout=60).stdout.strip()


def _workspace(tmp_path):
    """A small tracked repository whose public check fails until the patch is applied."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("def value():\n    return 0\n", encoding="utf-8", newline="\n")
    (workspace / "README.md").write_text("fixture repository\n", encoding="utf-8")
    _git(workspace, "init", "--quiet")
    _git(workspace, "add", "app.py", "README.md")
    _git(workspace, "-c", "user.name=Stage Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "baseline")
    return workspace, _git(workspace, "rev-parse", "HEAD")


def _pack(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "scenarios.py").write_text(SCENARIO, encoding="utf-8", newline="\n")
    return pack


def _task(factory, *, repository, base_commit):
    with factory() as session:
        task, _ = TaskService(session).create_task(
            title="Stage budget task", description="Constructed stage-driver fixture",
            repo=repository, idempotency_key=uuid4())
        task_id = task.id
    with factory() as session:
        TaskService(session).apply_admission_decision(
            task_id, TaskStatus.ELIGIBLE, base_commit=base_commit,
            admission_record=AdmissionRecord(reason="fixture", evidence=["dev-task:fixture"]))
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {
            **snapshot,
            "budget": {**snapshot["budget"], "tool_call_timeout_seconds": 20},
            "permissions": {"allowed_tools_by_role": {
                **snapshot["permissions"]["allowed_tools_by_role"], **BASE_POLICY_TOOLS}},
        }
    return task_id


def _experiment(factory, task_id, *, tokens=100_000):
    with factory.begin() as session:
        experiment_id = ExperimentService(session).open(
            identity_key=f"stage-{uuid4()}", base_execution_sha256="b" * 64,
            task_ids=[task_id],
            limits=__import__("devflow_ai.experiment", fromlist=["ExperimentLimits"]).ExperimentLimits(
                max_candidates=2, max_total_model_tokens=tokens, max_total_seconds=3600,
                promotion_min_formal_passes=1, promotion_max_failures=0),
            created_by="operator-a")
    with factory.begin() as session:
        ExperimentService(session).admit_candidate(
            experiment_id=experiment_id, label="candidate-a", execution_sha256="1" * 64)
    return experiment_id


def _hook(factory, experiment_id):
    return ExperimentBudgetHook(session_factory=factory, experiment_id=experiment_id,
                                candidate_label="candidate-a", operator="operator-a")


def _config(task_id, base_commit, pack):
    return VerificationConfig(
        task_id=task_id, base_commit=base_commit, command_name="pytest",
        command=(sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                 "scenarios.py"),
        target_tests=("scenarios::test_value",), pythonpath_relative=".",
        external_connections="none", public_tests_root=str(pack))


def _role_of(prompt: str) -> str:
    for name in ("Supervisor", "Explorer", "Planner", "Coder", "Verifier"):
        if f"You are DevFlow AI's {name}" in prompt:
            return name.upper()
    raise AssertionError("unrecognised prompt")


def _script(calls, *, usage=(10, 5), supervisor_decisions=None):
    """One generate function for all five roles, dispatched by the prompt's role line."""
    decisions = iter(supervisor_decisions or [])
    explorer_calls = []
    verifier_calls = []

    def generate(prompt, _limit):
        role = _role_of(prompt)
        calls.append(role)
        token_usage = None if usage is None else ModelTokenUsage(
            input_tokens=usage[0], output_tokens=usage[1])
        if role == "SUPERVISOR":
            return ModelReply(next(decisions), token_usage)
        if role == "EXPLORER":
            explorer_calls.append(prompt)
            if len(explorer_calls) == 1:
                return ModelReply({"path": "README.md"}, token_usage)
            data = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
            return ModelReply({"summary": "The repository holds app.py.",
                               "read_evidence": {"path": "README.md",
                                                 "tool_call_id": data["read"]["tool_call_id"]}},
                              token_usage)
        if role == "PLANNER":
            return ModelReply({"outcome": "PLAN_READY", "plan": {
                "approach": "Return 1 from value()", "target_paths": ["app.py"],
                "steps": ["Change the return value"], "scope_limits": "Only app.py",
                "verification": ["Run the public check"]}}, token_usage)
        if role == "CODER":
            return ModelReply({"outcome": "PATCH_DRAFT", "patch": PATCH,
                               "summary": "Return 1"}, token_usage)
        verifier_calls.append(prompt)
        if len(verifier_calls) == 1:
            return ModelReply({"outcome": "READ_FILE", "path": "app.py"}, token_usage)
        data = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
        return ModelReply({"outcome": "REVIEW", "result": "passed",
                           "reason": "The value is now 1",
                           "citations": [{"source": "read_file",
                                          "path": "app.py",
                                          "tool_call_id": data["reads"][0]["tool_call_id"],
                                          "snippet": "return 1",
                                          "reason": "The change is present"}]}, token_usage)

    return generate


def _decisions():
    return [
        {"outcome": "EXPLORE", "reason": "Need repository evidence",
         "exploration_focus": "Inspect the repository"},
        {"outcome": "PLAN", "reason": "Evidence is enough", "next_goal": "Plan the fix"},
        {"outcome": "BASELINE", "reason": "No baseline is recorded yet",
         "next_goal": "Record the public check"},
        {"outcome": "CODE", "reason": "Baseline recorded", "next_goal": "Change the value"},
        {"outcome": "VERIFY", "reason": "Patch prepared", "next_goal": "Verify the change"},
    ]


@dataclass(frozen=True)
class Fixture:
    workspace: Path
    artifacts: Path
    task_id: object
    base_commit: str
    config: VerificationConfig

    @property
    def directories(self):
        return RunDirectories(self.workspace, self.artifacts)


@contextmanager
def _drive(factory, fixture, *, experiment_id, calls, decisions, usage=(10, 5),
           worker_id="stage-worker"):
    """Claim one Run and hand back the five-role gateways built by the assembly."""
    worker = Worker(factory, worker_id)
    run_id = worker.claim_once().run_id
    hook = _hook(factory, experiment_id) if experiment_id is not None else None
    with model_gateways_from_env(factory, worker_id,
                                 generate=_script(calls, supervisor_decisions=decisions,
                                                  usage=usage),
                                 max_output_tokens=100, experiment_budget=hook) as gateways:
        yield worker, run_id, gateways


def _drive_stages(worker, run_id, gateways, directories, config):
    return drive_run_stages(
        worker.session_factory, run_id, worker=worker, gateways=gateways,
        directories=directories, stop_event=Event(), verification_config=config,
        max_stages=12, max_read_bytes=4096, max_patch_bytes=4096,
        verifier_max_diff_bytes=65536)


def _reservations(factory, experiment_id):
    with factory() as session:
        return session.scalars(select(ExperimentReservation).where(
            ExperimentReservation.experiment_id == experiment_id).order_by(
            ExperimentReservation.created_at)).all()


def _run_entries(factory, run_id):
    with factory() as session:
        executions = session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == run_id)).all()
        return {execution.id: dict((execution.llm_usage or {}).get("model_requests", {}))
                for execution in executions}


def _prepare_fixture(factory, tmp_path) -> Fixture:
    """One workspace, one pack, one admitted Task and the Run paths it will use."""
    workspace, base_commit = _workspace(tmp_path)
    pack = _pack(tmp_path)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    task_id = _task(factory, repository=str(workspace), base_commit=base_commit)
    return Fixture(workspace=workspace, artifacts=artifacts, task_id=task_id,
                   base_commit=base_commit, config=_config(task_id, base_commit, pack))


def test_every_stage_request_is_reserved_in_one_experiment_and_the_run(
        stage_session_factory, tmp_path):
    factory = stage_session_factory
    fixture = _prepare_fixture(factory, tmp_path)
    experiment_id = _experiment(factory, fixture.task_id)
    calls = []

    with _drive(factory, fixture, experiment_id=experiment_id, calls=calls,
                decisions=iter(_decisions())) as (worker, run_id, gateways):
        result = _drive_stages(worker, run_id, gateways, fixture.directories, fixture.config)

    # The driver routed these roles; a role that never ran is not claimed here.
    routed = sorted(set(calls))
    assert {"SUPERVISOR", "EXPLORER", "PLANNER"} <= set(routed)
    with factory() as session:
        baseline = session.execute(select(AgentExecution.output).where(
            AgentExecution.run_id == run_id,
            AgentExecution.agent_role == "verifier")).first()
    baseline_recorded = baseline is not None and baseline.output.get("outcome") == "BASELINE_RECORDED"
    if os.name == "nt":
        assert baseline_recorded, "Windows trusted baseline was not recorded"
        assert set(routed) == {"SUPERVISOR", "EXPLORER", "PLANNER", "CODER", "VERIFIER"}
        assert result["reason"] == "verification_blocked"
    else:
        # This platform cannot record the trusted baseline report, so the graph stops before
        # the Coder. Saying so is the point; the Coder and semantic Verifier did not run.
        assert set(routed) == {"SUPERVISOR", "EXPLORER", "PLANNER"}
        assert result["reason"] == "last_stage_not_succeeded"

    # Every model request is one experiment reservation, settled, inside this Run.
    reservations = _reservations(factory, experiment_id)
    assert len(reservations) == len(calls)
    assert {row.state for row in reservations} == {"settled"}
    assert {str(row.run_id) for row in reservations} == {str(run_id)}
    # Each experiment reservation names the execution that spent it, and those are exactly the
    # executions the Run charged. A stage that had used an unhooked gateway would show up as a
    # model call with no reservation.
    assert {str(row.agent_execution_id) for row in reservations} == {
        str(execution_id) for execution_id, usage in _run_entries(factory, run_id).items()
        if usage}
    entries = _run_entries(factory, run_id)
    recorded = [entry for usage in entries.values() for entry in usage.values()]
    assert len(recorded) == len(calls)
    assert {entry["actual_usage"] is not None for entry in recorded} == {True}
    assert sum(row.actual_tokens for row in reservations) == (
        sum(entry["actual_usage"]["input_tokens"] + entry["actual_usage"]["output_tokens"]
            for entry in recorded))
    with factory() as session:
        summary = ExperimentService(session).summary(experiment_id)
    assert summary["usage"]["accounting_complete"] is True
    assert summary["usage"]["dispatched_unsettled"] == 0
    # The original per-Run ceiling is still the inner one and was not exceeded.
    with factory() as session:
        snapshot = session.scalar(select(Task.policy_snapshot).where(Task.id == fixture.task_id))
    charged = sum(entry["actual_usage"]["input_tokens"] + entry["actual_usage"]["output_tokens"]
                  for entry in recorded)
    assert charged <= snapshot["budget"]["max_llm_tokens_per_run"]
    # Execution right and the Run's own records were untouched by the extra layer.
    with factory() as session:
        run = session.get(Run, run_id)
        assert run.worker_id == "stage-worker" and run.status.value == "RUNNING"
        # The stages really used the Harness (tool calls exist) and all of them belong here.
        calls_for_run = session.scalars(select(ToolCall.id).where(
            ToolCall.run_id == run_id)).all()
        assert calls_for_run


def test_the_experiment_ceiling_stops_the_first_stage_before_any_request(
        stage_session_factory, tmp_path):
    factory = stage_session_factory
    fixture = _prepare_fixture(factory, tmp_path)
    experiment_id = _experiment(factory, fixture.task_id, tokens=1)
    calls = []

    with _drive(factory, fixture, experiment_id=experiment_id, calls=calls,
                decisions=iter(_decisions())) as (worker, run_id, gateways):
        result = _drive_stages(worker, run_id, gateways, fixture.directories, fixture.config)

    assert calls == []
    assert _reservations(factory, experiment_id) == []
    assert all(usage == {} for usage in _run_entries(factory, run_id).values())
    assert result["outcome"] == "stopped"
    with factory() as session:
        assert session.scalar(select(Run.status).where(Run.id == run_id)).value == "RUNNING"


def test_an_unknown_usage_stops_the_following_stages(stage_session_factory, tmp_path):
    factory = stage_session_factory
    fixture = _prepare_fixture(factory, tmp_path)
    experiment_id = _experiment(factory, fixture.task_id)
    calls = []

    with _drive(factory, fixture, experiment_id=experiment_id, calls=calls,
                decisions=iter(_decisions()), usage=None) as (worker, run_id, gateways):
        result = _drive_stages(worker, run_id, gateways, fixture.directories, fixture.config)

    # The first supervisor request was answered without usage; nothing after it may be sent.
    assert calls == ["SUPERVISOR"]
    reservations = _reservations(factory, experiment_id)
    assert [row.state for row in reservations] == ["unknown"]
    assert reservations[0].actual_tokens is None
    with factory() as session:
        summary = ExperimentService(session).summary(experiment_id)
    assert summary["usage"]["accounting_complete"] is False
    assert result["outcome"] == "stopped"
    entries = [entry for usage in _run_entries(factory, run_id).values()
               for entry in usage.values()]
    assert entries and all(entry["actual_usage"] is None for entry in entries)


def test_a_failed_experiment_settlement_stops_the_following_stages(
        stage_session_factory, tmp_path, monkeypatch):
    factory = stage_session_factory
    fixture = _prepare_fixture(factory, tmp_path)
    experiment_id = _experiment(factory, fixture.task_id)
    calls = []

    def failing_settle(self, **kwargs):
        raise RuntimeError("settlement unavailable")

    monkeypatch.setattr(ExperimentService, "settle", failing_settle)
    with _drive(factory, fixture, experiment_id=experiment_id, calls=calls,
                decisions=iter(_decisions())) as (worker, run_id, gateways):
        with pytest.raises(RuntimeError, match="settlement unavailable"):
            _drive_stages(worker, run_id, gateways, fixture.directories, fixture.config)

    assert calls == ["SUPERVISOR"]
    with factory() as session:
        summary = ExperimentService(session).summary(experiment_id)
    # The call may have happened and was never settled: it keeps blocking the experiment.
    assert summary["usage"]["dispatched_unsettled"] == 1
    assert summary["usage"]["accounting_complete"] is False
    entries = [entry for usage in _run_entries(factory, run_id).values()
               for entry in usage.values()]
    assert entries and all(entry["actual_usage"] is None for entry in entries)


def test_without_a_hook_the_driver_keeps_the_v1_behaviour(stage_session_factory, tmp_path):
    factory = stage_session_factory
    fixture = _prepare_fixture(factory, tmp_path)
    calls = []

    with _drive(factory, fixture, experiment_id=None, calls=calls,
                decisions=iter(_decisions())) as (worker, run_id, gateways):
        _drive_stages(worker, run_id, gateways, fixture.directories, fixture.config)

    # Same stages, same Run budget, no experiment rows: the assembly was not given a hook.
    assert {"SUPERVISOR", "EXPLORER", "PLANNER"} <= set(calls)
    with factory() as session:
        assert session.scalars(select(ExperimentReservation)).all() == []
    entries = [entry for usage in _run_entries(factory, run_id).values()
               for entry in usage.values()]
    assert len(entries) == len(calls) and all(
        entry["actual_usage"] is not None for entry in entries)


def test_the_run_policy_budget_still_refuses_before_the_provider(stage_session_factory, tmp_path):
    factory = stage_session_factory
    fixture = _prepare_fixture(factory, tmp_path)
    # The experiment has room; the Run does not. The inner ceiling must still win.
    with factory.begin() as session:
        task = session.get(Task, fixture.task_id)
        snapshot = dict(task.policy_snapshot)
        snapshot["budget"] = {**snapshot["budget"], "max_llm_tokens_per_run": 1}
        task.policy_snapshot = snapshot
    experiment_id = _experiment(factory, fixture.task_id)
    calls = []

    with _drive(factory, fixture, experiment_id=experiment_id, calls=calls,
                decisions=iter(_decisions())) as (worker, run_id, gateways):
        result = _drive_stages(worker, run_id, gateways, fixture.directories, fixture.config)

    assert calls == []
    reservations = _reservations(factory, experiment_id)
    assert [row.state for row in reservations] == ["released"]
    assert reservations[0].ended_reason
    assert all(usage == {} for usage in _run_entries(factory, run_id).values())
    assert result["outcome"] == "stopped"
