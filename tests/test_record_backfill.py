"""Rebuilding a record for a Run that already finished, from the evidence it left behind.

The row-level fixtures here are constructed (draft, receipt, Verification), not evidence that
a real task passed; they exist so the readers and the classifier can be exercised without a
model, a container or a Windows Job. Everything the entry itself does is real: it reads the
rows, runs the same classifier and writes one new file.
"""
import io
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from devflow_ai.artifacts import write_artifact_file
from devflow_ai.benchmark_record import (
    AttemptKind, is_formal_sample, load_run_record,
)
from devflow_ai.enums import AgentExecutionStatus, AgentRole, RunStatus, TaskStatus, VerificationResult
from devflow_ai.models import AgentExecution, Artifact, Run, Task, ToolCall, Verification
from devflow_ai.record_backfill import BackfillError, BackfillRequest, backfill_record
from devflow_ai.services import AdmissionRecord, TaskService
from devflow_ai.verifier import IndependentEvaluationRecord, SemanticReviewDraftRecord
from devflow_ai.worker import Worker
from devflow_ai.record_backfill import main as backfill_main

BASE_COMMIT = "b" * 40
SUITE_VERSION = "backfill-suite-v1"


def _pack(tmp_path, *, base_commit=BASE_COMMIT, suite_version=SUITE_VERSION):
    pack = tmp_path / "pack"
    pack.mkdir(exist_ok=True)
    (pack / "scenarios.py").write_text(
        "def test_placeholder():\n    assert True\n", encoding="utf-8")
    manifest = {
        "pack_id": "backfill-pack", "schema_version": 1, "title": "Backfill pack",
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
        "independent_suite": {
            "suite_version": suite_version, "materials_sha256": "c" * 64,
            "digest_scope": "bytes as delivered",
            "digest_method": "sha256 over those bytes",
            "provided_by": "trusted_control_plane",
            "declaration": "Provided by the trusted control plane for this test only.",
        },
        "unverified": [{"field": "policy", "reason": "Not pinned for this fixture."}],
    }
    (pack / "task.json").write_text(json.dumps(manifest), encoding="utf-8")
    return pack


def _task_and_run(session_factory, *, base_commit=BASE_COMMIT, worker_id="backfill-worker"):
    with session_factory() as session:
        task, created = TaskService(session).create_task(
            title="Backfill task", description="Rebuild this Run's record",
            repo="example-repo", idempotency_key=uuid4())
        assert created
        task_id = task.id
    with session_factory() as session:
        TaskService(session).apply_admission_decision(
            task_id, TaskStatus.ELIGIBLE, base_commit=base_commit,
            admission_record=AdmissionRecord(reason="test decision", evidence=["fixture"]))
    run_id = Worker(session_factory, worker_id).claim_once().run_id
    return task_id, run_id


def _write_evidence(factory, artifact_root, *, run_id, verifier_execution_id,
                    suite_version=SUITE_VERSION, receipt_outcome="passed",
                    receipt_suite_version=None, detail=b"diff --git a/x b/x\n"):
    """Write a real-format draft, diff and independent receipt for one Verifier."""
    artifact_root = Path(artifact_root).resolve()
    draft_id, patch_id, diff_id = uuid4(), uuid4(), uuid4()
    diff_path, diff_hash = write_artifact_file(
        artifact_root, diff_id, io.BytesIO(detail), chunk_size=65536)
    draft = SemanticReviewDraftRecord.model_validate({
        "record_type": "semantic_review_draft", "schema_version": 1,
        "run_id": str(run_id), "agent_execution_id": str(verifier_execution_id),
        "selection": {
            "baseline_verifier_id": str(uuid4()), "baseline_call_id": str(uuid4()),
            "baseline_artifact_id": str(uuid4()), "patch_call_id": str(uuid4()),
            "current_call_id": str(uuid4()), "current_artifact_id": str(uuid4()),
            "diff_call_id": str(uuid4()), "command_name": "pytest",
            "target_tests": ["test_baseline.py::test_baseline"]},
        "review": {"outcome": "REVIEW", "result": "passed",
                   "reason": "Constructed fixture for a reader test.",
                   "citations": [{"source": "workspace_diff", "tool_call_id": str(uuid4()),
                                  "path": "x.py", "snippet": "return 1",
                                  "reason": "Constructed fixture citation.",
                                  "diff_side": "added"}]},
        "test_comparison": {"target_result": "passed", "regression_result": "passed"},
        "read_call_ids": [], "patch_artifact_id": str(patch_id),
        "diff_artifact_id": str(diff_id),
    })
    draft_path, draft_hash = write_artifact_file(
        artifact_root, draft_id, io.BytesIO(draft.model_dump_json().encode("utf-8")),
        chunk_size=65536)
    receipt = IndependentEvaluationRecord.model_validate({
        "record_type": "independent_evaluation", "schema_version": 1,
        "run_id": str(run_id), "agent_execution_id": str(verifier_execution_id),
        "draft_artifact_id": str(draft_id), "patch_artifact_id": str(patch_id),
        "diff_artifact_id": str(diff_id), "diff_sha256": diff_hash,
        "suite_version": receipt_suite_version or suite_version,
        "outcome": receipt_outcome, "planned_case_count": 1, "image_id": None,
        "calibrated": True, "adapter_sha256": "d" * 64,
    })
    receipt_id = uuid4()
    receipt_path, receipt_hash = write_artifact_file(
        artifact_root, receipt_id, io.BytesIO(receipt.model_dump_json().encode("utf-8")),
        chunk_size=65536)
    with factory.begin() as session:
        session.add(Artifact(id=draft_id, agent_execution_id=verifier_execution_id,
                             kind="semantic_review_draft", content_ref=str(draft_path),
                             content_hash=draft_hash))
        session.add(Artifact(id=receipt_id, agent_execution_id=verifier_execution_id,
                             kind="independent_evaluation", content_ref=str(receipt_path),
                             content_hash=receipt_hash))
        session.add(Artifact(id=diff_id, agent_execution_id=verifier_execution_id,
                             kind="workspace_diff", content_ref=str(diff_path),
                             content_hash=diff_hash))
    return SimpleNamespace(draft_id=draft_id, receipt_id=receipt_id,
                           receipt_path=receipt_path, receipt_hash=receipt_hash)


def _finish_formally(factory, artifacts_root, *, run_id, suite_version=SUITE_VERSION):
    """Mark one Run as formally passed with a loadable draft and receipt."""
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.status = RunStatus.PASSED
        run.finished_at = func.now()
        verifier = AgentExecution(
            run_id=run_id, agent_role=AgentRole.VERIFIER.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, input={}, output={})
        session.add(verifier)
        session.flush()
        verifier_id = verifier.id
    written = _write_evidence(factory, artifacts_root, run_id=run_id,
                              verifier_execution_id=verifier_id,
                              suite_version=suite_version)
    with factory.begin() as session:
        verification = Verification(
            run_id=run_id, result=VerificationResult.PASSED,
            evidence={"scope": "trusted_v1", "draft_artifact_id": str(written.draft_id),
                      "independent_artifact_id": str(written.receipt_id),
                      "suite_version": suite_version})
        session.add(verification)
        session.flush()
        session.get(AgentExecution, verifier_id).output = {
            "outcome": "VERIFICATION_PASSED", "verification_id": str(verification.id)}
        verification_id = verification.id
    return verification_id, written, verifier_id


def _counts(factory):
    with factory() as session:
        return {model.__tablename__: session.scalar(select(func.count()).select_from(model))
                for model in (Run, AgentExecution, ToolCall, Verification)}


def _request(tmp_path, *, task_id, run_id, pack, record, artifacts_root=None,
             attempt_kind="unknown"):
    return BackfillRequest(
        task_id=task_id, run_id=run_id, pack_dir=pack,
        artifacts_root=artifacts_root or (tmp_path / "artifacts"),
        record_path=record, operator="operator-a", attempt_kind=attempt_kind)


def test_a_verifiable_formal_run_is_backfilled_as_a_formal_pass(
    worker_session_factory, tmp_path,
):
    factory = worker_session_factory
    pack = _pack(tmp_path)
    task_id, run_id = _task_and_run(factory)
    artifacts = tmp_path / "artifacts" / str(run_id)
    artifacts.mkdir(parents=True)
    verification_id, written, _ = _finish_formally(
        factory, artifacts, run_id=run_id)
    before = _counts(factory)
    record_path = tmp_path / "records" / "backfilled.json"

    result = backfill_record(
        _request(tmp_path, task_id=task_id, run_id=run_id, pack=pack, record=record_path),
        session_factory=factory)

    assert result["outcome"] == "formal_pass"
    assert result["formal_verification_id"] == str(verification_id)
    assert result["human_review"] == "pending"
    record = load_run_record(record_path)
    assert record.outcome.value == "formal_pass"
    assert record.formal is not None
    assert record.formal.verification_id == str(verification_id)
    assert record.formal.independent_receipt_sha256 == written.receipt_hash
    assert record.task_id == str(task_id) and record.run_id == str(run_id)
    assert record.human_review.conclusion == "pending"
    assert record.policy_version and record.model_id == "unrecorded"
    # The kind was not recorded anywhere, so the reconstruction does not claim one — and an
    # unknown kind stays out of the formal sample even though the Run passed.
    assert record.attempt_kind is AttemptKind.UNKNOWN
    assert is_formal_sample(record) is False
    assert "backfill=true" in record.notes and "attempt_kind was not recorded" in record.notes
    assert "limits: not recovered" in record.notes
    # Nothing about the Run or its evidence changed; no second Run appeared.
    assert _counts(factory) == before
    with factory() as session:
        assert session.get(Run, run_id).status is RunStatus.PASSED


def test_tampered_evidence_is_not_backfilled_as_a_formal_pass(
    worker_session_factory, tmp_path,
):
    factory = worker_session_factory
    pack = _pack(tmp_path)
    task_id, run_id = _task_and_run(factory)
    artifacts = tmp_path / "artifacts" / str(run_id)
    artifacts.mkdir(parents=True)
    _, written, _ = _finish_formally(factory, artifacts, run_id=run_id)
    written.receipt_path.write_bytes(b'{"record_type": "tampered"}')
    record_path = tmp_path / "records" / "tampered.json"

    result = backfill_record(
        _request(tmp_path, task_id=task_id, run_id=run_id, pack=pack, record=record_path),
        session_factory=factory)

    assert result["outcome"] != "formal_pass"
    assert result["formal_verification_id"] is None
    record = load_run_record(record_path)
    assert record.formal is None
    assert record.outcome.value in ("incomplete", "public_only_pass")


def test_a_formal_pass_without_its_artifacts_is_not_claimed(worker_session_factory, tmp_path):
    factory = worker_session_factory
    pack = _pack(tmp_path)
    task_id, run_id = _task_and_run(factory)
    artifacts = tmp_path / "artifacts" / str(run_id)
    artifacts.mkdir(parents=True)
    # A Verification row that cites artifacts which were never written.
    with factory.begin() as session:
        session.get(Run, run_id).status = RunStatus.PASSED
        session.get(Run, run_id).finished_at = func.now()
        verifier = AgentExecution(
            run_id=run_id, agent_role=AgentRole.VERIFIER.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, input={}, output={})
        session.add(verifier)
        session.flush()
        verification = Verification(
            run_id=run_id, result=VerificationResult.PASSED,
            evidence={"scope": "trusted_v1", "draft_artifact_id": str(uuid4()),
                      "independent_artifact_id": str(uuid4()),
                      "suite_version": SUITE_VERSION})
        session.add(verification)
        session.flush()
        session.get(AgentExecution, verifier.id).output = {
            "outcome": "VERIFICATION_PASSED", "verification_id": str(verification.id)}
    record_path = tmp_path / "records" / "missing.json"

    result = backfill_record(
        _request(tmp_path, task_id=task_id, run_id=run_id, pack=pack, record=record_path),
        session_factory=factory)

    assert result["outcome"] != "formal_pass"
    assert load_run_record(record_path).formal is None


def test_a_run_that_belongs_to_another_task_is_refused(worker_session_factory, tmp_path):
    factory = worker_session_factory
    pack = _pack(tmp_path)
    first_task, first_run = _task_and_run(factory, worker_id="worker-a")
    second_task, _ = _task_and_run(factory, worker_id="worker-b")
    (tmp_path / "artifacts").mkdir(exist_ok=True)
    record_path = tmp_path / "records" / "mismatch.json"

    with pytest.raises(BackfillError, match="does not belong"):
        backfill_record(
            _request(tmp_path, task_id=second_task, run_id=first_run, pack=pack,
                     record=record_path),
            session_factory=factory)

    assert not record_path.exists()


def test_a_running_run_is_refused(worker_session_factory, tmp_path):
    factory = worker_session_factory
    pack = _pack(tmp_path)
    task_id, run_id = _task_and_run(factory)
    (tmp_path / "artifacts").mkdir(exist_ok=True)
    record_path = tmp_path / "records" / "running.json"

    with pytest.raises(BackfillError, match="still RUNNING"):
        backfill_record(
            _request(tmp_path, task_id=task_id, run_id=run_id, pack=pack, record=record_path),
            session_factory=factory)

    assert not record_path.exists()


def test_an_existing_record_path_is_refused(worker_session_factory, tmp_path):
    factory = worker_session_factory
    pack = _pack(tmp_path)
    task_id, run_id = _task_and_run(factory)
    artifacts = tmp_path / "artifacts" / str(run_id)
    artifacts.mkdir(parents=True)
    _finish_formally(factory, artifacts, run_id=run_id)
    record_path = tmp_path / "records" / "taken.json"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text("earlier evidence\n", encoding="utf-8")

    with pytest.raises(BackfillError, match="already exists"):
        backfill_record(
            _request(tmp_path, task_id=task_id, run_id=run_id, pack=pack, record=record_path),
            session_factory=factory)

    assert record_path.read_text(encoding="utf-8") == "earlier evidence\n"


def test_a_repeat_call_does_not_overwrite_the_first_reconstruction(
    worker_session_factory, tmp_path,
):
    factory = worker_session_factory
    pack = _pack(tmp_path)
    task_id, run_id = _task_and_run(factory)
    artifacts = tmp_path / "artifacts" / str(run_id)
    artifacts.mkdir(parents=True)
    _finish_formally(factory, artifacts, run_id=run_id)
    record_path = tmp_path / "records" / "once.json"
    request = _request(tmp_path, task_id=task_id, run_id=run_id, pack=pack,
                       record=record_path)

    backfill_record(request, session_factory=factory)
    original = record_path.read_bytes()
    with pytest.raises(BackfillError, match="already exists"):
        backfill_record(request, session_factory=factory)

    assert record_path.read_bytes() == original


def test_the_cli_writes_one_record_and_reports_it(worker_session_factory, tmp_path, capsys):
    factory = worker_session_factory
    pack = _pack(tmp_path)
    task_id, run_id = _task_and_run(factory)
    artifacts = tmp_path / "artifacts" / str(run_id)
    artifacts.mkdir(parents=True)
    _finish_formally(factory, artifacts, run_id=run_id)
    record_path = tmp_path / "records" / "cli.json"
    before = _counts(factory)

    exit_code = backfill_main(
        ["--task-id", str(task_id), "--run-id", str(run_id), "--pack", str(pack),
         "--artifacts-root", str(tmp_path / "artifacts"), "--record", str(record_path),
         "--operator", "operator-a"],
        session_factory=factory)

    assert exit_code == 0
    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert event["event"] == "record_backfill_written"
    assert event["outcome"] == "formal_pass"
    assert event["attempt_kind"] == "unknown"
    assert load_run_record(record_path).run_id == str(run_id)
    assert _counts(factory) == before  # no Run, execution or tool call was created


def test_the_cli_reports_unusable_identifiers(worker_session_factory, tmp_path, capsys):
    factory = worker_session_factory
    pack = _pack(tmp_path)

    exit_code = backfill_main(
        ["--task-id", "not-a-uuid", "--run-id", str(uuid4()), "--pack", str(pack),
         "--artifacts-root", str(tmp_path), "--record", str(tmp_path / "x.json"),
         "--operator", "operator-a"],
        session_factory=factory)

    assert exit_code == 2
    assert "UUID" in capsys.readouterr().err
