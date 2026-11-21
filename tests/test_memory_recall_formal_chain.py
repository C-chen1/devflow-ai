"""Constructed end-to-end path: formal acceptance -> memory -> recall from another Task.

This is the only test here that drives the *formal* chain instead of writing a PASSED Run: the
source Run is finished by ``Worker.finalize_with_independent_evaluation``, which runs the prepared
adapter image through the calibrated independent evaluation and only then commits a ``trusted_v1``
Verification. Two real prerequisites gate it:

* the Windows Job pytest capture — without it a formal attempt is refused before a Run exists
  (see ``tests/test_worker_run.py``, "a formal attempt is refused where the trusted baseline
  cannot be recorded");
* a locally built ``devflow-rehearsal-adapter:v1`` image (``docker build -t ... <root>/adapter``),
  with ``DEVFLOW_TEST_EVALUATION_CONTAINER=1`` opting into container tests.

What it establishes when it runs: the prepared material carries a *formal* pass into memory, the
other Task on the same repository and baseline recalls it, and that memory-assisted Run is then
refused independent evaluation while a registered check is still recorded as evidence.

What it does not establish: that a real model solved a real task, or that recall improved
anything. Every model call is a stub and the repository is the constructed rehearsal one.
"""
import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import select, text

from devflow_ai.coordinator import HandoffDecision, ModelTokenUsage, RunCoordinator
from devflow_ai.enums import AgentExecutionStatus, AgentRole, TaskStatus
from devflow_ai.independent_evaluation import PrivateCase
from devflow_ai.memory import (
    MemoryCandidate, MemoryRecallRequest, MemoryScopeError, MemoryService, RepositoryScope,
)
from devflow_ai.model_gateway import ModelGateway, ModelReply
from devflow_ai.models import AgentExecution, MemoryUse, Run, Task, ToolCall, Verification
from devflow_ai.services import AdmissionRecord, TaskService
from devflow_ai.skills import SkillNotApplicable, check_diff_scope, run_registered_check
from devflow_ai.task_pack import ScopeSpec
from devflow_ai.verification_config import (
    VerificationConfig, public_check_command, public_test_modules, public_tests_copy_root,
    public_tests_sha256,
)
from devflow_ai.verifier import VerificationEvidenceError, VerifierEvidenceSelection
from devflow_ai.worker import RunDirectories, Worker
from rehearsals.memory_recall import prepare as rehearsal

ENVIRONMENT = "dev-box-win11-py311"
FORMAL_CHAIN_AVAILABLE = (os.name == "nt"
                          and os.environ.get("DEVFLOW_TEST_EVALUATION_CONTAINER") == "1")
SKIP_REASON = ("needs the Windows Job pytest capture and a locally built evaluation image "
               "(DEVFLOW_TEST_EVALUATION_CONTAINER=1)")


@pytest.fixture
def worker_audit_session_factory(worker_session_factory):
    """Extend this test's isolated worker schema with the real memory migrations."""
    engine = worker_session_factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations"))
    with engine.begin() as connection:
        connection.execution_options(schema_translate_map=None)
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        with Operations.context(MigrationContext.configure(connection)):
            for revision in ("b7f2c31d8a90", "c4a7e2f19b30", "d18e42f70a91",
                             "ec624a31d740", "f6a92c3184d2", "a5810e46f2c9",
                             "b41e9d2c8370"):
                scripts.get_revision(revision).module.upgrade()
    return worker_session_factory


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8", timeout=60).stdout.strip()


def _admitted_task(factory, *, title, repository, base_commit, evidence="dev-task:operator-a:fixture"):
    with factory() as session:
        task, _ = TaskService(session).create_task(
            title=title, description="Constructed formal-chain fixture", repo=repository,
            idempotency_key=uuid4())
        task_id = task.id
    with factory() as session:
        TaskService(session).apply_admission_decision(
            task_id, TaskStatus.ELIGIBLE, base_commit=base_commit,
            admission_record=AdmissionRecord(reason="fixture", evidence=[evidence]))
    return task_id


def _allow(factory, task_id, *, tools, timeout_seconds=20):
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {
            **snapshot,
            "budget": {**snapshot["budget"], "tool_call_timeout_seconds": timeout_seconds},
            "permissions": {"allowed_tools_by_role": {
                **snapshot["permissions"]["allowed_tools_by_role"], **tools}},
        }


def _handoff(coordinator, run_id, worker_id, *, from_execution, to_role, decision, refs=()):
    supervisor = coordinator.start_agent_execution(
        run_id, worker_id, role=AgentRole.SUPERVISOR, agent_input={})
    return coordinator.finish_agent_execution(
        run_id, worker_id, agent_execution_id=supervisor, status=AgentExecutionStatus.SUCCEEDED,
        output={}, llm_usage=None,
        approved_handoff=HandoffDecision.model_validate({
            "from_agent_execution_id": from_execution, "to_agent_role": to_role,
            "context": {"completed_work": "stub step", "blocker": None,
                        "evidence_refs": list(refs), "decision_required": decision,
                        "scope_impact": "Only src/"},
        }))


@pytest.mark.skipif(not FORMAL_CHAIN_AVAILABLE, reason=SKIP_REASON)
def test_a_formal_pass_on_the_prepared_material_reaches_the_other_task(
        worker_audit_session_factory, tmp_path, monkeypatch):
    factory = worker_audit_session_factory
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")

    # 0. The prepared materials: one repository at C, two packs, one adapter image, one material.
    plan = rehearsal.prepare(tmp_path / "rehearsal")
    source_pack = Path(plan["tasks"]["source"]["pack"])
    base_commit = plan["repository"]["base_commit"]
    image = plan["adapter"]["image"]
    material = json.loads(Path(plan["material"]["path"]).read_text(encoding="utf-8"))
    calibration = plan["material"]["calibration"]
    cases = tuple(PrivateCase(stdin=base64.b64decode(case["stdin_base64"]),
                              expected_stdout=base64.b64decode(case["stdout_base64"]))
                  for case in material["cases"])
    calibration_workspaces = (Path(calibration["baseline_workspace"]),
                              Path(calibration["corrected_workspace"]),
                              calibration["corrected_commit"])

    # 1. Work on a clone of the prepared repository so the prepared root stays on C.
    workspace, artifacts = tmp_path / "workspace", tmp_path / "artifacts"
    workspace.mkdir()
    artifacts.mkdir()
    subprocess.run(["git", "clone", "-q", "--no-local", plan["repository"]["repo"], str(workspace)],
                   check=True, capture_output=True, timeout=60)
    assert _git(workspace, "rev-parse", "HEAD") == base_commit
    repository = str(workspace)

    source_id = _admitted_task(factory, title="Rehearsal source (formal)", repository=repository,
                               base_commit=base_commit)
    _allow(factory, source_id, tools={"EXPLORER": ["read_file"], "CODER": ["apply_patch"],
                                      "VERIFIER": ["run_command", "git_diff", "read_file"]})
    worker = Worker(factory, "formal-rehearsal-worker")
    run_id = worker.claim_once().run_id
    directories = RunDirectories(workspace, artifacts)
    stop = Event()

    # The public tests live in the pack, outside the checkout, so they are materialised into the
    # run's artifacts exactly as the worker's own entry does; command and digest come from one read.
    config = VerificationConfig(
        task_id=source_id, base_commit=base_commit, command_name="pytest",
        command=(sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                 "scenarios.py"),
        target_tests=("scenarios::test_empty_cart_costs_nothing",), pythonpath_relative=".",
        external_connections="none", independent_suite_version=material["suite_version"],
        public_tests_root=str(source_pack))
    modules = public_test_modules(config)
    copy_root = public_tests_copy_root(artifacts)
    copy_root.mkdir(parents=True, exist_ok=True)
    for name, data in modules.items():
        (copy_root / name).write_bytes(data)
    public_digest = public_tests_sha256(modules)
    registry = {"pytest": public_check_command(config, artifact_root=artifacts,
                                               modules=modules)}

    # 2. The formal chain, through the existing coordinator entry points only.
    with factory() as session:
        coordinator = RunCoordinator(session)
        planner = coordinator.start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.PLANNER, agent_input={})
        coordinator.finish_agent_execution(
            run_id, worker.worker_id, agent_execution_id=planner,
            status=AgentExecutionStatus.SUCCEEDED, llm_usage=None,
            output={"outcome": "PLAN_READY", "plan": {
                "approach": "Return 0 for an empty cart", "target_paths": ["src/pricing.py"],
                "steps": ["Guard the empty list", "Reproduce with the public check"],
                "scope_limits": "Only src/", "verification": ["Run the public check"]}},
        )
        baseline_handoff = _handoff(coordinator, run_id, worker.worker_id, from_execution=planner,
                                    to_role="VERIFIER", decision="Run the public check")
    baseline = worker.execute_baseline_verifier(
        run_id, handoff_id=baseline_handoff, directories=directories, command_name="pytest",
        registry=registry, stop_event=stop, pythonpath_relative=".", verify_baseline=True,
        public_tests_digest=public_digest)
    assert baseline["valid"] and baseline["receipt"]["status"] == "failed"
    baseline_verifier = baseline["execution_id"]
    baseline_call = UUID(baseline["receipt"]["tool_call_id"])
    baseline_report = UUID(next(item["id"] for item in baseline["receipt"]["artifacts"]
                                if item["kind"] == "pytest_junit"))

    patch = ("--- a/src/pricing.py\n+++ b/src/pricing.py\n@@ -3,4 +3,6 @@\n"
             " \n"
             " def average_price(prices):\n"
             '     """Return the average price; an empty cart costs nothing."""\n'
             "+    if not prices:\n"
             "+        return 0\n"
             "     return sum(prices) / len(prices)\n")
    with factory() as session:
        coordinator = RunCoordinator(session)
        coder_handoff = _handoff(coordinator, run_id, worker.worker_id, from_execution=planner,
                                 to_role="CODER", decision="Implement the guard",
                                 refs=[{"baseline_verifier_id": str(baseline_verifier)}])
    coder_gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id, estimate_input_tokens=lambda _: 100,
        max_output_tokens=200, generate=lambda _prompt, _limit: ModelReply(
            {"outcome": "PATCH_DRAFT", "patch": patch, "summary": "Guard the empty cart"},
            ModelTokenUsage(input_tokens=100, output_tokens=30)))
    coder = worker.execute_coder(run_id, handoff_id=coder_handoff, directories=directories,
                                 model_gateway=coder_gateway, max_patch_bytes=4096,
                                 max_read_bytes=4096, stop_event=stop)
    with factory() as session:
        coder_output = dict(session.get(AgentExecution, coder).output)
        patch_call = session.scalars(select(ToolCall.id).where(
            ToolCall.agent_execution_id == coder, ToolCall.tool_name == "apply_patch")).one()
        session.rollback()
        coordinator = RunCoordinator(session)
        verifier_handoff = _handoff(
            coordinator, run_id, worker.worker_id, from_execution=coder, to_role="VERIFIER",
            decision="Verify the fix",
            refs=[{"baseline_verifier_id": str(baseline_verifier),
                   "patch_tool_call_id": str(patch_call),
                   "patch_artifact_id": str(coder_output["patch_artifact_id"])}])
        verifier = coordinator.start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.VERIFIER, agent_input={},
            handoff_id=verifier_handoff)
    current = worker.build_verifier_command(
        run_id, agent_execution_id=verifier, directories=directories, registry=registry,
        pytest_command_names=frozenset({"pytest"}), pythonpath_relative=".", stop_event=stop,
        public_tests_digest=public_digest,
    )("pytest")
    assert (current["status"], current["exit_code"]) == ("succeeded", 0)
    diff = worker.build_verifier_diff(run_id, agent_execution_id=verifier, directories=directories,
                                      max_bytes=65536, stop_event=stop)()
    current_report = UUID(next(item["id"] for item in current["artifacts"]
                               if item["kind"] == "pytest_junit"))
    prompts = []

    def review(prompt, _limit):
        prompts.append(prompt)
        if len(prompts) == 1:
            return ModelReply({"outcome": "READ_FILE", "path": "src/pricing.py"},
                              ModelTokenUsage(input_tokens=100, output_tokens=20))
        context = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
        return ModelReply({"outcome": "REVIEW", "result": "passed",
                           "reason": "The empty cart now returns 0",
                           "citations": [{"source": "read_file",
                                          "tool_call_id": context["reads"][0]["tool_call_id"],
                                          "path": "src/pricing.py",
                                          "snippet": "if not prices:",
                                          "reason": "The guard is present"}]},
                          ModelTokenUsage(input_tokens=100, output_tokens=30))

    semantic_gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id, estimate_input_tokens=lambda _: 100,
        max_output_tokens=200, generate=review)
    result = worker.review_verifier_semantics(
        run_id, agent_execution_id=verifier, directories=directories,
        selection=VerifierEvidenceSelection(
            baseline_verifier_id=baseline_verifier, baseline_call_id=baseline_call,
            baseline_artifact_id=baseline_report, patch_call_id=patch_call,
            current_call_id=UUID(current["tool_call_id"]), current_artifact_id=current_report,
            diff_call_id=UUID(diff["tool_call_id"]), command_name="pytest",
            target_tests=("scenarios::test_empty_cart_costs_nothing",)),
        model_gateway=semantic_gateway, max_read_bytes=4096, max_diff_bytes=65536,
        max_patch_bytes=4096, stop_event=stop)
    assert result.comparison.target_result == "passed"
    assert result.review.result == "passed"

    finalized = worker.finalize_with_independent_evaluation(
        run_id, agent_execution_id=verifier, draft_artifact_id=result.draft_artifact_id,
        directories=directories, verification_config=config, image=image,
        command=("python", "/suite/adapter.py"), cases=cases,
        calibration_workspaces=calibration_workspaces, max_diff_bytes=65536,
        max_patch_bytes=4096, timeout_seconds=15, stop_event=stop)
    assert finalized["outcome"] == "passed", finalized
    with factory() as session:
        verification = session.get(Verification, finalized["verification_id"])
        assert verification.result.value == "passed"
        assert verification.evidence["scope"] == "trusted_v1"
        assert verification.evidence["suite_version"] == material["suite_version"]
        assert session.get(Run, run_id).status.value == "PASSED"

    # 3. Ingest one reviewed memory from that formal record.
    with factory() as session:
        ingested = MemoryService(session).ingest(MemoryCandidate(
            identity_key="formal-rehearsal-repo-layout",
            scope=RepositoryScope(repository=repository), source_kind="verification",
            task_id=source_id, run_id=run_id, verification_id=finalized["verification_id"],
            content="The code lives under src/ and the public check is a pytest pack outside the "
                    "checkout; a fix belongs to src/ only.",
            topic="repo-layout", valid_until=datetime.now(timezone.utc) + timedelta(days=30),
            applicable_base_commit=base_commit, applicable_environment=ENVIRONMENT,
            created_by="operator-a"))
    memory_id = UUID(ingested["id"])
    assert ingested["source_state"] == "trusted_passed"
    assert ingested["content_status"] == "unverified_candidate"
    with factory() as session:
        shown = MemoryService(session).read(scope=RepositoryScope(repository=repository),
                                           memory_id=memory_id)
    with factory() as session:
        MemoryService(session).review(memory_id=memory_id,
                                      scope=RepositoryScope(repository=repository),
                                      candidate_sha256=shown["candidate_sha256"],
                                      conclusion="accepted", reviewer="reviewer-a",
                                      reason="Constructed rehearsal text; no hidden material")

    # 4. The other Task, same repository string and baseline, recalls it.
    target_id = _admitted_task(factory, title="Rehearsal target", repository=repository,
                               base_commit=base_commit)
    _allow(factory, target_id, tools={"EXPLORER": ["read_file"],
                                      "VERIFIER": ["run_command", "git_diff", "read_file"]})
    target_worker = Worker(factory, "rehearsal-target-worker")
    target_run = target_worker.claim_once().run_id
    target_directories = RunDirectories(tmp_path / "target-workspace", tmp_path / "target-artifacts")
    target_directories.workspace_root.mkdir()
    target_directories.artifact_root.mkdir()
    (target_directories.workspace_root / "README.md").write_text("fixture code evidence\n",
                                                                 encoding="utf-8")
    turns = []

    def explore(prompt, _limit):
        assert "unverified past experience" in prompt
        data = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
        context = data if not turns else data["agent_input"]
        turns.append(context)
        if len(turns) == 1:
            result = {"path": "README.md"}
        else:
            result = {"summary": "The readme contains fixture code evidence.",
                      "read_evidence": {"path": "README.md",
                                        "tool_call_id": data["read"]["tool_call_id"]}}
        return ModelReply(json.dumps(result), ModelTokenUsage(input_tokens=10, output_tokens=5))

    explorer_gateway = ModelGateway(
        session_factory=factory, worker_id=target_worker.worker_id,
        estimate_input_tokens=lambda _: 100, generate=explore, max_output_tokens=200)
    execution_id = target_worker.execute_explorer(
        target_run, directories=target_directories,
        agent_input={"issue": "inspect the rehearsal checkout"}, max_bytes=4096, stop_event=Event(),
        model_gateway=explorer_gateway,
        memory_recall=MemoryRecallRequest(
            idempotency_key=uuid4(), memory_id=memory_id,
            candidate_sha256=shown["candidate_sha256"], operator="operator-a",
            purpose="constructed cross-task recall", target_environment=ENVIRONMENT))
    with factory() as session:
        execution = session.get(AgentExecution, execution_id)
        recalled = execution.input["memory_recall_v1"]
        assert recalled["content_status"] == "unverified_candidate"
        assert recalled["source_verification_id"] == str(finalized["verification_id"])
        assert recalled["source_state"] == "trusted_passed" and recalled["review_id"]
        assert session.get(MemoryUse, UUID(recalled["use_id"])) is not None

    # 5. A memory-assisted Run cannot claim independent evaluation...
    target_config = config.model_copy(update={"task_id": target_id})
    with factory() as session, pytest.raises(VerificationEvidenceError, match="Memory-assisted"):
        RunCoordinator(session).run_independent_evaluation(
            target_run, target_worker.worker_id, agent_execution_id=execution_id,
            draft_artifact_id=uuid4(), workspace_root=target_directories.workspace_root,
            artifact_root=target_directories.artifact_root, image=image,
            command=("python", "/suite/adapter.py"), cases=cases,
            suite_version=material["suite_version"], max_diff_bytes=65536,
            max_patch_bytes=4096, timeout_seconds=15, stop_event=Event(),
            calibration_workspaces=calibration_workspaces)
    with factory() as session, pytest.raises(VerificationEvidenceError, match="Memory-assisted"):
        RunCoordinator(session).finalize_passing_verification(
            target_run, target_worker.worker_id, agent_execution_id=execution_id,
            draft_artifact_id=uuid4(), independent_artifact_id=uuid4(),
            verification_config=target_config,
            workspace_root=target_directories.workspace_root,
            artifact_root=target_directories.artifact_root, max_diff_bytes=65536,
            max_patch_bytes=4096, stop_event=Event())

    # ...while the Skills that do apply are still recorded as diagnostic evidence only.
    with factory() as session:
        coordinator = RunCoordinator(session)
        target_verifier = coordinator.start_agent_execution(
            target_run, target_worker.worker_id, role=AgentRole.VERIFIER, agent_input={})
    with pytest.raises(SkillNotApplicable, match="scope"):
        check_diff_scope(
            version="2", worker=target_worker, run_id=target_run,
            verifier_execution_id=target_verifier, directories=target_directories,
            trusted_scope=ScopeSpec(allowed_paths=("src/",), forbidden_paths=()),
            max_diff_bytes=65536, stop_event=Event())
    check = run_registered_check(
        version="1", worker=target_worker, run_id=target_run,
        verifier_execution_id=target_verifier, directories=target_directories,
        registry={"noop": (sys.executable, "-c", "print('checked')")}, command_name="noop",
        stop_event=Event())
    assert check["tool_status"] == "succeeded"
    with factory() as session:
        call = session.get(ToolCall, UUID(check["tool_call_id"]))
        assert call.run_id == target_run and call.status.value == "succeeded"
        assert session.get(Run, target_run).status.value == "RUNNING"
        assert not session.scalars(select(Verification).where(
            Verification.run_id == target_run)).all()

    # The prepared root is untouched by the whole run: it still sits on C with its material.
    assert _git(plan["repository"]["repo"], "rev-parse", "HEAD") == base_commit
    with pytest.raises(MemoryScopeError, match="outside the declared scope"):
        with factory() as session:
            MemoryService(session).inspect_candidates(
                scope=RepositoryScope(repository=str(tmp_path / "elsewhere")),
                target_task_id=target_id, topic="repo-layout", target_environment=ENVIRONMENT)
