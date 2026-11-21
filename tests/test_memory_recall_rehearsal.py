"""Pre-verification for the two-task cross-task recall rehearsal (constructed data only).

What this file checks is the *plumbing* and the *gates*: that a Task created by
``benchmark_task`` cannot be a memory source or a recall target, that a development Task
shaped the same way can, and that a rehearsal Run puts one reviewed candidate into the
Explorer input while producing no formal Verification.

What it does **not** check, and must never be read as checking:

* that a real development Task with a ``trusted_v1`` Verification exists (the source Run and
  its Verification here are written straight into the isolated schema, exactly like
  ``tests/test_memory.py`` does);
* that the candidate is true, complete, free of hidden evaluation material, or that a model
  used it;
* that the rehearsal improves anything, or that it is a sample of any kind.

Those need a real formal attempt on a real Task, which this file cannot produce.
"""
import json
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import select, text

from devflow_ai.benchmark_task import create_benchmark_task
from devflow_ai.config import HeartbeatConfig
from devflow_ai.context import MEMORY_RECALL_CONTEXT_KEY
from devflow_ai.coordinator import (
    ModelTokenUsage, RunCoordinator, VerificationEvidenceError,
)
from devflow_ai.enums import AgentRole, RunStatus, TaskStatus, VerificationResult
from devflow_ai.memory import (
    MemoryCandidate, MemoryRecallRequest, MemoryScopeError, MemoryService, RepositoryScope,
)
from devflow_ai.model_gateway import ModelGateway, ModelReply, PlannerModelGateway
from devflow_ai.model_gateways import ModelGateways
from devflow_ai.models import AgentExecution, MemoryItem, MemoryUse, Run, Task, Verification
from devflow_ai.policy import build_default_policy
from devflow_ai.services import AdmissionRecord, TaskService
from devflow_ai.task_pack import load_manifest
from devflow_ai.worker_run import main as worker_run_main

HEARTBEAT = HeartbeatConfig(statement_timeout_seconds=5, shutdown_timeout_seconds=5)
ENVIRONMENT = "dev-rehearsal-windows"
TOPIC = "rehearsal-repo-layout"
MEMORY_MIGRATIONS = ("b7f2c31d8a90", "c4a7e2f19b30", "d18e42f70a91", "ec624a31d740",
                     "f6a92c3184d2", "a5810e46f2c9", "b41e9d2c8370")
EXPLORE_DECISION = {"outcome": "EXPLORE", "reason": "The Issue needs repository evidence",
                    "exploration_focus": "Locate the affected facts"}
BLOCKED_DECISION = {"outcome": "BLOCKED", "reason": "Rehearsal stops here",
                    "decision_required": "None"}


@pytest.fixture
def rehearsal_factory(worker_session_factory):
    """The worker fixture plus the memory tables, inside the same isolated schema."""
    engine = worker_session_factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations"))
    with engine.begin() as connection:
        connection.execution_options(schema_translate_map=None)
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        with Operations.context(MigrationContext.configure(connection)):
            for revision in MEMORY_MIGRATIONS:
                scripts.get_revision(revision).module.upgrade()
    return worker_session_factory


# --- fixtures the rehearsal needs ---------------------------------------------------

def _policy():
    policy = build_default_policy()
    policy.policy_version = "recall-rehearsal-v1"
    policy.permissions.allowed_tools_by_role[AgentRole.EXPLORER] = ["read_file"]
    return policy


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          check=True, timeout=15, text=True).stdout.strip()


def _repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    repo = tmp_path / "dev-repo"
    repo.mkdir()
    (repo / "README.md").write_bytes(b"Repository facts\n")
    _git(repo, "init", "--quiet")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "initial")
    return repo, _git(repo, "rev-parse", "HEAD")


def _manifest(base_commit, *, pack_id="recall-rehearsal-pack", independent_suite=None):
    manifest = {
        "pack_id": pack_id, "schema_version": 1, "title": "Recall rehearsal pack",
        "source": {"issue_url": "https://example.invalid/issues/1",
                   "repository_url": "https://example.invalid/repo",
                   "license": "MIT", "issue_reference": "rehearsal#1"},
        "base_commit": base_commit,
        "environment": {"python_requires": ">=3.11", "dependencies": ["pytest"],
                        "platform_notes": "rehearsal fixture"},
        "issue": {"sanitized_problem": "A sanitized problem.",
                  "observable_error": "An error.",
                  "expected_behavior": "The expected behaviour."},
        "scope": {"allowed_paths": ["src/"]},
        "public_checks": {"command": ["python", "-m", "pytest"], "command_name": "pytest",
                          "target_tests": ["scenarios::test_placeholder"]},
        "baseline": {"status": "verified_here"},
        "external_connections": "none",
        "independent_suite": independent_suite,
        "unverified": ([{"field": "policy", "reason": "The rehearsal snapshot is not a benchmark policy."}]
                       + ([] if independent_suite is not None else [
                           {"field": "independent_suite", "reason": "No trusted material in a rehearsal."}])),
    }
    return manifest


def _pack(tmp_path, base_commit, *, name="pack", **overrides):
    pack_dir = tmp_path / name
    pack_dir.mkdir(exist_ok=True)
    (pack_dir / "scenarios.py").write_text(
        "def test_placeholder():\n    assert True\n", encoding="utf-8")
    (pack_dir / "task.json").write_text(
        json.dumps(_manifest(base_commit, **overrides)), encoding="utf-8")
    return pack_dir


def _dev_task(factory, repo, base_commit, *, policy=None):
    """A development Task: admitted with evidence that carries no ``pack:`` marker."""
    policy = policy if policy is not None else _policy()
    with factory() as session:
        task, _ = TaskService(session, policy_factory=lambda: policy).create_task(
            title="Development task", description="A development issue",
            repo=str(repo), idempotency_key=uuid4())
        task_id = task.id
    with factory() as session:
        TaskService(session).apply_admission_decision(
            task_id, TaskStatus.ELIGIBLE, base_commit=base_commit,
            admission_record=AdmissionRecord(
                reason="development admission", evidence=["dev-task:rehearsal-fixture"]))
    return task_id


def _policy_file(tmp_path, factory, task_id, name="policy.json"):
    with factory() as session:
        snapshot = dict(session.scalar(select(Task.policy_snapshot).where(Task.id == task_id)))
    path = tmp_path / name
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    return path


def _passed_source(factory, task_id):
    """The source's formal record: a PASSED Run and a trusted_v1 Verification.

    Constructed on purpose. A real one can only come from a formal attempt with the
    Windows baseline capture, a calibrated independent evaluation and trusted material.
    """
    with factory() as session:
        run, _ = RunCoordinator(session).start_initial_run(
            task_id, "source-worker", f"source:{task_id}")
        run_id = run.id
    with factory.begin() as session:
        session.get(Run, run_id).status = RunStatus.PASSED
        session.get(Task, task_id).status = TaskStatus.AWAITING_REVIEW
        verification = Verification(run_id=run_id, result=VerificationResult.PASSED,
                                    evidence={"scope": "trusted_v1", "suite_version": "fixture-suite"})
        session.add(verification)
        session.flush()
        verification_id = verification.id
    return run_id, verification_id


def _ingest(factory, *, source_task, source_run, verification_id, repository,
            base_commit, identity="rehearsal-memory", content="The public check lives in scenarios.py."):
    with factory() as session:
        return MemoryService(session).ingest(MemoryCandidate(
            identity_key=identity, scope=RepositoryScope(repository), source_kind="verification",
            task_id=source_task, run_id=source_run, verification_id=verification_id,
            content=content, created_by="operator-a", topic=TOPIC,
            valid_until=datetime.now(timezone.utc) + timedelta(days=2),
            applicable_base_commit=base_commit, applicable_environment=ENVIRONMENT))


def _review(factory, *, repository, memory_id, conclusion="accepted"):
    with factory() as session:
        shown = MemoryService(session).read(
            scope=RepositoryScope(repository), memory_id=memory_id)
    with factory() as session:
        record = MemoryService(session).review(
            memory_id=memory_id, scope=RepositoryScope(repository),
            candidate_sha256=shown["candidate_sha256"], conclusion=conclusion,
            reviewer="person", reason="rehearsal review")
    return shown, record


def _recall(factory, *, repository, target_task, base_commit, memory_id, sha, key=None):
    return MemoryRecallRequest(
        idempotency_key=key or uuid4(), memory_id=memory_id, candidate_sha256=sha,
        operator="operator-a", purpose="rehearsal", target_environment=ENVIRONMENT)


def _scripted_gateways(factory, worker_id):
    """Role stubs: one read, one summary, then a Supervisor stop. No provider is called."""
    def make(generate):
        return ModelGateway(session_factory=factory, worker_id=worker_id,
                            estimate_input_tokens=lambda _: 100, generate=generate,
                            max_output_tokens=100)

    def unused(*_args):
        pytest.fail("this role is not part of the rehearsal under test")

    supervisor_decisions = iter([EXPLORE_DECISION, BLOCKED_DECISION])
    prompts = []

    def supervisor(_prompt, _limit):
        return ModelReply(next(supervisor_decisions),
                          ModelTokenUsage(input_tokens=40, output_tokens=20))

    def explorer(prompt, _limit):
        data = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
        prompts.append(prompt)
        if len(prompts) == 1:
            return ModelReply({"path": "README.md"},
                              ModelTokenUsage(input_tokens=40, output_tokens=20))
        return ModelReply({"summary": "Read repository facts", "read_evidence": {
            "path": "README.md", "tool_call_id": data["read"]["tool_call_id"]}},
            ModelTokenUsage(input_tokens=40, output_tokens=20))

    gateways = ModelGateways(
        supervisor=make(supervisor), explorer=make(explorer),
        planner=PlannerModelGateway(session_factory=factory, worker_id=worker_id,
                                    estimate_input_tokens=lambda _: 100, generate=unused,
                                    max_output_tokens=100),
        coder=make(unused), verifier=make(unused))

    @contextmanager
    def factory_context(_session_factory, _worker_id):
        yield gateways

    return factory_context, prompts


def _cli_args(tmp_path, *, setup, extra=()):
    for name in ("workspaces", "artifacts"):
        (tmp_path / name).mkdir(exist_ok=True)
    return [
        "--pack", str(setup["pack"]), "--task-id", str(setup["task_id"]),
        "--policy", str(setup["policy"]), "--operator", "operator-a",
        "--python-executable", sys.executable,
        "--workspaces-root", str(tmp_path / "workspaces"),
        "--artifacts-root", str(tmp_path / "artifacts"),
        "--attempt-kind", "rehearsal", "--worker-id", "recall-rehearsal", *extra,
    ]


def _runs(factory, task_id):
    with factory() as session:
        return session.scalars(select(Run).where(Run.task_id == task_id)).all()


# --- the gates: pack-created Tasks are refused on both sides -------------------------

def test_a_pack_created_source_is_refused_while_a_dev_source_is_admitted(
        rehearsal_factory, tmp_path, monkeypatch):
    factory = rehearsal_factory
    repo, head = _repo(tmp_path, monkeypatch)
    policy = _policy()

    # The real producer of the marker: this is the same call the Click packs use.
    pack = _pack(tmp_path, head, name="benchmark-pack", pack_id="rehearsal-benchmark")
    benchmark_task = create_benchmark_task(
        factory, load_manifest(pack), repo=str(repo), policy=policy, operator="operator-a")
    with factory() as session:
        evidence = session.get(Task, benchmark_task).admission_record["evidence"]
    assert any(item.startswith("pack:") for item in evidence)
    run_id, verification_id = _passed_source(factory, benchmark_task)
    with pytest.raises(MemoryScopeError, match="benchmark"):
        _ingest(factory, source_task=benchmark_task, source_run=run_id,
                verification_id=verification_id, repository=str(repo), base_commit=head)

    # A development Task with the same evidence shape but no pack marker is admitted.
    dev_source = _dev_task(factory, repo, head, policy=policy)
    dev_run, dev_verification = _passed_source(factory, dev_source)
    item = _ingest(factory, source_task=dev_source, source_run=dev_run,
                   verification_id=dev_verification, repository=str(repo), base_commit=head,
                   identity="dev-memory")
    with factory() as session:
        stored = session.get(MemoryItem, UUID(item["id"]))
        assert stored.source_state == "trusted_passed"
        assert stored.content_status == "unverified_candidate"
    with factory() as session:
        assert session.scalar(select(MemoryItem).where(
            MemoryItem.identity_key == "rehearsal-memory")) is None


def test_a_pack_created_target_refuses_preview_and_declaration(
        rehearsal_factory, tmp_path, monkeypatch):
    factory = rehearsal_factory
    repo, head = _repo(tmp_path, monkeypatch)
    dev_source = _dev_task(factory, repo, head)
    dev_run, dev_verification = _passed_source(factory, dev_source)
    item = _ingest(factory, source_task=dev_source, source_run=dev_run,
                   verification_id=dev_verification, repository=str(repo), base_commit=head)
    shown, _ = _review(factory, repository=str(repo), memory_id=UUID(item["id"]))

    pack = _pack(tmp_path, head, name="target-benchmark-pack", pack_id="rehearsal-target")
    benchmark_target = create_benchmark_task(
        factory, load_manifest(pack), repo=str(repo), policy=_policy(), operator="operator-a")
    with factory() as session:
        run, _ = RunCoordinator(session).start_initial_run(
            benchmark_target, "target-worker", f"target:{benchmark_target}")
        target_run = run.id
    scope = RepositoryScope(str(repo))
    with pytest.raises(MemoryScopeError, match="benchmark"):
        with factory() as session:
            MemoryService(session).inspect_candidates(
                scope=scope, target_task_id=benchmark_target, topic=TOPIC,
                target_environment=ENVIRONMENT)
    with pytest.raises(MemoryScopeError, match="benchmark"):
        with factory() as session:
            MemoryService(session).prepare_recall(
                idempotency_key=uuid4(), scope=scope, task_id=benchmark_target,
                run_id=target_run, memory_id=UUID(item["id"]),
                candidate_sha256=shown["candidate_sha256"], operator="operator-a",
                purpose="rehearsal", target_environment=ENVIRONMENT)
    with factory() as session:
        assert session.scalars(select(MemoryUse)).all() == []


# --- the rehearsal itself: one reviewed candidate reaches the Explorer input --------

def test_two_dev_tasks_run_a_recall_rehearsal_without_a_formal_result(
        rehearsal_factory, tmp_path, monkeypatch, capsys):
    factory = rehearsal_factory
    repo, head = _repo(tmp_path, monkeypatch)
    audit_source = _dev_task(factory, repo, head)
    audit_run, audit_verification = _passed_source(factory, audit_source)
    item = _ingest(factory, source_task=audit_source, source_run=audit_run,
                   verification_id=audit_verification, repository=str(repo), base_commit=head)
    memory_id = UUID(item["id"])

    target = _dev_task(factory, repo, head)
    pack = _pack(tmp_path, head)
    policy_path = _policy_file(tmp_path, factory, target)

    # Preview must name the target environment, or applicability is simply unknown.
    with factory() as session:
        without_environment = MemoryService(session).inspect_candidates(
            scope=RepositoryScope(str(repo)), target_task_id=target, topic=TOPIC)
        with_environment = MemoryService(session).inspect_candidates(
            scope=RepositoryScope(str(repo)), target_task_id=target, topic=TOPIC,
            target_environment=ENVIRONMENT)
    assert without_environment[0]["candidate_checks"]["eligible_to_request_use"] is False
    assert with_environment[0]["candidate_checks"]["eligible_to_request_use"] is False
    candidate_checks = with_environment[0]["candidate_checks"]
    assert candidate_checks["review_state"] == "missing"

    shown, review = _review(factory, repository=str(repo), memory_id=memory_id)
    assert review["conclusion"] == "accepted"
    with factory() as session:
        after_review = MemoryService(session).inspect_candidates(
            scope=RepositoryScope(str(repo)), target_task_id=target, topic=TOPIC,
            target_environment=ENVIRONMENT)
    assert after_review[0]["candidate_checks"]["eligible_to_request_use"] is True

    selection = _recall(factory, repository=str(repo), target_task=target, base_commit=head,
                        memory_id=memory_id, sha=shown["candidate_sha256"])
    gateways_factory, prompts = _scripted_gateways(factory, "recall-rehearsal")
    setup = {"task_id": target, "pack": pack, "policy": policy_path}
    args = _cli_args(tmp_path, setup=setup, extra=[
        "--confirm-run", "--memory-id", str(memory_id),
        "--memory-sha256", shown["candidate_sha256"],
        "--memory-use-key", str(selection.idempotency_key),
        "--memory-environment", ENVIRONMENT])
    assert worker_run_main(args, session_factory=factory,
                           gateways_factory=gateways_factory) == 0
    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert event["claimed"] and event["attempt_kind"] == "rehearsal"
    assert event["formal_acceptance"] == "not_performed"
    assert event["run_status"] == RunStatus.RUNNING.value

    # What was passed in: the candidate text is in the Explorer prompt and in the
    # recorded Agent input. This is evidence of construction, not of model use.
    context = json.loads(prompts[0].split("\nInput data JSON:\n", 1)[1])[MEMORY_RECALL_CONTEXT_KEY]
    assert context["candidate_id"] == str(memory_id)
    assert context["content"] == "The public check lives in scenarios.py."
    assert context["content_status"] == "unverified_candidate"
    # The second Explorer call carries the same record under ``agent_input``.
    second = json.loads(prompts[1].split("\nInput data JSON:\n", 1)[1])
    assert second["agent_input"][MEMORY_RECALL_CONTEXT_KEY]["use_id"] == context["use_id"]
    run_id = _runs(factory, target)[0].id
    with factory() as session:
        explorer = session.scalar(select(AgentExecution).where(
            AgentExecution.run_id == run_id, AgentExecution.agent_role == "explorer"))
        assert explorer.input[MEMORY_RECALL_CONTEXT_KEY]["candidate_sha256"] == shown["candidate_sha256"]
        uses = session.scalars(select(MemoryUse).where(MemoryUse.task_id == target)).all()
        assert len(uses) == 1 and uses[0].idempotency_key == selection.idempotency_key
        assert session.scalars(select(Verification).where(
            Verification.run_id == run_id)).all() == []

    # The Task is permanently barred from an independent evaluation result.
    with factory() as session, session.begin():
        with pytest.raises(VerificationEvidenceError, match="independent evaluation"):
            RunCoordinator(session).assert_independent_memory_isolation(run_id)


def test_recall_is_refused_before_claim_when_the_declaration_does_not_fit(
        rehearsal_factory, tmp_path, monkeypatch, capsys):
    factory = rehearsal_factory
    repo, head = _repo(tmp_path, monkeypatch)
    source = _dev_task(factory, repo, head)
    source_run, source_verification = _passed_source(factory, source)
    item = _ingest(factory, source_task=source, source_run=source_run,
                   verification_id=source_verification, repository=str(repo), base_commit=head)
    memory_id = UUID(item["id"])
    shown, _ = _review(factory, repository=str(repo), memory_id=memory_id)

    def attempt(target, **overrides):
        pack_commit = overrides.pop("pack_commit", head)
        pack = _pack(tmp_path, pack_commit, name=f"pack-{uuid4().hex[:6]}")
        policy_path = _policy_file(tmp_path, factory, target, name=f"policy-{uuid4().hex[:6]}.json")
        extra = ["--confirm-run", "--memory-id", str(memory_id),
                 "--memory-sha256", overrides.pop("sha", shown["candidate_sha256"]),
                 "--memory-use-key", str(overrides.pop("key", uuid4())),
                 "--memory-environment", overrides.pop("environment", ENVIRONMENT)]
        return worker_run_main(
            _cli_args(tmp_path, setup={"task_id": target, "pack": pack, "policy": policy_path},
                      extra=extra),
            session_factory=factory, gateways_factory=_scripted_gateways(factory, "recall-rehearsal")[0])

    wrong_environment = _dev_task(factory, repo, head)
    assert attempt(wrong_environment, environment="some-other-environment") == 2
    assert "not eligible" in capsys.readouterr().err
    assert _runs(factory, wrong_environment) == []

    # The candidate applies to one base commit only; a Task at another commit is refused,
    # and the pack has to agree with that Task for the check to even be reached.
    other_commit = _dev_task(factory, repo, "c" * 40)
    assert attempt(other_commit, pack_commit="c" * 40) == 2
    assert "not eligible" in capsys.readouterr().err
    assert _runs(factory, other_commit) == []

    # One declaration key cannot be spent on a second Task.
    first_target = _dev_task(factory, repo, head)
    used_key = uuid4()
    assert attempt(first_target, key=used_key) == 0
    capsys.readouterr()
    second_target = _dev_task(factory, repo, head)
    assert attempt(second_target, key=used_key) == 2
    assert "already in use" in capsys.readouterr().err
    assert _runs(factory, second_target) == []
    with factory() as session:
        assert session.scalar(select(MemoryUse).where(
            MemoryUse.task_id == first_target)) is not None
        assert session.scalar(select(MemoryUse).where(
            MemoryUse.task_id == second_target)) is None
