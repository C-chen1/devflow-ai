"""Experiment accounting against real PostgreSQL rows in an isolated schema.

Every Run, Verification, Artifact and Task in this file is a constructed fixture written into
this test's own schema and its own artifact directory, so the recording rules can be exercised.
They are **not** evidence that any task passed, that any candidate is better, or that an
experiment was really run: nothing here starts an RSI loop, calls a model, or evaluates a
candidate. What is real is the database and the shared classifier — the constraints the service
relies on, the row locks, the concurrent reservations that must not oversubscribe the budget,
and the same ``classify_outcome`` rules the benchmark, scheduler-record and backfill entries use.
"""
import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateSchema, DropSchema

from devflow_ai.artifacts import write_artifact_file
from devflow_ai.benchmark_record import (
    SCHEMA_VERSION, AttemptContext, AttemptKind, BenchmarkRunRecord, FormalEvidence,
    IndependentEvaluationEvidence, RunOutcome, write_run_record,
)
from devflow_ai.database import create_db_engine
from devflow_ai.enums import (
    AgentExecutionStatus, AgentRole, RunStatus, TaskStatus, ToolCallStatus, VerificationResult,
)
from devflow_ai.experiment import (
    ExperimentConflictError, ExperimentError, ExperimentEvidenceError, ExperimentLimitError,
    ExperimentLimits, ExperimentService, ExperimentStateError,
)
from devflow_ai.experiment_record import main as ledger_main
from devflow_ai.models import (
    AgentExecution, Artifact, Experiment, ExperimentCandidate, ExperimentRecord,
    ExperimentReservation, ExperimentTaskResult, Run, Task, ToolCall, Verification,
)
from devflow_ai.services import AdmissionRecord, TaskService
from devflow_ai.verifier import IndependentEvaluationRecord, SemanticReviewDraftRecord
from devflow_ai.worker import Worker

MIGRATION = "d9b3c7e1a250"
BASE = "a" * 40
SUITE_VERSION = "experiment-suite-v1"
SCRIPTS = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations"))


def _apply(engine, revision_ids) -> None:
    schema = engine.get_execution_options()["schema_translate_map"][None]
    with engine.begin() as connection:
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        with Operations.context(MigrationContext.configure(connection)):
            for revision_id in revision_ids:
                SCRIPTS.get_revision(revision_id).module.upgrade()


@pytest.fixture
def experiment_session_factory(worker_session_factory):
    """Add the Run execution-version column and the experiment tables to the schema."""
    _apply(worker_session_factory.kw["bind"], (MIGRATION,))
    return worker_session_factory


def _limits(**overrides):
    values = dict(max_candidates=3, max_total_model_tokens=1000, max_total_seconds=3600,
                  promotion_min_formal_passes=1, promotion_max_failures=0)
    values.update(overrides)
    return ExperimentLimits(**values)


def _task(factory, title="Experiment task"):
    with factory() as session:
        task, _ = TaskService(session).create_task(
            title=title, description="Fixture", repo="example-repo", idempotency_key=uuid4())
        task_id = task.id
    with factory() as session:
        TaskService(session).apply_admission_decision(
            task_id, TaskStatus.ELIGIBLE, base_commit=BASE,
            admission_record=AdmissionRecord(reason="fixture", evidence=["fixture"]))
    return task_id


def _unadmitted_task(factory, title="Unadmitted task"):
    """A Task that exists but has not been admitted, so it has no pinned base commit."""
    with factory() as session:
        task, _ = TaskService(session).create_task(
            title=title, description="Fixture", repo="example-repo", idempotency_key=uuid4())
        return task.id


def _attempt(factory, title="Experiment task", *, status=None, finished=True,
             execution_sha256=None):
    """One declared Task and the single Run it is claimed for.

    Created and claimed one at a time on purpose: discovery claims whichever Task is eligible,
    so leaving a second eligible Task behind would make this fixture ambiguous. An unfinished
    attempt keeps the status a stopped worker leaves behind — ``RUNNING``. ``execution_sha256``
    stands in for what an entry wrote after verifying a pinned manifest; ``None`` is the
    unknown case.
    """
    task_id = _task(factory, title)
    claim = Worker(factory, f"exp-{uuid4().hex[:8]}").claim_once()
    assert claim is not None
    assert claim.task_id == task_id
    with factory.begin() as session:
        run = session.get(Run, claim.run_id)
        if finished:
            run.status = status or RunStatus.PASSED
            run.finished_at = datetime.now(timezone.utc)
        else:
            run.status = status or RunStatus.RUNNING
            run.finished_at = None
        run.execution_sha256 = execution_sha256
    return task_id, claim.run_id


def _trusted_verification(factory, run_id, *, result=VerificationResult.PASSED,
                          scope="trusted_v1", receipt=True):
    evidence = {"scope": scope, "draft_artifact_id": str(uuid4()), "suite_version": "suite-1"}
    if receipt:
        evidence["independent_artifact_id"] = str(uuid4())
    with factory.begin() as session:
        verification = Verification(run_id=run_id, result=result, evidence=evidence)
        session.add(verification)
        session.flush()
        return verification.id


def _write_evidence(factory, artifact_root, *, run_id, verifier_execution_id,
                    suite_version=SUITE_VERSION, receipt_outcome="passed"):
    """Write a real-format Verifier draft, diff and independent receipt for one attempt.

    Same shape as the backfill tests' fixture: the classifier reads these files back, so the
    verdict it derives here comes from the same code path a real attempt goes through.
    """
    artifact_root = Path(artifact_root).resolve()
    draft_id, patch_id, diff_id = uuid4(), uuid4(), uuid4()
    diff_path, diff_hash = write_artifact_file(
        artifact_root, diff_id, io.BytesIO(b"diff --git a/x b/x\n"), chunk_size=65536)
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
                   "reason": "Constructed fixture for an accounting test.",
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
        "suite_version": suite_version,
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


def _finish_formally(factory, artifacts_root, *, run_id, suite_version=SUITE_VERSION,
                     receipt_outcome="passed"):
    """Mark one Run as formally passed with a draft and receipt the classifier can read back."""
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
                              suite_version=suite_version, receipt_outcome=receipt_outcome)
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
    return verification_id, written


def _registered(service, experiment_id, *, label, task_id, record_path,
                operator="operator-a"):
    return service.register_record(
        experiment_id=experiment_id, candidate_label=label, task_id=task_id,
        record_path=record_path, registered_by=operator)


def _checkout_tool_call(factory, run_id, commit, *, status=ToolCallStatus.SUCCEEDED):
    """The row ``execute_worker_git_checkout`` leaves behind when it verifies HEAD."""
    with factory.begin() as session:
        session.add(ToolCall(run_id=run_id, tool_name="git_checkout",
                             args={"base_commit": commit}, status=status,
                             stdout_excerpt=commit, exit_code=0))


def _second_run(factory, task_id, *, execution_sha256=None):
    """One extra attempt row for an already-claimed Task.

    Constructed directly because the claim path only hands out one attempt per eligible Task;
    the accounting rules under test do not depend on how the second attempt came to exist.
    """
    with factory.begin() as session:
        run = Run(task_id=task_id, attempt_number=2, status=RunStatus.FAILED,
                  creation_idempotency_key=f"fixture-{uuid4()}",
                  worker_id="fixture-worker",
                  execution_sha256=execution_sha256,
                  lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                  finished_at=datetime.now(timezone.utc))
        session.add(run)
        session.flush()
        return run.id


def _opened(factory, task_ids, *, limits=None, identity="experiment-one",
            base="b" * 64, created_by="operator-a"):
    with factory.begin() as session:
        return ExperimentService(session).open(
            identity_key=identity, base_execution_sha256=base, task_ids=task_ids,
            limits=limits or _limits(), created_by=created_by)


# ------------------------------------------------------------------ opening and limits


def test_open_freezes_the_declaration_and_replays_only_an_identical_submission(
        experiment_session_factory):
    factory = experiment_session_factory
    task_ids = [_task(factory, "one"), _task(factory, "two")]
    experiment_id = _opened(factory, task_ids,
                            limits=_limits(max_candidates=2, promotion_min_formal_passes=2))
    with factory() as session:
        service = ExperimentService(session)
        experiment = service.read(experiment_id)
        assert experiment.max_candidates == 2
        assert experiment.max_total_model_tokens == 1000
        assert experiment.promotion_min_formal_passes == 2
        assert experiment.created_by == "operator-a"
        assert experiment.closed_at is None
        assert set(service.task_ids(experiment_id)) == set(task_ids)
        # The deadline is derived from the declared wall-clock budget, not supplied. It is
        # computed from the database clock slightly before the row's own created_at, so the
        # two readings differ by microseconds rather than exactly.
        assert abs((experiment.deadline_at - experiment.created_at)
                   - timedelta(seconds=3600)) < timedelta(seconds=1)

    replay = _opened(factory, list(reversed(task_ids)),
                     limits=_limits(max_candidates=2, promotion_min_formal_passes=2))
    assert replay == experiment_id
    with pytest.raises(ExperimentConflictError):
        _opened(factory, task_ids,
                limits=_limits(max_candidates=3, promotion_min_formal_passes=2))
    with pytest.raises(ExperimentConflictError):
        _opened(factory, [task_ids[0]])
    with pytest.raises(ExperimentConflictError):
        _opened(factory, task_ids,
                limits=_limits(max_candidates=2, promotion_min_formal_passes=2),
                created_by="someone-else")
    assert _opened(factory, task_ids, identity="experiment-two") != experiment_id


def test_open_requires_a_pinned_baseline_for_every_declared_task(
        experiment_session_factory):
    factory = experiment_session_factory
    admitted = _task(factory)
    unadmitted = _unadmitted_task(factory)

    with pytest.raises(ExperimentError, match="no pinned base commit"):
        _opened(factory, [admitted, unadmitted])
    with pytest.raises(ExperimentError, match="do not exist"):
        _opened(factory, [admitted, uuid4()])
    with factory() as session:
        # Neither refusal opened anything.
        assert session.scalar(select(func.count()).select_from(Experiment)) == 0


def test_open_refuses_an_incomplete_or_out_of_range_declaration(
        experiment_session_factory):
    factory = experiment_session_factory
    task_id = _task(factory)
    for limits in (
        dict(max_candidates=0), dict(max_candidates=101), dict(max_total_model_tokens=0),
        dict(max_total_seconds=0), dict(promotion_min_formal_passes=0),
        dict(promotion_max_failures=-1),
    ):
        with pytest.raises(ExperimentLimitError):
            _limits(**limits)
    with pytest.raises(ExperimentLimitError):
        _limits(max_candidates=True)
    with pytest.raises(ExperimentError):
        _opened(factory, [])
    with pytest.raises(ExperimentError):
        _opened(factory, [task_id, task_id])
    with pytest.raises(ExperimentError):
        _opened(factory, ["not-a-uuid"])
    with pytest.raises(ExperimentError):
        _opened(factory, [task_id], base="short")
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Experiment)) == 0


def test_open_freezes_each_tasks_target_baseline(experiment_session_factory):
    factory = experiment_session_factory
    first = _task(factory, "one")
    second = _task(factory, "two", )
    experiment_id = _opened(factory, [first, second])
    with factory() as session:
        service = ExperimentService(session)
        assert service.task_baselines(experiment_id) == {first: BASE, second: BASE}
        summary = service.summary(experiment_id)
        assert summary["task_baselines"] == {str(first): BASE, str(second): BASE}
    # The same declaration replays; a changed baseline would not.
    assert _opened(factory, [first, second]) == experiment_id


def test_a_task_whose_baseline_changed_after_open_is_refused(
        experiment_session_factory):
    factory = experiment_session_factory
    task_id, run_id = _attempt(factory, status=RunStatus.FAILED)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        ExperimentService(session).admit_candidate(
            experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
    # Fixture-only mutation: the Task row is edited outside the ledger's control.
    with factory.begin() as session:
        session.get(Task, task_id).base_commit = "c" * 40
    with factory.begin() as session:
        service = ExperimentService(session)
        with pytest.raises(ExperimentConflictError, match="changed after the experiment opened"):
            service.record_task_result(
                experiment_id=experiment_id, candidate_label="a", task_id=task_id,
                run_id=run_id, outcome="failed", recorded_by="operator-a")
    with pytest.raises(ExperimentConflictError) as refusal:
        _opened(factory, [task_id])
    assert "task_baselines" in str(refusal.value)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ExperimentTaskResult)) == 0


def test_candidate_cap_lineage_and_idempotent_admission(experiment_session_factory):
    factory = experiment_session_factory
    experiment_id = _opened(factory, [_task(factory)], limits=_limits(max_candidates=2))
    with factory.begin() as session:
        service = ExperimentService(session)
        first = service.admit_candidate(experiment_id=experiment_id, label="a",
                                        execution_sha256="1" * 64)
        assert service.admit_candidate(experiment_id=experiment_id, label="a",
                                       execution_sha256="1" * 64) == first
        with pytest.raises(ExperimentConflictError):
            service.admit_candidate(experiment_id=experiment_id, label="a",
                                    execution_sha256="2" * 64)
        service.admit_candidate(experiment_id=experiment_id, label="b",
                                execution_sha256="2" * 64)
        with pytest.raises(ExperimentLimitError):
            service.admit_candidate(experiment_id=experiment_id, label="c",
                                    execution_sha256="3" * 64)
        with pytest.raises(ExperimentStateError):
            service.admit_candidate(experiment_id=experiment_id, label="d",
                                    execution_sha256="4" * 64, parent_label="missing")
    with factory.begin() as session:
        service = ExperimentService(session)
        # A concluded candidate does not free a candidate slot: the cap counts declarations.
        service.decide(experiment_id=experiment_id, candidate_label="b", outcome="rejected",
                       reason="did not compile", decided_by="operator-a")
        with pytest.raises(ExperimentLimitError):
            service.admit_candidate(experiment_id=experiment_id, label="c",
                                    execution_sha256="3" * 64)

    lineage_task = _task(factory, "lineage")
    with factory.begin() as session:
        fresh = ExperimentService(session).open(
            identity_key="lineage", base_execution_sha256="c" * 64, task_ids=[lineage_task],
            limits=_limits(), created_by="operator-a")
    with factory.begin() as session:
        service = ExperimentService(session)
        parent = service.admit_candidate(experiment_id=fresh, label="parent",
                                         execution_sha256="5" * 64)
        child = service.admit_candidate(experiment_id=fresh, label="child",
                                        execution_sha256="6" * 64, parent_label="parent")
        assert session.get(ExperimentCandidate, child).parent_id == parent


# ------------------------------------------------------------------ shared token budget


def test_budget_is_shared_by_all_candidates_not_allocated_per_candidate(
        experiment_session_factory):
    factory = experiment_session_factory
    experiment_id = _opened(factory, [_task(factory)],
                            limits=_limits(max_total_model_tokens=1000))
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        service.admit_candidate(experiment_id=experiment_id, label="b", execution_sha256="2" * 64)
        first = service.reserve(experiment_id=experiment_id, request_key=uuid4(), tokens=600,
                                candidate_label="a")
        assert first["remaining_tokens"] == 400
        with pytest.raises(ExperimentLimitError):
            service.reserve(experiment_id=experiment_id, request_key=uuid4(), tokens=500,
                            candidate_label="b")
        second = service.reserve(experiment_id=experiment_id, request_key=uuid4(), tokens=400,
                                 candidate_label="b")
        assert second["remaining_tokens"] == 0
    with factory.begin() as session:
        service = ExperimentService(session)
        settled = service.settle(experiment_id=experiment_id,
                                 request_key=first["request_key"], actual_tokens=250)
        assert settled["settled_tokens"] == 250
        assert settled["reserved_tokens"] == 400
        # The unused part of a reservation goes back: the budget is spend, not commitment.
        assert settled["remaining_tokens"] == 350
        assert settled["over_budget"] is False
        summary = service.summary(experiment_id)
        assert summary["usage"] == {
            "reserved_tokens": 400, "settled_tokens": 250, "over_budget": False,
            "remaining_tokens": 350, "outstanding_reservations": 1,
            "dispatched_unsettled": 0,
            "unknown_usage_reservations": 0, "accounting_complete": False,
            "accounting_gaps": ["1 reservation(s) not settled or released"]}
        with pytest.raises(ExperimentStateError):
            service.settle(experiment_id=experiment_id, request_key=uuid4(), actual_tokens=1)


def test_reservation_replay_is_idempotent_and_a_different_amount_is_refused(
        experiment_session_factory):
    factory = experiment_session_factory
    experiment_id = _opened(factory, [_task(factory)],
                            limits=_limits(max_total_model_tokens=900))
    key = uuid4()
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        service.reserve(experiment_id=experiment_id, request_key=key, tokens=300)
    with factory.begin() as session:
        service = ExperimentService(session)
        replay = service.reserve(experiment_id=experiment_id, request_key=key, tokens=300)
        assert replay["reserved_tokens"] == 300
        with pytest.raises(ExperimentConflictError):
            service.reserve(experiment_id=experiment_id, request_key=key, tokens=301)
        with pytest.raises(ExperimentConflictError):
            service.reserve(experiment_id=experiment_id, request_key=key, tokens=300,
                            candidate_label="a")
    with factory.begin() as session:
        service = ExperimentService(session)
        first = service.settle(experiment_id=experiment_id, request_key=key, actual_tokens=280)
        assert first["settled_tokens"] == 280
        assert service.settle(experiment_id=experiment_id, request_key=key,
                              actual_tokens=280)["settled_tokens"] == 280
        with pytest.raises(ExperimentConflictError):
            service.settle(experiment_id=experiment_id, request_key=key, actual_tokens=290)
        with pytest.raises(ExperimentStateError):
            service.release(experiment_id=experiment_id, request_key=key,
                            reason="no usage", confirmed_by="operator-a")


def test_release_needs_a_declaration_and_replays_only_the_same_one(
        experiment_session_factory):
    factory = experiment_session_factory
    experiment_id = _opened(factory, [_task(factory)],
                            limits=_limits(max_total_model_tokens=500))
    key = uuid4()
    with factory.begin() as session:
        service = ExperimentService(session)
        service.reserve(experiment_id=experiment_id, request_key=key, tokens=500)
    with factory.begin() as session:
        service = ExperimentService(session)
        with pytest.raises(ExperimentError):
            service.release(experiment_id=experiment_id, request_key=key,
                            reason="   ", confirmed_by="operator-a")
        with pytest.raises(ExperimentError):
            service.release(experiment_id=experiment_id, request_key=key,
                            reason="no usage", confirmed_by="  ")
        released = service.release(experiment_id=experiment_id, request_key=key,
                                   reason="the call was refused before dispatch",
                                   confirmed_by="operator-a")
        assert released["state"] == "released"
        assert released["reserved_tokens"] == 0
        assert released["settled_tokens"] == 0
        assert released["remaining_tokens"] == 500
        # The declaration is recorded, not inferred: a reader can see who said no usage
        # occurred, which is what separates this from "usage unknown".
        assert released["ended_by"] == "operator-a"
        assert released["ended_reason"] == "the call was refused before dispatch"
        assert service.release(
            experiment_id=experiment_id, request_key=key,
            reason="the call was refused before dispatch",
            confirmed_by="operator-a")["state"] == "released"
        with pytest.raises(ExperimentConflictError):
            service.release(experiment_id=experiment_id, request_key=key,
                            reason="a different story", confirmed_by="operator-a")
        with pytest.raises(ExperimentStateError):
            service.settle(experiment_id=experiment_id, request_key=key, actual_tokens=10)
        with pytest.raises(ExperimentStateError):
            service.settle_unknown(experiment_id=experiment_id, request_key=key,
                                   reason="unknown", recorded_by="operator-a")


def test_unknown_usage_is_recorded_as_unknown_rather_than_zero(
        experiment_session_factory):
    factory = experiment_session_factory
    task_id, run_id = _attempt(factory, "unknown cost", status=RunStatus.FAILED)
    experiment_id = _opened(factory, [task_id],
                            limits=_limits(max_total_model_tokens=500,
                                           promotion_max_failures=1))
    key = uuid4()
    with factory.begin() as session:
        service = ExperimentService(session)
        service.reserve(experiment_id=experiment_id, request_key=key, tokens=400)
    with factory.begin() as session:
        service = ExperimentService(session)
        ended = service.settle_unknown(experiment_id=experiment_id, request_key=key,
                                       reason="provider did not return usage",
                                       recorded_by="operator-a")
        assert ended["state"] == "unknown"
        assert ended["actual_tokens"] is None
        assert ended["remaining_tokens"] is None
        # The budget does not silently regain the amount and the settled total is untouched.
        assert ended["settled_tokens"] == 0
        assert ended["reserved_tokens"] == 0
        assert ended["ended_reason"] == "provider did not return usage"
        assert service.settle_unknown(
            experiment_id=experiment_id, request_key=key,
            reason="provider did not return usage", recorded_by="operator-a")["state"] == "unknown"
        with pytest.raises(ExperimentConflictError):
            service.settle_unknown(experiment_id=experiment_id, request_key=key,
                                   reason="something else", recorded_by="operator-a")
        summary = service.summary(experiment_id)
        assert summary["usage"]["unknown_usage_reservations"] == 1
        assert summary["usage"]["accounting_complete"] is False
        assert summary["usage"]["remaining_tokens"] is None
        with pytest.raises(ExperimentLimitError, match="Unknown usage"):
            service.reserve(experiment_id=experiment_id, request_key=uuid4(), tokens=1)
        assert "unknown usage" in " ".join(summary["usage"]["accounting_gaps"])
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        service.record_task_result(
            experiment_id=experiment_id, candidate_label="a", task_id=task_id, run_id=run_id,
            outcome="failed", recorded_by="operator-a")
        with pytest.raises(ExperimentLimitError) as refusal:
            service.decide(experiment_id=experiment_id, candidate_label="a", outcome="accepted",
                           reason="should not be possible", decided_by="operator-a")
        assert "accounting is incomplete" in str(refusal.value)


def test_settling_past_the_budget_is_recorded_as_over_budget(experiment_session_factory):
    factory = experiment_session_factory
    task_id, run_id = _attempt(factory)
    experiment_id = _opened(factory, [task_id], limits=_limits(max_total_model_tokens=100))
    key = uuid4()
    with factory.begin() as session:
        service = ExperimentService(session)
        service.reserve(experiment_id=experiment_id, request_key=key, tokens=100)
    with factory.begin() as session:
        service = ExperimentService(session)
        settled = service.settle(experiment_id=experiment_id, request_key=key, actual_tokens=140)
        assert settled["settled_tokens"] == 140
        assert settled["over_budget"] is True
    _trusted_verification(factory, run_id)
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        service.record_task_result(
            experiment_id=experiment_id, candidate_label="a", task_id=task_id, run_id=run_id,
            outcome="formal_pass", recorded_by="operator-a")
        # The overrun is recorded rather than hidden, and it blocks a promotion decision.
        with pytest.raises(ExperimentLimitError):
            service.decide(experiment_id=experiment_id, candidate_label="a", outcome="accepted",
                           reason="looks good", decided_by="operator-a")
    with factory() as session:
        assert session.get(Experiment, experiment_id).over_budget is True


def test_concurrent_reservations_cannot_oversubscribe_the_shared_budget(
        experiment_session_factory):
    factory = experiment_session_factory
    experiment_id = _opened(factory, [_task(factory)],
                            limits=_limits(max_total_model_tokens=1000))

    def attempt(_index):
        with factory.begin() as session:
            try:
                ExperimentService(session).reserve(
                    experiment_id=experiment_id, request_key=uuid4(), tokens=300)
                return "reserved"
            except ExperimentLimitError:
                return "refused"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(8)))
    assert outcomes.count("reserved") == 3
    assert outcomes.count("refused") == 5
    with factory() as session:
        assert session.get(Experiment, experiment_id).reserved_tokens == 900
        assert session.scalar(select(func.count()).select_from(ExperimentReservation).where(
            ExperimentReservation.experiment_id == experiment_id)) == 3


# ------------------------------------------------------------------ recorded verdicts


def test_a_result_must_describe_a_finished_attempt_on_a_declared_task(
        experiment_session_factory):
    factory = experiment_session_factory
    task_id, running = _attempt(factory, "declared", finished=False)
    other_id, foreign = _attempt(factory, "not declared", status=RunStatus.FAILED)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        with pytest.raises(ExperimentEvidenceError):
            service.record_task_result(
                experiment_id=experiment_id, candidate_label="a", task_id=task_id,
                run_id=running, outcome="incomplete", recorded_by="operator-a")
        with pytest.raises(ExperimentEvidenceError):
            service.record_task_result(
                experiment_id=experiment_id, candidate_label="a", task_id=task_id,
                run_id=uuid4(), outcome="failed", recorded_by="operator-a")
        with pytest.raises(ExperimentEvidenceError):
            service.record_task_result(
                experiment_id=experiment_id, candidate_label="a", task_id=task_id,
                run_id=foreign, outcome="failed", recorded_by="operator-a")
        with pytest.raises(ExperimentStateError):
            service.record_task_result(
                experiment_id=experiment_id, candidate_label="a", task_id=other_id,
                run_id=foreign, outcome="failed", recorded_by="operator-a")
        with pytest.raises(ExperimentError):
            service.record_task_result(
                experiment_id=experiment_id, candidate_label="a", task_id=task_id,
                run_id=foreign, outcome="formal_pass ", recorded_by="operator-a")
        with pytest.raises(ExperimentError):
            service.record_task_result(
                experiment_id=experiment_id, candidate_label="a", task_id=task_id,
                run_id=foreign, outcome="failed", recorded_by="  ")
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ExperimentTaskResult)) == 0


def test_formal_pass_needs_a_recorded_trusted_verification(experiment_session_factory):
    factory = experiment_session_factory
    public_task, public_run = _attempt(factory, "untrusted scope")
    _trusted_verification(factory, public_run, scope="public_v0")
    receipt_task, receipt_run = _attempt(factory, "missing receipt")
    _trusted_verification(factory, receipt_run, receipt=False)
    failed_task, failed_run = _attempt(factory, "failed run", status=RunStatus.FAILED)
    _trusted_verification(factory, failed_run)
    trusted_task, trusted_run = _attempt(factory, "trusted")
    _trusted_verification(factory, trusted_run)
    experiment_id = _opened(
        factory, [public_task, receipt_task, failed_task, trusted_task])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        for task_id, run_id in ((public_task, public_run), (receipt_task, receipt_run),
                                (failed_task, failed_run)):
            with pytest.raises(ExperimentEvidenceError):
                service.record_task_result(
                    experiment_id=experiment_id, candidate_label="a", task_id=task_id,
                    run_id=run_id, outcome="formal_pass", recorded_by="operator-a")
        recorded = service.record_task_result(
            experiment_id=experiment_id, candidate_label="a", task_id=trusted_task,
            run_id=trusted_run, outcome="formal_pass", recorded_by="operator-a")
        assert recorded["formal_passes"] == 1
        assert recorded["non_formal_results"] == 0
    # A non-formal verdict needs no formal chain, and is not counted as a pass either.
    with factory.begin() as session:
        service = ExperimentService(session)
        failed = service.record_task_result(
            experiment_id=experiment_id, candidate_label="a", task_id=failed_task,
            run_id=failed_run, outcome="failed", recorded_by="operator-a")
        assert failed["formal_passes"] == 1
        assert failed["non_formal_results"] == 1


def test_a_recorded_verdict_cannot_be_relabelled(experiment_session_factory):
    factory = experiment_session_factory
    task_id, run_id = _attempt(factory, status=RunStatus.FAILED)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        service.record_task_result(
            experiment_id=experiment_id, candidate_label="a", task_id=task_id, run_id=run_id,
            outcome="failed", recorded_by="operator-a")
    with factory.begin() as session:
        service = ExperimentService(session)
        replay = service.record_task_result(
            experiment_id=experiment_id, candidate_label="a", task_id=task_id, run_id=run_id,
            outcome="failed", recorded_by="another-operator")
        assert replay["non_formal_results"] == 1
        with pytest.raises(ExperimentConflictError):
            service.record_task_result(
                experiment_id=experiment_id, candidate_label="a", task_id=task_id,
                run_id=run_id, outcome="timeout", recorded_by="operator-a")
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ExperimentTaskResult)) == 1


# ------------------------------------------ attribution and classifier-derived verdicts


def _formal_attempt(factory, tmp_path, title, *, execution_sha256=None):
    """One Task/Run with a Verifier draft and receipt the shared classifier can load back."""
    task_id, run_id = _attempt(factory, title, execution_sha256=execution_sha256)
    artifacts = Path(tmp_path) / "artifacts" / str(run_id)
    artifacts.mkdir(parents=True)
    verification_id, written = _finish_formally(factory, artifacts, run_id=run_id)
    return task_id, run_id, artifacts, verification_id


def _record_classified(service, experiment_id, *, label, task_id, run_id, artifacts_root,
                       suite_version=SUITE_VERSION, deadline_context=None,
                       context_source="fixture_attempt", finalization=None, record_path=None):
    return service.record_classified_result(
        experiment_id=experiment_id, candidate_label=label, task_id=task_id, run_id=run_id,
        artifacts_root=artifacts_root, independent_suite_version=suite_version,
        context_source=context_source, recorded_by="operator-a", finalization=finalization,
        deadline_context=deadline_context, record_path=record_path)


def _attempt_record(tmp_path, *, task_id, run_id, artifacts, outcome="formal_pass",
                    deadline="not_fired", verification_id=None, suite_version=SUITE_VERSION,
                    recorded_suite=None, finalization="recorded",
                    finalization_outcome="skipped", context=True, attempt_kind="formal",
                    execution_sha256=None, name=None):
    """The run record the attempt's own entry would have written, for the ledger to read.

    It is a constructed file, but it is produced by the same model and writer the real entries
    use, so the ledger reads it exactly as it reads a real one.
    """
    attempt_context = None
    if context:
        attempt_context = AttemptContext(
            deadline=deadline, finalization=finalization,
            finalization_outcome=(finalization_outcome
                                  if finalization == "recorded" else None),
            execution_sha256=execution_sha256, source="benchmark_attempt")
    record = BenchmarkRunRecord(
        schema_version=SCHEMA_VERSION, pack_id="fixture-pack", base_commit=BASE,
        task_id=str(task_id), run_id=str(run_id), attempt_index=1,
        started_at="2026-11-18T00:00:00Z", finished_at="2026-11-18T00:05:00Z",
        outcome=RunOutcome(outcome), model_id="fixture-model",
        policy_version="fixture-policy", operator="operator-a",
        artifacts=(str(artifacts),), attempt_kind=AttemptKind(attempt_kind),
        attempt_context=attempt_context,
        formal=(FormalEvidence(verification_id=str(verification_id),
                               independent_suite_version=suite_version)
                if outcome == "formal_pass" else None),
        independent_evaluation=(IndependentEvaluationEvidence(
            outcome="unavailable", verified=False, reason="fixture pin", suite_version=recorded_suite)
            if recorded_suite is not None else None))
    path = Path(tmp_path) / "records" / (name or f"{run_id}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    write_run_record(record, path)
    return path


def test_concurrent_classified_recording_is_idempotent(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "concurrent classified", execution_sha256="1" * 64)
    record_path = _attempt_record(tmp_path, task_id=task_id, run_id=run_id,
                                  artifacts=artifacts, verification_id=verification_id)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        ExperimentService(session).admit_candidate(
            experiment_id=experiment_id, label="a", execution_sha256="1" * 64)

    def record(_index):
        with factory.begin() as session:
            try:
                ExperimentService(session).record_classified_result(
                    experiment_id=experiment_id, candidate_label="a", task_id=task_id,
                    run_id=run_id, artifacts_root=artifacts,
                    independent_suite_version=SUITE_VERSION, context_source="run_record",
                    recorded_by="operator-a", record_path=record_path)
                return "recorded"
            except ExperimentError as error:
                return f"refused:{type(error).__name__}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(record, range(2)))
    # One attempt, one row: the same identical submission is a replay, not a second verdict.
    # The experiment row lock serialises the two writers, so both see a consistent state.
    assert outcomes == ["recorded", "recorded"]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ExperimentTaskResult)) == 1


def test_a_closed_experiment_takes_no_classified_result(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "closed", execution_sha256="1" * 64)
    record_path = _attempt_record(tmp_path, task_id=task_id, run_id=run_id,
                                  artifacts=artifacts, verification_id=verification_id)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a",
                                execution_sha256="1" * 64)
        service.close(experiment_id=experiment_id, reason="round complete")
    with factory.begin() as session:
        service = ExperimentService(session)
        with pytest.raises(ExperimentStateError) as refusal:
            _record_classified(service, experiment_id, label="a", task_id=task_id,
                               run_id=run_id, artifacts_root=artifacts,
                               context_source="run_record", record_path=record_path)
        assert "closed" in str(refusal.value)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ExperimentTaskResult)) == 0


def test_a_run_cannot_be_attributed_to_two_candidates(experiment_session_factory):
    factory = experiment_session_factory
    task_id, run_id = _attempt(factory, status=RunStatus.FAILED)
    experiment_id = _opened(factory, [task_id],
                            limits=_limits(max_candidates=2, promotion_max_failures=5))
    with factory.begin() as session:
        service = ExperimentService(session)
        first = service.admit_candidate(experiment_id=experiment_id, label="a",
                                        execution_sha256="1" * 64)
        second = service.admit_candidate(experiment_id=experiment_id, label="b",
                                         execution_sha256="2" * 64)
        service.record_task_result(
            experiment_id=experiment_id, candidate_label="a", task_id=task_id, run_id=run_id,
            outcome="failed", recorded_by="operator-a")
        with pytest.raises(ExperimentConflictError) as refusal:
            service.record_task_result(
                experiment_id=experiment_id, candidate_label="b", task_id=task_id,
                run_id=run_id, outcome="failed", recorded_by="operator-a")
        assert "one Run cannot be attributed to two candidates" in str(refusal.value)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(
            ExperimentTaskResult).where(ExperimentTaskResult.run_id == run_id)) == 1
    # The rule is a database constraint, not only a service check: a second row for the same
    # Run is refused even when it is written directly.
    with pytest.raises(IntegrityError):
        with factory.begin() as session:
            session.add(ExperimentTaskResult(
                experiment_id=experiment_id, candidate_id=second, task_id=task_id,
                run_id=run_id, outcome="failed", recorded_by="operator-a"))
            session.flush()
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(
            ExperimentTaskResult).where(ExperimentTaskResult.run_id == run_id)) == 1
        assert session.get(ExperimentCandidate, first) is not None


def test_concurrent_attribution_cannot_record_one_run_twice(experiment_session_factory):
    factory = experiment_session_factory
    task_id, run_id = _attempt(factory, status=RunStatus.FAILED)
    experiment_id = _opened(factory, [task_id],
                            limits=_limits(max_candidates=2, promotion_max_failures=5))
    with factory.begin() as session:
        service = ExperimentService(session)
        for label, digest in (("a", "1" * 64), ("b", "2" * 64)):
            service.admit_candidate(experiment_id=experiment_id, label=label,
                                    execution_sha256=digest)

    def attempt(label):
        with factory.begin() as session:
            try:
                ExperimentService(session).record_task_result(
                    experiment_id=experiment_id, candidate_label=label, task_id=task_id,
                    run_id=run_id, outcome="failed", recorded_by="operator-a")
                return "recorded"
            except ExperimentConflictError:
                return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ("a", "b")))
    # The experiment row lock serialises the two writers and the constraint refuses the
    # second: the same attempt is never counted in both arms.
    assert sorted(outcomes) == ["recorded", "refused"]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(
            ExperimentTaskResult).where(ExperimentTaskResult.run_id == run_id)) == 1


def test_a_run_with_a_different_execution_version_cannot_be_attributed(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    # The Run records the version its entry verified; the candidate names another one.
    task_id, run_id, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "other version", execution_sha256="2" * 64)
    record_path = _attempt_record(tmp_path, task_id=task_id, run_id=run_id, artifacts=artifacts,
                                  verification_id=verification_id)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a",
                                execution_sha256="1" * 64)
        with pytest.raises(ExperimentConflictError) as refusal:
            _record_classified(service, experiment_id, label="a", task_id=task_id,
                               run_id=run_id, artifacts_root=artifacts,
                               context_source="run_record", record_path=record_path)
        assert "different execution version" in str(refusal.value)
        with pytest.raises(ExperimentConflictError):
            service.record_task_result(
                experiment_id=experiment_id, candidate_label="a", task_id=task_id,
                run_id=run_id, outcome="failed", recorded_by="operator-a")
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ExperimentTaskResult)) == 0


def test_a_run_without_a_verified_version_is_recorded_but_never_promotable(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    # No manifest was pinned for this attempt, so the Run records no version.
    task_id, run_id, artifacts, verification_id = _formal_attempt(factory, tmp_path, "unbound")
    record_path = _attempt_record(tmp_path, task_id=task_id, run_id=run_id, artifacts=artifacts,
                                  verification_id=verification_id)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a",
                                execution_sha256="1" * 64)
        recorded = _record_classified(service, experiment_id, label="a", task_id=task_id,
                                      run_id=run_id, artifacts_root=artifacts,
                                      context_source="run_record", record_path=record_path)
        # Everything else about this attempt is right; the version is unknown, which alone
        # keeps it out of the promotable set.
        assert recorded["outcome"] == "formal_pass"
        assert recorded["context_evidence"] == "record"
        assert recorded["tasks_with_formal_pass"] == 0
        summary = service.summary(experiment_id)
        candidate = summary["candidates"][0]
        assert candidate["execution_binding"] == "no_verified_run"
        assert candidate["results"][0]["run_execution_sha256"] is None
        with pytest.raises(ExperimentLimitError):
            service.decide(experiment_id=experiment_id, candidate_label="a", outcome="accepted",
                           reason="unbound run", decided_by="operator-a")


def test_classified_result_records_what_the_shared_classifier_derives(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "trusted", execution_sha256="1" * 64)
    record_path = _attempt_record(tmp_path, task_id=task_id, run_id=run_id, artifacts=artifacts,
                                  verification_id=verification_id)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        recorded = _record_classified(service, experiment_id, label="a", task_id=task_id,
                                      run_id=run_id, artifacts_root=artifacts,
                                      record_path=record_path)
        assert recorded["outcome"] == "formal_pass"
        assert recorded["evidence_source"] == "classified"
        assert recorded["verification_id"] == str(verification_id)
        assert recorded["classifier_reason"] is None
        assert recorded["tasks_with_formal_pass"] == 1
        assert recorded["formal_passes"] == 1
        # The context came from the record and the record's own bytes are named.
        assert recorded["context_evidence"] == "record"
        assert recorded["deadline_context"] == "not_fired"
        assert recorded["record_outcome"] == "formal_pass"
        assert recorded["tasks_with_formal_pass"] == 1
        assert recorded["record_sha256"] == sha256(Path(record_path).read_bytes()).hexdigest()
        replay = _record_classified(service, experiment_id, label="a", task_id=task_id,
                                    run_id=run_id, artifacts_root=artifacts,
                                    record_path=record_path)
        assert replay["tasks_with_formal_pass"] == 1
        summary = service.summary(experiment_id)
        candidate = summary["candidates"][0]
        assert candidate["execution_binding"] == "verified_runs"
        assert candidate["results"][0]["run_execution_sha256"] == "1" * 64
    with factory() as session:
        row = session.scalar(select(ExperimentTaskResult))
        assert row.evidence_source == "classified"
        assert row.verification_id == verification_id
        assert row.decision_reason is None
        assert row.outcome == "formal_pass"
        assert row.context_evidence == "record"
        assert row.record_sha256 == sha256(Path(record_path).read_bytes()).hexdigest()


def test_the_ledger_freezes_the_runs_version_at_record_time(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "frozen version", execution_sha256="1" * 64)
    record_path = _attempt_record(tmp_path, task_id=task_id, run_id=run_id, artifacts=artifacts,
                                  verification_id=verification_id)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a",
                                execution_sha256="1" * 64)
        recorded = _record_classified(service, experiment_id, label="a", task_id=task_id,
                                      run_id=run_id, artifacts_root=artifacts,
                                      context_source="run_record", record_path=record_path)
        assert recorded["run_execution_sha256"] == "1" * 64
        assert recorded["tasks_with_formal_pass"] == 1
    # Fixture-only mutation: someone edits the Run row afterwards. The ledger keeps what it
    # saw when it wrote the result, so the attempt cannot be re-labelled into another arm.
    with factory.begin() as session:
        session.get(Run, run_id).execution_sha256 = "9" * 64
    with factory() as session:
        service = ExperimentService(session)
        row = session.scalar(select(ExperimentTaskResult))
        assert row.run_execution_sha256 == "1" * 64
        summary = service.summary(experiment_id)
        candidate = summary["candidates"][0]
        assert candidate["results"][0]["run_execution_sha256"] == "1" * 64
        assert candidate["tasks_with_classified_formal_pass"] == 1
        assert candidate["execution_binding"] == "verified_runs"


def test_classified_result_does_not_invent_a_formal_pass_the_chain_cannot_support(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    # (a) the Verification row claims a trusted pass, but the receipt is not on disk any more.
    task_a, run_a, artifacts_a, _ = _formal_attempt(factory, tmp_path, "receipt removed")
    with factory() as session:
        receipt_path = session.scalar(select(Artifact.content_ref).where(
            Artifact.kind == "independent_evaluation",
            Artifact.agent_execution_id.in_(select(AgentExecution.id).where(
                AgentExecution.run_id == run_a))))
    Path(receipt_path).unlink()
    # (b) the pack pins a different suite version than the receipt names.
    task_b, run_b, artifacts_b, _ = _formal_attempt(factory, tmp_path, "suite mismatch")
    # (c) the independent evaluation recorded a failure, not a pass.
    task_c, run_c = _attempt(factory, "receipt failed")
    artifacts_c = Path(tmp_path) / "artifacts" / str(run_c)
    artifacts_c.mkdir(parents=True)
    _finish_formally(factory, artifacts_c, run_id=run_c, receipt_outcome="failed")
    experiment_id = _opened(factory, [task_a, task_b, task_c])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        first = _record_classified(service, experiment_id, label="a", task_id=task_a,
                                   run_id=run_a, artifacts_root=artifacts_a,
                                   deadline_context="not_fired")
        second = _record_classified(service, experiment_id, label="a", task_id=task_b,
                                    run_id=run_b, artifacts_root=artifacts_b,
                                    suite_version="some-other-suite",
                                    deadline_context="not_fired")
        third = _record_classified(service, experiment_id, label="a", task_id=task_c,
                                   run_id=run_c, artifacts_root=artifacts_c,
                                   deadline_context="not_fired")
        for item in (first, second):
            assert item["outcome"] == "incomplete"
            assert item["verification_id"] is None
            assert item["classifier_reason"]
        # The receipt says failed while the Verification row claims a pass: whatever the shared
        # rules make of that, they never turn it into a formal pass.
        assert third["outcome"] != "formal_pass"
        assert third["verification_id"] is None
        assert third["tasks_with_formal_pass"] == 0
    # The same evidence recorded as a declaration would have been accepted as a formal pass —
    # which is exactly why declared results never count towards the thresholds.
    declared_experiment = _opened(factory, [task_a], identity="declared-contrast")
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=declared_experiment, label="a",
                                execution_sha256="1" * 64)
        declared = service.record_task_result(
            experiment_id=declared_experiment, candidate_label="a", task_id=task_a,
            run_id=run_a, outcome="formal_pass", recorded_by="operator-a")
        assert declared["outcome"] == "formal_pass"
        assert declared["evidence_source"] == "declared"
        assert declared["verification_id"] is None


def test_classified_result_needs_the_artifact_root_and_a_finished_run(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id = _attempt(factory, finished=False)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        with pytest.raises(ExperimentEvidenceError):
            _record_classified(service, experiment_id, label="a", task_id=task_id,
                               run_id=run_id, artifacts_root=tmp_path / "missing",
                               deadline_context="not_fired")
        with pytest.raises(ExperimentEvidenceError):
            _record_classified(service, experiment_id, label="a", task_id=task_id,
                               run_id=run_id, artifacts_root=tmp_path,
                               deadline_context="not_fired")
        with pytest.raises(ExperimentError):
            service.record_classified_result(
                experiment_id=experiment_id, candidate_label="a", task_id=task_id,
                run_id=run_id, artifacts_root=tmp_path,
                independent_suite_version="  ", deadline_context="not_fired",
                context_source="fixture_attempt", recorded_by="operator-a")


def test_classified_result_needs_a_context_source_and_never_infers_one(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, _ = _formal_attempt(factory, tmp_path, "context required")
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        common = dict(experiment_id=experiment_id, candidate_label="a", task_id=task_id,
                      run_id=run_id, artifacts_root=artifacts,
                      independent_suite_version=SUITE_VERSION, recorded_by="operator-a")
        # Neither source supplied: "not fired" may not be implied by omission.
        with pytest.raises(ExperimentError):
            service.record_classified_result(**common, context_source="fixture_attempt")
        with pytest.raises(ExperimentError):
            service.record_classified_result(**common, deadline_context="maybe",
                                             context_source="fixture_attempt")
        with pytest.raises(ExperimentError):
            service.record_classified_result(**common, deadline_context="not_fired",
                                             context_source="   ")
        with pytest.raises(ExperimentError):
            service.record_classified_result(**common, deadline_context="not_fired",
                                             context_source="x" * 201)
        with pytest.raises(ExperimentError):
            service.record_classified_result(**common, deadline_context="not_fired",
                                             context_source="fixture_attempt",
                                             finalization="not-a-dict")
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ExperimentTaskResult)) == 0


def test_a_fired_deadline_outranks_a_formal_pass(experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, _ = _formal_attempt(factory, tmp_path, "deadline fired")
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        recorded = _record_classified(service, experiment_id, label="a", task_id=task_id,
                                      run_id=run_id, artifacts_root=artifacts,
                                      deadline_context="fired",
                                      context_source="benchmark_attempt")
        # The same Run carries a loadable formal chain, but a fired deadline is a timeout.
        assert recorded["outcome"] == "timeout"
        assert recorded["verification_id"] is None
        assert recorded["tasks_with_formal_pass"] == 0
        assert recorded["deadline_context"] == "fired"
        assert recorded["context_source"] == "benchmark_attempt"


def test_unknown_deadline_context_records_the_pass_but_is_never_promotable(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "unknown", execution_sha256="1" * 64)
    # A record written before the context field existed cannot say when the attempt ended.
    record_path = _attempt_record(tmp_path, task_id=task_id, run_id=run_id, artifacts=artifacts,
                                  verification_id=verification_id, context=False)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        recorded = _record_classified(service, experiment_id, label="a", task_id=task_id,
                                      run_id=run_id, artifacts_root=artifacts,
                                      context_source="run_record_without_context",
                                      record_path=record_path)
        # The verdict is recorded honestly — the chain really did load — but the attempt's end
        # conditions are unknown, so it may not stand in for a promotion sample.
        assert recorded["outcome"] == "formal_pass"
        assert recorded["verification_id"] == str(verification_id)
        assert recorded["context_evidence"] == "record"
        assert recorded["deadline_context"] == "unknown"
        assert recorded["tasks_with_formal_pass"] == 0
        summary = service.summary(experiment_id)
        candidate = summary["candidates"][0]
        assert candidate["results_with_unknown_deadline_context"] == 1
        assert candidate["tasks_with_classified_formal_pass"] == 0
        assert candidate["results"][0]["deadline_context"] == "unknown"
        assert candidate["results"][0]["record_sha256"] == sha256(
            Path(record_path).read_bytes()).hexdigest()
        with pytest.raises(ExperimentLimitError) as refusal:
            service.decide(experiment_id=experiment_id, candidate_label="a", outcome="accepted",
                           reason="pass recorded", decided_by="operator-a")
        assert "distinct Tasks with a classified formal pass 0 < 1" in str(refusal.value)


def test_a_declared_context_is_recorded_but_never_promotable(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, _ = _formal_attempt(
        factory, tmp_path, "declared context", execution_sha256="1" * 64)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        recorded = _record_classified(service, experiment_id, label="a", task_id=task_id,
                                      run_id=run_id, artifacts_root=artifacts,
                                      deadline_context="not_fired",
                                      context_source="operator_recall")
        # A caller's statement is kept apart from a record: the verdict is real, the context
        # is not evidence, so it does not enter the promotable set.
        assert recorded["outcome"] == "formal_pass"
        assert recorded["context_evidence"] == "declared"
        assert recorded["record_sha256"] is None
        assert recorded["tasks_with_formal_pass"] == 0
        summary = service.summary(experiment_id)
        assert summary["candidates"][0]["results_with_declared_context"] == 1
        with pytest.raises(ExperimentLimitError) as refusal:
            service.decide(experiment_id=experiment_id, candidate_label="a", outcome="accepted",
                           reason="declared context", decided_by="operator-a")
        assert "distinct Tasks with a classified formal pass 0 < 1" in str(refusal.value)


def test_a_rehearsal_record_is_evidence_but_not_a_promotion_sample(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "rehearsal", execution_sha256="1" * 64)
    # A constructed rehearsal whose chain happens to load: the verdict is real, the attempt
    # kind is not, and the ledger must keep both facts apart.
    record_path = _attempt_record(tmp_path, task_id=task_id, run_id=run_id, artifacts=artifacts,
                                  verification_id=verification_id, attempt_kind="rehearsal")
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        recorded = _record_classified(service, experiment_id, label="a", task_id=task_id,
                                      run_id=run_id, artifacts_root=artifacts,
                                      context_source="run_record", record_path=record_path)
        assert recorded["outcome"] == "formal_pass"
        assert recorded["attempt_kind"] == "rehearsal"
        assert recorded["tasks_with_formal_pass"] == 0
        assert service.summary(experiment_id)["candidates"][0]["results"][0]["attempt_kind"] == (
            "rehearsal")
        with pytest.raises(ExperimentLimitError):
            service.decide(experiment_id=experiment_id, candidate_label="a", outcome="accepted",
                           reason="rehearsal pass", decided_by="operator-a")


def test_a_run_record_can_carry_a_fired_deadline_into_a_timeout(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, _ = _formal_attempt(factory, tmp_path, "record fired")
    record_path = _attempt_record(tmp_path, task_id=task_id, run_id=run_id, artifacts=artifacts,
                                  outcome="timeout", deadline="fired",
                                  finalization="recorded", finalization_outcome="not_finalized")
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        recorded = _record_classified(service, experiment_id, label="a", task_id=task_id,
                                      run_id=run_id, artifacts_root=artifacts,
                                      context_source="run_record",
                                      record_path=record_path)
        # The record says the deadline fired; the classifier agrees even though a formal chain
        # is on disk, because a fired deadline outranks a formal pass.
        assert recorded["outcome"] == "timeout"
        assert recorded["deadline_context"] == "fired"
        assert recorded["context_evidence"] == "record"
        assert recorded["tasks_with_formal_pass"] == 0


def test_a_timeout_may_be_recorded_for_a_run_the_worker_left_running(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id = _attempt(factory, "timed out", finished=False)
    artifacts = Path(tmp_path) / "artifacts" / str(run_id)
    artifacts.mkdir(parents=True)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        # A stopped attempt leaves the Run RUNNING; that alone never justifies a verdict.
        for context in ("not_fired", "unknown"):
            with pytest.raises(ExperimentEvidenceError):
                _record_classified(service, experiment_id, label="a", task_id=task_id,
                                   run_id=run_id, artifacts_root=artifacts,
                                   deadline_context=context)
        recorded = _record_classified(service, experiment_id, label="a", task_id=task_id,
                                      run_id=run_id, artifacts_root=artifacts,
                                      deadline_context="fired",
                                      context_source="deadline_watch")
        assert recorded["outcome"] == "timeout"
        assert recorded["verification_id"] is None
        assert recorded["tasks_with_formal_pass"] == 0
    with factory() as session:
        assert session.get(Run, run_id).status == RunStatus.RUNNING
        row = session.scalar(select(ExperimentTaskResult))
        assert row.outcome == "timeout"
        assert row.deadline_context == "fired"


def test_finalization_is_passed_through_and_never_invented(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, _ = _formal_attempt(factory, tmp_path, "finalization")
    bare_task, bare_run = _attempt(factory, "no finalization", status=RunStatus.FAILED)
    experiment_id = _opened(factory, [task_id, bare_task])
    bare_artifacts = Path(artifacts).parent / str(bare_run)
    bare_artifacts.mkdir(parents=True)
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        with_finalization = _record_classified(
            service, experiment_id, label="a", task_id=task_id, run_id=run_id,
            artifacts_root=artifacts, finalization={"outcome": "skipped"},
            deadline_context="not_fired", context_source="live_finalization")
        assert with_finalization["finalization_supplied"] is True
        # The formal branch does not depend on the finalization dict, so a supplied one does
        # not change a loadable formal pass.
        assert with_finalization["outcome"] == "formal_pass"
        without = _record_classified(service, experiment_id, label="a", task_id=bare_task,
                                     run_id=bare_run, artifacts_root=bare_artifacts,
                                     deadline_context="not_fired")
        assert without["finalization_supplied"] is False
        assert without["outcome"] == "failed"
    with factory() as session:
        rows = {row.run_id: row for row in session.scalars(select(ExperimentTaskResult))}
        assert rows[run_id].deadline_context == "not_fired"
        assert rows[bare_run].outcome == "failed"


def test_a_formal_pass_without_a_verified_baseline_cannot_be_promoted(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "no baseline", execution_sha256="1" * 64)
    record_path = _attempt_record(tmp_path, task_id=task_id, run_id=run_id,
                                  artifacts=artifacts, verification_id=verification_id)
    experiment_id = _opened(factory, [task_id],
                            limits=_limits(promotion_min_formal_passes=1))
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a",
                                execution_sha256="1" * 64)
        recorded = _record_classified(service, experiment_id, label="a", task_id=task_id,
                                      run_id=run_id, artifacts_root=artifacts,
                                      context_source="run_record", record_path=record_path)
        # Worth recording, and worth exactly nothing as a promotion sample.
        assert recorded["outcome"] == "formal_pass"
        assert recorded["workspace_base_commit"] is None
        explained = service.explain_acceptance(experiment_id, "a")
        baseline = next(item for item in explained["conditions"]
                        if item["condition"] == "promotable_passes_have_verified_baseline")
        assert baseline["met"] is False
        assert "no verified target baseline" in baseline["detail"]
        with pytest.raises(ExperimentLimitError) as refusal:
            service.decide(experiment_id=experiment_id, candidate_label="a", outcome="accepted",
                           reason="pass without baseline", decided_by="operator-a")
        assert "no verified target baseline" in str(refusal.value)


def test_a_registered_record_is_traced_to_its_registration(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "registered", execution_sha256="1" * 64)
    _checkout_tool_call(factory, run_id, BASE)
    record_path = _attempt_record(tmp_path, task_id=task_id, run_id=run_id,
                                  artifacts=artifacts, verification_id=verification_id)
    experiment_id = _opened(factory, [task_id], limits=_limits(promotion_min_formal_passes=1))
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a",
                                execution_sha256="1" * 64)
        registered = _registered(service, experiment_id, label="a", task_id=task_id,
                                 record_path=record_path)
        assert registered["record_sha256"] == sha256(Path(record_path).read_bytes()).hexdigest()
        recorded = _record_classified(service, experiment_id, label="a", task_id=task_id,
                                      run_id=run_id, artifacts_root=artifacts,
                                      context_source="run_record", record_path=record_path)
        assert recorded["record_provenance"] == "control_plane_registered"
        summary = service.summary(experiment_id)
        candidate = summary["candidates"][0]
        assert candidate["record_origin"] == "registered"
        assert candidate["results_with_registered_record"] == 1
        origin = service.explain_acceptance(experiment_id, "a")["conditions"][-1]
        assert origin["condition"] == "promotable_passes_have_registered_records"
        assert origin["met"] is True


def test_a_record_changed_after_registration_is_refused(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "tampered", execution_sha256="1" * 64)
    record_path = _attempt_record(tmp_path, task_id=task_id, run_id=run_id,
                                  artifacts=artifacts, verification_id=verification_id)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a",
                                execution_sha256="1" * 64)
        _registered(service, experiment_id, label="a", task_id=task_id,
                    record_path=record_path)
    # The file is edited after the control plane fixed its digest.
    payload = json.loads(Path(record_path).read_text(encoding="utf-8"))
    payload["notes"] = (payload.get("notes") or "") + " (edited)"
    Path(record_path).write_text(json.dumps(payload), encoding="utf-8")
    with factory.begin() as session:
        service = ExperimentService(session)
        with pytest.raises(ExperimentConflictError) as refusal:
            _record_classified(service, experiment_id, label="a", task_id=task_id,
                               run_id=run_id, artifacts_root=artifacts,
                               context_source="run_record", record_path=record_path)
        assert "fixed a different record for this attempt" in str(refusal.value)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ExperimentTaskResult)) == 0


def test_a_record_registered_for_another_candidate_is_refused(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "crossed", execution_sha256="1" * 64)
    record_path = _attempt_record(tmp_path, task_id=task_id, run_id=run_id,
                                  artifacts=artifacts, verification_id=verification_id)
    experiment_id = _opened(factory, [task_id], limits=_limits(max_candidates=2))
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a",
                                execution_sha256="1" * 64)
        service.admit_candidate(experiment_id=experiment_id, label="b",
                                execution_sha256="1" * 64)
        _registered(service, experiment_id, label="a", task_id=task_id,
                    record_path=record_path)
    with factory.begin() as session:
        service = ExperimentService(session)
        with pytest.raises(ExperimentConflictError) as refusal:
            _record_classified(service, experiment_id, label="b", task_id=task_id,
                               run_id=run_id, artifacts_root=artifacts,
                               context_source="run_record", record_path=record_path)
        assert "fixed a different record for this attempt" in str(refusal.value)


def test_two_runs_of_one_task_each_fix_their_own_record(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    first_task, first_run, first_artifacts, first_verification = _formal_attempt(
        factory, tmp_path, "attempt one", execution_sha256="1" * 64)
    repeat_run = _second_run(factory, first_task, execution_sha256="1" * 64)
    repeat_artifacts = Path(tmp_path) / "artifacts" / str(repeat_run)
    repeat_artifacts.mkdir(parents=True)
    repeat_verification, _ = _finish_formally(factory, repeat_artifacts, run_id=repeat_run)
    first_record = _attempt_record(tmp_path, task_id=first_task, run_id=first_run,
                                   artifacts=first_artifacts,
                                   verification_id=first_verification, name="run1.json")
    repeat_record = _attempt_record(tmp_path, task_id=first_task, run_id=repeat_run,
                                    artifacts=repeat_artifacts,
                                    verification_id=repeat_verification, name="run2.json")
    experiment_id = _opened(factory, [first_task])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a",
                                execution_sha256="1" * 64)
        one = _registered(service, experiment_id, label="a", task_id=first_task,
                          record_path=first_record)
        two = _registered(service, experiment_id, label="a", task_id=first_task,
                          record_path=repeat_record)
        # Two attempts of one Task, two fixed records: neither blocks the other.
        assert one["run_id"] == str(first_run) and two["run_id"] == str(repeat_run)
        assert one["record_sha256"] != two["record_sha256"]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ExperimentRecord)) == 2


def test_one_run_cannot_have_two_fixed_records(experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "one run", execution_sha256="1" * 64)
    first = _attempt_record(tmp_path, task_id=task_id, run_id=run_id, artifacts=artifacts,
                            verification_id=verification_id, name="first.json")
    second = _attempt_record(tmp_path, task_id=task_id, run_id=run_id, artifacts=artifacts,
                             verification_id=verification_id,
                             finalization_outcome="completed", name="second.json")
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a",
                                execution_sha256="1" * 64)
        _registered(service, experiment_id, label="a", task_id=task_id, record_path=first)
        with pytest.raises(ExperimentConflictError) as refusal:
            _registered(service, experiment_id, label="a", task_id=task_id,
                        record_path=second)
        assert "already has a fixed record" in str(refusal.value)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ExperimentRecord)) == 1


def test_registering_after_the_attempt_was_recorded_is_refused(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "late registration", execution_sha256="1" * 64)
    _checkout_tool_call(factory, run_id, BASE)
    record_path = _attempt_record(tmp_path, task_id=task_id, run_id=run_id,
                                  artifacts=artifacts, verification_id=verification_id)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a",
                                execution_sha256="1" * 64)
        recorded = _record_classified(service, experiment_id, label="a", task_id=task_id,
                                      run_id=run_id, artifacts_root=artifacts,
                                      context_source="run_record", record_path=record_path)
        assert recorded["record_provenance"] == "caller_supplied"
        with pytest.raises(ExperimentStateError) as refusal:
            _registered(service, experiment_id, label="a", task_id=task_id,
                        record_path=record_path)
        assert "already recorded without this fixed record" in str(refusal.value)
    with factory() as session:
        # Neither the result nor a registration was rewritten by the refusal.
        assert session.scalar(select(func.count()).select_from(ExperimentRecord)) == 0
        row = session.scalar(select(ExperimentTaskResult))
        assert row.record_provenance == "caller_supplied"


def test_concurrent_registration_is_idempotent(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "register race", execution_sha256="1" * 64)
    record_path = _attempt_record(tmp_path, task_id=task_id, run_id=run_id,
                                  artifacts=artifacts, verification_id=verification_id)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        ExperimentService(session).admit_candidate(
            experiment_id=experiment_id, label="a", execution_sha256="1" * 64)

    def register(_index):
        with factory.begin() as session:
            return ExperimentService(session).register_record(
                experiment_id=experiment_id, candidate_label="a", task_id=task_id,
                record_path=record_path, registered_by="operator-a")["record_sha256"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        digests = list(pool.map(register, range(2)))
    assert digests[0] == digests[1]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ExperimentRecord)) == 1


def test_registration_needs_a_record_that_names_a_run_of_the_task(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, _declared_run = _attempt(factory, "declared", status=RunStatus.FAILED)
    other_task, other_run = _attempt(factory, "other task", status=RunStatus.FAILED)
    other_record = _attempt_record(tmp_path, task_id=other_task, run_id=other_run,
                                   artifacts=tmp_path / "unused", outcome="failed",
                                   name="other.json")
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a",
                                execution_sha256="1" * 64)
        with pytest.raises(ExperimentEvidenceError):
            _registered(service, experiment_id, label="a", task_id=task_id,
                        record_path=other_record)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ExperimentRecord)) == 0


def test_registration_is_idempotent_and_a_closed_experiment_takes_none(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "register twice", execution_sha256="1" * 64)
    record_path = _attempt_record(tmp_path, task_id=task_id, run_id=run_id,
                                  artifacts=artifacts, verification_id=verification_id)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a",
                                execution_sha256="1" * 64)
        first = _registered(service, experiment_id, label="a", task_id=task_id,
                            record_path=record_path)
        second = _registered(service, experiment_id, label="a", task_id=task_id,
                             record_path=record_path)
        assert first["record_sha256"] == second["record_sha256"]
        replay = service.register_record(
            experiment_id=experiment_id, candidate_label="a", task_id=task_id,
            record_path=record_path, registered_by="another-operator")
        assert replay["registered_by"] == first["registered_by"]
        service.close(experiment_id=experiment_id, reason="round complete")
    with factory.begin() as session:
        service = ExperimentService(session)
        with pytest.raises(ExperimentStateError):
            _registered(service, experiment_id, label="a", task_id=task_id,
                        record_path=record_path)


def test_a_promotable_pass_without_a_registered_record_cannot_be_promoted(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "unregistered", execution_sha256="1" * 64)
    _checkout_tool_call(factory, run_id, BASE)
    record_path = _attempt_record(tmp_path, task_id=task_id, run_id=run_id,
                                  artifacts=artifacts, verification_id=verification_id)
    experiment_id = _opened(factory, [task_id], limits=_limits(promotion_min_formal_passes=1))
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a",
                                execution_sha256="1" * 64)
        recorded = _record_classified(service, experiment_id, label="a", task_id=task_id,
                                      run_id=run_id, artifacts_root=artifacts,
                                      context_source="run_record", record_path=record_path)
        assert recorded["record_provenance"] == "caller_supplied"
        origin = service.explain_acceptance(experiment_id, "a")["conditions"][-1]
        assert origin["met"] is False
        assert "control plane did not fix" in origin["detail"]
        with pytest.raises(ExperimentLimitError) as refusal:
            service.decide(experiment_id=experiment_id, candidate_label="a", outcome="accepted",
                           reason="unregistered record", decided_by="operator-a")
        assert "control plane did not fix" in str(refusal.value)


def test_the_ledger_records_the_verified_workspace_baseline(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "checkout", execution_sha256="1" * 64)
    _checkout_tool_call(factory, run_id, BASE)
    record_path = _attempt_record(tmp_path, task_id=task_id, run_id=run_id,
                                  artifacts=artifacts, verification_id=verification_id)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a",
                                execution_sha256="1" * 64)
        recorded = _record_classified(service, experiment_id, label="a", task_id=task_id,
                                      run_id=run_id, artifacts_root=artifacts,
                                      context_source="run_record", record_path=record_path)
        assert recorded["workspace_base_commit"] == BASE
        summary = service.summary(experiment_id)
        candidate = summary["candidates"][0]
        assert candidate["workspace_baseline"] == "recorded"
        assert candidate["results_with_workspace_baseline"] == 1
        assert candidate["results"][0]["workspace_base_commit"] == BASE
        assert service.explain_acceptance(experiment_id, "a")["observed"][
            "results_with_workspace_baseline"] == 1


def test_a_missing_checkout_record_leaves_the_baseline_unknown(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "no checkout", execution_sha256="1" * 64)
    record_path = _attempt_record(tmp_path, task_id=task_id, run_id=run_id,
                                  artifacts=artifacts, verification_id=verification_id)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a",
                                execution_sha256="1" * 64)
        recorded = _record_classified(service, experiment_id, label="a", task_id=task_id,
                                      run_id=run_id, artifacts_root=artifacts,
                                      context_source="run_record", record_path=record_path)
        # Recording is allowed; the baseline is simply not claimed.
        assert recorded["workspace_base_commit"] is None
        assert service.summary(experiment_id)["candidates"][0]["workspace_baseline"] == (
            "unverified")


def test_a_checkout_at_another_commit_cannot_be_filed_as_the_pinned_baseline(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "wrong checkout", execution_sha256="1" * 64)
    _checkout_tool_call(factory, run_id, "b" * 40)
    record_path = _attempt_record(tmp_path, task_id=task_id, run_id=run_id,
                                  artifacts=artifacts, verification_id=verification_id)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a",
                                execution_sha256="1" * 64)
        with pytest.raises(ExperimentConflictError) as refusal:
            _record_classified(service, experiment_id, label="a", task_id=task_id,
                               run_id=run_id, artifacts_root=artifacts,
                               context_source="run_record", record_path=record_path)
        assert "different base commit" in str(refusal.value)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ExperimentTaskResult)) == 0


def test_a_run_record_must_agree_with_the_classifier(experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(factory, tmp_path, "disagree")
    failed_record = _attempt_record(tmp_path, task_id=task_id, run_id=run_id,
                                    artifacts=artifacts, outcome="failed",
                                    name="failed.json")
    fired_record = _attempt_record(tmp_path, task_id=task_id, run_id=run_id,
                                   artifacts=artifacts, verification_id=verification_id,
                                   outcome="formal_pass", deadline="fired",
                                   name="fired.json")
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        # The record says failed, the committed evidence loads a formal chain.
        with pytest.raises(ExperimentConflictError) as refusal:
            _record_classified(service, experiment_id, label="a", task_id=task_id,
                               run_id=run_id, artifacts_root=artifacts,
                               context_source="run_record", record_path=failed_record)
        assert "resolve the discrepancy" in str(refusal.value)
        # The record says the deadline fired, but claims a formal pass for it.
        with pytest.raises(ExperimentConflictError) as refusal:
            _record_classified(service, experiment_id, label="a", task_id=task_id,
                               run_id=run_id, artifacts_root=artifacts,
                               context_source="run_record", record_path=fired_record)
        assert "resolve the discrepancy" in str(refusal.value)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ExperimentTaskResult)) == 0


def test_a_run_record_must_describe_this_attempt(experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(factory, tmp_path, "binding")
    other_task, other_run = _attempt(factory, "other", status=RunStatus.FAILED)
    wrong_run = _attempt_record(tmp_path, task_id=task_id, run_id=uuid4(), artifacts=artifacts,
                                verification_id=verification_id, name="wrong-run.json")
    wrong_task = _attempt_record(tmp_path, task_id=other_task, run_id=run_id, artifacts=artifacts,
                                 verification_id=verification_id, name="wrong-task.json")
    unreadable = Path(tmp_path) / "records" / "broken.json"
    unreadable.parent.mkdir(parents=True, exist_ok=True)
    unreadable.write_text("{not json", encoding="utf-8")
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        for path in (wrong_run, wrong_task, unreadable,
                     Path(tmp_path) / "records" / "missing.json"):
            with pytest.raises(ExperimentEvidenceError):
                _record_classified(service, experiment_id, label="a", task_id=task_id,
                                   run_id=run_id, artifacts_root=artifacts,
                                   context_source="run_record", record_path=path)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ExperimentTaskResult)) == 0
        assert session.get(Run, other_run).status == RunStatus.FAILED


def test_a_run_record_cannot_name_a_different_execution_version_than_its_run(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "record version", execution_sha256="1" * 64)
    wrong = _attempt_record(tmp_path, task_id=task_id, run_id=run_id, artifacts=artifacts,
                            verification_id=verification_id,
                            execution_sha256="2" * 64, name="wrong-version.json")
    right = _attempt_record(tmp_path, task_id=task_id, run_id=run_id, artifacts=artifacts,
                            verification_id=verification_id,
                            execution_sha256="1" * 64, name="right-version.json")
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a",
                                execution_sha256="1" * 64)
        with pytest.raises(ExperimentConflictError) as refusal:
            _record_classified(service, experiment_id, label="a", task_id=task_id,
                               run_id=run_id, artifacts_root=artifacts,
                               context_source="run_record", record_path=wrong)
        assert "different execution version" in str(refusal.value)
        recorded = _record_classified(service, experiment_id, label="a", task_id=task_id,
                                      run_id=run_id, artifacts_root=artifacts,
                                      context_source="run_record", record_path=right)
        assert recorded["outcome"] == "formal_pass"
        assert recorded["run_execution_sha256"] == "1" * 64
        assert recorded["tasks_with_formal_pass"] == 1


def test_a_run_record_pins_the_suite_and_the_deadline(experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(factory, tmp_path, "pins")
    other_suite = _attempt_record(tmp_path, task_id=task_id, run_id=run_id, artifacts=artifacts,
                                  verification_id=verification_id,
                                  recorded_suite="some-other-suite", name="other-suite.json")
    quiet = _attempt_record(tmp_path, task_id=task_id, run_id=run_id, artifacts=artifacts,
                            verification_id=verification_id, name="quiet.json")
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        with pytest.raises(ExperimentConflictError) as refusal:
            _record_classified(service, experiment_id, label="a", task_id=task_id,
                               run_id=run_id, artifacts_root=artifacts,
                               context_source="run_record", record_path=other_suite)
        assert "suite version" in str(refusal.value)
        # A caller may not override what the record observed.
        with pytest.raises(ExperimentConflictError):
            _record_classified(service, experiment_id, label="a", task_id=task_id,
                               run_id=run_id, artifacts_root=artifacts,
                               context_source="run_record", record_path=quiet,
                               deadline_context="fired")
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ExperimentTaskResult)) == 0


def test_replaying_with_a_different_run_record_is_refused(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "replay", execution_sha256="1" * 64)
    first = _attempt_record(tmp_path, task_id=task_id, run_id=run_id, artifacts=artifacts,
                            verification_id=verification_id, name="first.json")
    second = _attempt_record(tmp_path, task_id=task_id, run_id=run_id, artifacts=artifacts,
                             verification_id=verification_id,
                             finalization_outcome="completed", name="second.json")
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        recorded = _record_classified(service, experiment_id, label="a", task_id=task_id,
                                      run_id=run_id, artifacts_root=artifacts,
                                      context_source="run_record", record_path=first)
        assert recorded["tasks_with_formal_pass"] == 1
        with pytest.raises(ExperimentConflictError):
            _record_classified(service, experiment_id, label="a", task_id=task_id,
                               run_id=run_id, artifacts_root=artifacts,
                               context_source="run_record", record_path=second)
    with factory() as session:
        row = session.scalar(select(ExperimentTaskResult))
        assert row.record_sha256 == sha256(Path(first).read_bytes()).hexdigest()


# ------------------------------------------------------------------ promotion decisions

def test_acceptance_stays_disabled_after_classified_results_meet_the_thresholds(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    first, first_run, first_artifacts, first_verification = _formal_attempt(
        factory, tmp_path, "one", execution_sha256="1" * 64)
    second, second_run, second_artifacts, second_verification = _formal_attempt(
        factory, tmp_path, "two", execution_sha256="1" * 64)
    _checkout_tool_call(factory, first_run, BASE)
    _checkout_tool_call(factory, second_run, BASE)
    first_record = _attempt_record(tmp_path, task_id=first, run_id=first_run,
                                   artifacts=first_artifacts,
                                   verification_id=first_verification)
    second_record = _attempt_record(tmp_path, task_id=second, run_id=second_run,
                                    artifacts=second_artifacts,
                                    verification_id=second_verification)
    experiment_id = _opened(factory, [first, second],
                            limits=_limits(promotion_min_formal_passes=2,
                                           promotion_max_failures=0, max_candidates=2))
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        _registered(service, experiment_id, label="a", task_id=first,
                    record_path=first_record)
        _registered(service, experiment_id, label="a", task_id=second,
                    record_path=second_record)
        _record_classified(service, experiment_id, label="a", task_id=first,
                           run_id=first_run, artifacts_root=first_artifacts,
                           context_source="run_record", record_path=first_record)
        with pytest.raises(ExperimentLimitError) as refusal:
            service.decide(experiment_id=experiment_id, candidate_label="a", outcome="accepted",
                           reason="one task passed", decided_by="operator-a")
        assert "no result" in str(refusal.value)
    with factory.begin() as session:
        service = ExperimentService(session)
        recorded = _record_classified(service, experiment_id, label="a", task_id=second,
                                      run_id=second_run, artifacts_root=second_artifacts,
                                      context_source="run_record", record_path=second_record)
        assert recorded["tasks_with_formal_pass"] == 2
        assert recorded["context_evidence"] == "record"
        with pytest.raises(ExperimentStateError, match="Acceptance is unavailable"):
            service.decide(experiment_id=experiment_id, candidate_label="a", outcome="accepted",
                           reason="both classified formal passes", decided_by="operator-a")
        assert service.read(experiment_id).closed_at is None
        with pytest.raises(ExperimentError):
            service.decide(experiment_id=experiment_id, candidate_label="a", outcome="best",
                           reason="unknown vocabulary", decided_by="operator-a")
    # The refused acceptance left the candidate usable: nothing was concluded by it.
    with factory() as session:
        candidate = session.scalar(select(ExperimentCandidate))
        assert candidate.state == "prepared"
        assert candidate.outcome is None


def test_repeated_passes_on_one_task_do_not_cover_the_declared_task_set(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    first, first_run, first_artifacts, first_verification = _formal_attempt(
        factory, tmp_path, "only task", execution_sha256="1" * 64)
    repeat_run = _second_run(factory, first, execution_sha256="1" * 64)
    repeat_artifacts = Path(tmp_path) / "artifacts" / str(repeat_run)
    repeat_artifacts.mkdir(parents=True)
    repeat_verification, _ = _finish_formally(factory, repeat_artifacts, run_id=repeat_run)
    first_record = _attempt_record(tmp_path, task_id=first, run_id=first_run,
                                   artifacts=first_artifacts,
                                   verification_id=first_verification)
    repeat_record = _attempt_record(tmp_path, task_id=first, run_id=repeat_run,
                                    artifacts=repeat_artifacts,
                                    verification_id=repeat_verification)
    second, second_run = _attempt(factory, "other task", status=RunStatus.FAILED)
    experiment_id = _opened(factory, [first, second],
                            limits=_limits(promotion_min_formal_passes=2,
                                           promotion_max_failures=0))
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        one = _record_classified(service, experiment_id, label="a", task_id=first,
                                 run_id=first_run, artifacts_root=first_artifacts,
                                 context_source="run_record", record_path=first_record)
        two = _record_classified(service, experiment_id, label="a", task_id=first,
                                 run_id=repeat_run, artifacts_root=repeat_artifacts,
                                 context_source="run_record", record_path=repeat_record)
        assert one["tasks_with_formal_pass"] == 1
        # Both attempts are kept and both are formal passes — but they cover one Task.
        assert two["formal_passes"] == 2
        assert two["tasks_with_formal_pass"] == 1
        service.record_task_result(
            experiment_id=experiment_id, candidate_label="a", task_id=second,
            run_id=second_run, outcome="failed", recorded_by="operator-a")
        with pytest.raises(ExperimentLimitError) as refusal:
            service.decide(experiment_id=experiment_id, candidate_label="a", outcome="accepted",
                           reason="two passes", decided_by="operator-a")
        assert "distinct Tasks with a classified formal pass 1 < 2" in str(refusal.value)


def test_declared_results_never_satisfy_the_promotion_thresholds(
        experiment_session_factory):
    factory = experiment_session_factory
    first, first_run = _attempt(factory, "one")
    _trusted_verification(factory, first_run)
    second, second_run = _attempt(factory, "two")
    _trusted_verification(factory, second_run)
    experiment_id = _opened(factory, [first, second],
                            limits=_limits(promotion_min_formal_passes=2))
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        for task_id, run_id in ((first, first_run), (second, second_run)):
            recorded = service.record_task_result(
                experiment_id=experiment_id, candidate_label="a", task_id=task_id,
                run_id=run_id, outcome="formal_pass", recorded_by="operator-a")
            assert recorded["evidence_source"] == "declared"
            assert recorded["tasks_with_formal_pass"] == 0
        with pytest.raises(ExperimentLimitError) as refusal:
            service.decide(experiment_id=experiment_id, candidate_label="a", outcome="accepted",
                           reason="two declared passes", decided_by="operator-a")
        assert "distinct Tasks with a classified formal pass 0 < 2" in str(refusal.value)


def test_promotion_refuses_more_failures_than_the_declared_maximum(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    passed_task, passed_run, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "passes", execution_sha256="1" * 64)
    passed_record = _attempt_record(tmp_path, task_id=passed_task, run_id=passed_run,
                                    artifacts=artifacts, verification_id=verification_id)
    failed_task, failed_run = _attempt(factory, "fails", status=RunStatus.FAILED)
    experiment_id = _opened(factory, [passed_task, failed_task],
                            limits=_limits(promotion_max_failures=0))
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        _record_classified(service, experiment_id, label="a", task_id=passed_task,
                           run_id=passed_run, artifacts_root=artifacts,
                           context_source="run_record", record_path=passed_record)
        service.record_task_result(
            experiment_id=experiment_id, candidate_label="a", task_id=failed_task,
            run_id=failed_run, outcome="failed", recorded_by="operator-a")
        with pytest.raises(ExperimentLimitError) as refusal:
            service.decide(experiment_id=experiment_id, candidate_label="a", outcome="accepted",
                           reason="mostly fine", decided_by="operator-a")
        assert "distinct Tasks with a failure 1 > 0" in str(refusal.value)


def test_rejection_needs_no_threshold_but_needs_a_reason(experiment_session_factory):
    factory = experiment_session_factory
    experiment_id = _opened(factory, [_task(factory)])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        with pytest.raises(ExperimentError):
            service.decide(experiment_id=experiment_id, candidate_label="a", outcome="rejected",
                           reason="   ", decided_by="operator-a")
        decided = service.decide(experiment_id=experiment_id, candidate_label="a",
                                 outcome="abandoned", reason="budget reallocated",
                                 decided_by="operator-a")
        assert decided["outcome"] == "abandoned"
        assert decided["formal_passes"] == 0
    with factory() as session:
        row = session.scalar(select(ExperimentCandidate))
        assert row.concluded_at is not None
        assert row.state == "concluded"


def test_a_concluded_candidate_takes_no_further_accounting(experiment_session_factory):
    factory = experiment_session_factory
    task_id, run_id = _attempt(factory, status=RunStatus.FAILED)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        service.record_task_result(
            experiment_id=experiment_id, candidate_label="a", task_id=task_id, run_id=run_id,
            outcome="failed", recorded_by="operator-a")
        service.decide(experiment_id=experiment_id, candidate_label="a", outcome="rejected",
                       reason="fails the declared task", decided_by="operator-a")
    later_task, later_run = _attempt(factory, "later", status=RunStatus.FAILED)
    with factory.begin() as session:
        service = ExperimentService(session)
        with pytest.raises(ExperimentStateError):
            service.reserve(experiment_id=experiment_id, request_key=uuid4(), tokens=10,
                            candidate_label="a")
        with pytest.raises(ExperimentStateError):
            service.record_task_result(
                experiment_id=experiment_id, candidate_label="a", task_id=task_id,
                run_id=later_run, outcome="failed", recorded_by="operator-a")
        assert later_task != task_id
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ExperimentTaskResult)) == 1


# ------------------------------------------------------------------ closing and the deadline


def test_close_stops_new_accounting_but_still_allows_settlement(experiment_session_factory):
    factory = experiment_session_factory
    task_id, run_id = _attempt(factory, status=RunStatus.FAILED)
    experiment_id = _opened(factory, [task_id])
    key = uuid4()
    with factory.begin() as session:
        service = ExperimentService(session)
        service.reserve(experiment_id=experiment_id, request_key=key, tokens=40)
        closed = service.close(experiment_id=experiment_id, reason="round complete")
        assert closed["closed_reason"] == "round complete"
        assert closed["closed_at"] is not None
        assert closed["usage"]["outstanding_reservations"] == 1
        assert service.close(experiment_id=experiment_id,
                             reason="round complete")["closed_at"] == closed["closed_at"]
        with pytest.raises(ExperimentConflictError):
            service.close(experiment_id=experiment_id, reason="a different story")
        with pytest.raises(ExperimentStateError):
            service.reserve(experiment_id=experiment_id, request_key=uuid4(), tokens=10)
        with pytest.raises(ExperimentStateError):
            service.admit_candidate(experiment_id=experiment_id, label="late",
                                    execution_sha256="9" * 64)
        with pytest.raises(ExperimentStateError):
            service.record_task_result(
                experiment_id=experiment_id, candidate_label="late", task_id=task_id,
                run_id=run_id, outcome="failed", recorded_by="operator-a")
    with factory.begin() as session:
        service = ExperimentService(session)
        settled = service.settle(experiment_id=experiment_id, request_key=key, actual_tokens=40)
        assert settled["settled_tokens"] == 40
        assert settled["over_budget"] is False
        assert service.summary(experiment_id)["usage"]["outstanding_reservations"] == 0
    with factory.begin() as session:
        with pytest.raises(ExperimentStateError):
            ExperimentService(session).close(experiment_id=uuid4(), reason="unknown")


def test_the_deadline_blocks_new_spend_but_not_recording_a_finished_attempt(
        experiment_session_factory):
    factory = experiment_session_factory
    task_id, run_id = _attempt(factory, status=RunStatus.FAILED)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        # Move the frozen deadline into the past without touching the recorded start time:
        # the CHECK constraint requires deadline_at > created_at, so it stays one microsecond
        # later and the database clock has long passed it.
        experiment = session.get(Experiment, experiment_id)
        experiment.deadline_at = experiment.created_at + timedelta(microseconds=1)
    with factory.begin() as session:
        service = ExperimentService(session)
        with pytest.raises(ExperimentLimitError):
            service.reserve(experiment_id=experiment_id, request_key=uuid4(), tokens=10)
        with pytest.raises(ExperimentLimitError):
            service.admit_candidate(experiment_id=experiment_id, label="b",
                                    execution_sha256="2" * 64)
        # A run that finished before the deadline can still be recorded afterwards: the
        # deadline stops new spend, it does not erase an attempt that already happened.
        recorded = service.record_task_result(
            experiment_id=experiment_id, candidate_label="a", task_id=task_id, run_id=run_id,
            outcome="failed", recorded_by="operator-a")
        assert recorded["non_formal_results"] == 1


# ------------------------------------------------------------------ summary and migration


def test_summary_reports_declarations_and_accounting_only(experiment_session_factory):
    factory = experiment_session_factory
    task_id = _task(factory)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a", execution_sha256="1" * 64)
        summary = service.summary(experiment_id)
    assert summary["experiment_id"] == str(experiment_id)
    assert summary["base_execution_sha256"] == "b" * 64
    assert summary["tasks"] == [str(task_id)]
    assert summary["limits"]["max_candidates"] == 3
    assert summary["usage"] == {
        "reserved_tokens": 0, "settled_tokens": 0, "over_budget": False,
        "remaining_tokens": 1000, "outstanding_reservations": 0,
        "dispatched_unsettled": 0,
        "unknown_usage_reservations": 0, "accounting_complete": True,
        "accounting_gaps": []}
    assert [item["label"] for item in summary["candidates"]] == ["a"]
    assert summary["candidates"][0]["results"] == []
    # The candidate digest is labelled as what it is: a caller declaration, not a verified
    # binding to the bytes a Run executed.
    assert summary["candidate_execution_binding"] == "per_candidate"
    assert summary["candidates"][0]["execution_binding"] == "no_verified_run"
    assert summary["candidates"][0]["tasks_with_classified_formal_pass"] == 0
    # The summary ranks nothing and claims no quality: labels, verdicts, reasons only.
    rendered = repr(summary).lower()
    for claim in ("better", "best", "improved", "superior"):
        assert claim not in rendered


def test_model_and_migration_agree_on_the_experiment_tables(experiment_session_factory):
    """The ORM model must not drift from the migration that creates these tables."""
    from sqlalchemy import inspect as inspect_schema

    from devflow_ai.models import Base

    engine = experiment_session_factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    tables = ("experiments", "experiment_tasks", "experiment_candidates",
              "experiment_records", "experiment_reservations", "experiment_task_results",
              "runs")
    with engine.connect() as connection:
        inspector = inspect_schema(connection)
        for table in tables:
            migrated = {column["name"]: bool(column["nullable"])
                        for column in inspector.get_columns(table, schema=schema)}
            modelled = {column.name: bool(column.nullable)
                        for column in Base.metadata.tables[table].columns}
            assert migrated == modelled, table


def test_migration_downgrade_removes_exactly_what_upgrade_added():
    if os.environ.get("DEVFLOW_DB_NAME") != "devflow_test":
        pytest.skip("Requires explicitly configured devflow_test")
    engine = create_db_engine()
    schema = f"experiment_migration_test_{uuid4().hex}"
    created = False
    tables = ("experiments", "experiment_tasks", "experiment_candidates",
              "experiment_reservations", "experiment_task_results")
    revision = SCRIPTS.get_revision(MIGRATION)
    try:
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT current_database()")) == "devflow_test"
            connection.execute(CreateSchema(schema))
        created = True
        scoped = engine.execution_options(schema_translate_map={None: schema})
        _apply(scoped, (MIGRATION,))
        with scoped.begin() as connection:
            connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            for table in tables:
                assert connection.scalar(text("SELECT to_regclass(:name)"),
                                         {"name": table}) is not None
        # The compiled DDL — not only the service's checks — enforces the two attribution and
        # completeness rules. A valid row is written first so the refusals below cannot pass
        # for an unrelated reason.
        with scoped.begin() as connection:
            connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            experiment_row = connection.execute(text(
                "INSERT INTO experiments (identity_key, base_execution_sha256, max_candidates, "
                "max_total_model_tokens, max_total_seconds, promotion_min_formal_passes, "
                "promotion_max_failures, created_by, deadline_at) "
                "VALUES ('migration-probe', repeat('a', 64), 2, 1000, 60, 1, 0, 'operator', "
                "now() + interval '1 hour') RETURNING id")).scalar_one()
            candidates = connection.execute(text(
                "INSERT INTO experiment_candidates (experiment_id, label, execution_sha256) "
                "VALUES (:experiment, 'a', repeat('b', 64)), "
                "(:experiment, 'b', repeat('c', 64)) RETURNING id, label"),
                {"experiment": experiment_row}).all()
            by_label = {label: candidate for candidate, label in candidates}
            run_id = uuid4()
            connection.execute(text(
                "INSERT INTO experiment_task_results (experiment_id, candidate_id, task_id, "
                "run_id, outcome, recorded_by) VALUES (:experiment, :candidate, :task, :run, "
                "'failed', 'operator')"),
                {"experiment": experiment_row, "candidate": by_label["a"], "task": uuid4(),
                 "run": run_id})
            connection.execute(text(
                "INSERT INTO experiment_reservations (experiment_id, request_key, tokens) "
                "VALUES (:experiment, :key, 10)"), {"experiment": experiment_row, "key": uuid4()})
        with pytest.raises(IntegrityError):
            with scoped.begin() as connection:
                connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
                connection.execute(text(
                    "INSERT INTO experiment_task_results (experiment_id, candidate_id, task_id, "
                    "run_id, outcome, recorded_by) VALUES (:experiment, :candidate, :task, :run, "
                    "'failed', 'operator')"),
                    {"experiment": experiment_row, "candidate": by_label["b"],
                     "task": uuid4(), "run": run_id})
        with pytest.raises(IntegrityError):
            with scoped.begin() as connection:
                connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
                connection.execute(text(
                    "INSERT INTO experiment_reservations (experiment_id, request_key, tokens, "
                    "state) VALUES (:experiment, :key, 10, 'released')"),
                    {"experiment": experiment_row, "key": uuid4()})
        with pytest.raises(IntegrityError):
            with scoped.begin() as connection:
                connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
                connection.execute(text(
                    "INSERT INTO experiment_task_results (experiment_id, candidate_id, task_id, "
                    "run_id, outcome, recorded_by, evidence_source) VALUES (:experiment, "
                    ":candidate, :task, :run, 'formal_pass', 'operator', 'classified')"),
                    {"experiment": experiment_row, "candidate": by_label["b"],
                     "task": uuid4(), "run": uuid4()})
        # A classified row must state the context it was classified under: an omitted deadline
        # is refused rather than being read as "no timeout".
        with pytest.raises(IntegrityError):
            with scoped.begin() as connection:
                connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
                connection.execute(text(
                    "INSERT INTO experiment_task_results (experiment_id, candidate_id, task_id, "
                    "run_id, outcome, recorded_by, evidence_source) VALUES (:experiment, "
                    ":candidate, :task, :run, 'incomplete', 'operator', 'classified')"),
                    {"experiment": experiment_row, "candidate": by_label["b"],
                     "task": uuid4(), "run": uuid4()})
        with pytest.raises(IntegrityError):
            with scoped.begin() as connection:
                connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
                connection.execute(text(
                    "INSERT INTO experiment_task_results (experiment_id, candidate_id, task_id, "
                    "run_id, outcome, recorded_by, evidence_source, deadline_context, "
                    "context_source) VALUES (:experiment, :candidate, :task, :run, 'incomplete', "
                    "'operator', 'classified', 'maybe', 'probe')"),
                    {"experiment": experiment_row, "candidate": by_label["b"],
                     "task": uuid4(), "run": uuid4()})
        # One fixed record per attempt, and one attempt per digest — enforced by the schema.
        registered_run, other_run = uuid4(), uuid4()
        with scoped.begin() as connection:
            connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            connection.execute(text(
                "INSERT INTO experiment_records (experiment_id, candidate_id, task_id, run_id, "
                "record_sha256, registered_by) VALUES (:experiment, :candidate, :task, :run, "
                ":digest, 'operator')"),
                {"experiment": experiment_row, "candidate": by_label["a"], "task": uuid4(),
                 "run": registered_run, "digest": "0" * 64})
        for run_id, digest in (
                (uuid4(), "0" * 64),          # the same bytes for another attempt
                (registered_run, "1" * 64),   # a second record for the same attempt
                (uuid4(), "not-a-digest")):   # a malformed digest
            with pytest.raises(IntegrityError):
                with scoped.begin() as connection:
                    connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
                    connection.execute(text(
                        "INSERT INTO experiment_records (experiment_id, candidate_id, task_id, "
                        "run_id, record_sha256, registered_by) VALUES (:experiment, :candidate, "
                        ":task, :run, :digest, 'operator')"),
                        {"experiment": experiment_row, "candidate": by_label["a"],
                         "task": uuid4(), "run": run_id, "digest": digest})
        # A declared Task always carries its baseline, and the format is checked.
        with pytest.raises(IntegrityError):
            with scoped.begin() as connection:
                connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
                connection.execute(text(
                    "INSERT INTO experiment_tasks (experiment_id, task_id, base_commit) "
                    "VALUES (:experiment, :task, 'not-a-commit')"),
                    {"experiment": experiment_row, "task": uuid4()})
        # The frozen target baseline follows the commit id format.
        with pytest.raises(IntegrityError):
            with scoped.begin() as connection:
                connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
                connection.execute(text(
                    "INSERT INTO experiment_task_results (experiment_id, candidate_id, task_id, "
                    "run_id, outcome, recorded_by, workspace_base_commit) VALUES (:experiment, "
                    ":candidate, :task, :run, 'failed', 'operator', 'not-a-commit')"),
                    {"experiment": experiment_row, "candidate": by_label["b"],
                     "task": uuid4(), "run": uuid4()})
        # The frozen execution digest follows the same format rule.
        with pytest.raises(IntegrityError):
            with scoped.begin() as connection:
                connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
                connection.execute(text(
                    "INSERT INTO experiment_task_results (experiment_id, candidate_id, task_id, "
                    "run_id, outcome, recorded_by, run_execution_sha256) VALUES (:experiment, "
                    ":candidate, :task, :run, 'failed', 'operator', 'not-a-digest')"),
                    {"experiment": experiment_row, "candidate": by_label["b"],
                     "task": uuid4(), "run": uuid4()})
        # A record-labelled row must state the kind of attempt the record describes.
        with pytest.raises(IntegrityError):
            with scoped.begin() as connection:
                connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
                connection.execute(text(
                    "INSERT INTO experiment_task_results (experiment_id, candidate_id, task_id, "
                    "run_id, outcome, recorded_by, evidence_source, deadline_context, "
                    "context_source, context_evidence, record_sha256) VALUES (:experiment, "
                    ":candidate, :task, :run, 'incomplete', 'operator', 'classified', "
                    "'not_fired', 'probe', 'record', repeat('a', 64))"),
                    {"experiment": experiment_row, "candidate": by_label["b"],
                     "task": uuid4(), "run": uuid4()})
        # "record" evidence must name the exact bytes it read.
        for record_sha256 in (None, "not-a-digest"):
            with pytest.raises(IntegrityError):
                with scoped.begin() as connection:
                    connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
                    connection.execute(text(
                        "INSERT INTO experiment_task_results (experiment_id, candidate_id, "
                        "task_id, run_id, outcome, recorded_by, evidence_source, "
                        "deadline_context, context_source, context_evidence, record_sha256) "
                        "VALUES (:experiment, :candidate, :task, :run, 'incomplete', 'operator', "
                        "'classified', 'not_fired', 'probe', 'record', :digest)"),
                        {"experiment": experiment_row, "candidate": by_label["b"],
                         "task": uuid4(), "run": uuid4(), "digest": record_sha256})
        # A declared formal pass is allowed — it is a declaration, and the schema records that
        # it carries no Verification id — while a "classified" one without a Verification is
        # refused.
        with scoped.begin() as connection:
            connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            connection.execute(text(
                "INSERT INTO experiment_task_results (experiment_id, candidate_id, task_id, "
                "run_id, outcome, recorded_by, evidence_source) VALUES (:experiment, "
                ":candidate, :task, :run, 'formal_pass', 'operator', 'declared')"),
                {"experiment": experiment_row, "candidate": by_label["b"],
                 "task": uuid4(), "run": uuid4()})
            assert connection.scalar(text(
                "SELECT count(*) FROM experiment_task_results WHERE experiment_id = :experiment "
                "AND evidence_source = 'declared'"), {"experiment": experiment_row}) == 2
        with scoped.begin() as connection:
            connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            with Operations.context(MigrationContext.configure(connection)):
                revision.module.downgrade()
        with scoped.begin() as connection:
            connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            for table in tables:
                assert connection.scalar(text("SELECT to_regclass(:name)"),
                                         {"name": table}) is None
    finally:
        if created:
            with engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        engine.dispose()


# ------------------------------------------------------------------ trusted ledger CLI


def _ledger_args(experiment_id, task_id, run_id, artifacts, *, candidate="a",
                 suite_version=SUITE_VERSION, record_path=None, deadline_context=None,
                 extra=()):
    args = ["record-result", "--experiment", str(experiment_id), "--candidate", candidate,
            "--task-id", str(task_id), "--run-id", str(run_id),
            "--artifacts-root", str(artifacts), "--suite-version", suite_version,
            "--operator", "operator-a"]
    if record_path is not None:
        args += ["--record", str(record_path)]
    if deadline_context is not None:
        args += ["--deadline-context", deadline_context]
    return args + list(extra)


def test_the_ledger_cli_records_a_classified_result(
        experiment_session_factory, tmp_path, capsys):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "cli", execution_sha256="1" * 64)
    record_path = _attempt_record(tmp_path, task_id=task_id, run_id=run_id,
                                  artifacts=artifacts, verification_id=verification_id)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        ExperimentService(session).admit_candidate(
            experiment_id=experiment_id, label="a", execution_sha256="1" * 64)

    exit_code = ledger_main(
        _ledger_args(experiment_id, task_id, run_id, artifacts, record_path=record_path),
        session_factory=factory)

    printed = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert printed["outcome"] == "formal_pass"
    assert printed["evidence_source"] == "classified"
    assert printed["tasks_with_formal_pass"] == 1
    with factory() as session:
        rows = session.scalars(select(ExperimentTaskResult)).all()
        assert len(rows) == 1 and rows[0].run_execution_sha256 == "1" * 64


def test_the_ledger_cli_refuses_a_conflict_and_writes_nothing(
        experiment_session_factory, tmp_path, capsys):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "cli conflict", execution_sha256="1" * 64)
    record_path = _attempt_record(tmp_path, task_id=task_id, run_id=run_id,
                                  artifacts=artifacts, verification_id=verification_id)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        ExperimentService(session).admit_candidate(
            experiment_id=experiment_id, label="a", execution_sha256="2" * 64)

    exit_code = ledger_main(
        _ledger_args(experiment_id, task_id, run_id, artifacts, record_path=record_path),
        session_factory=factory)

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out.strip() == ""
    assert "different execution version" in captured.err
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ExperimentTaskResult)) == 0


def test_the_ledger_cli_never_infers_a_missing_context(
        experiment_session_factory, tmp_path, capsys):
    factory = experiment_session_factory
    task_id, run_id, artifacts, _ = _formal_attempt(factory, tmp_path, "cli context")
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        ExperimentService(session).admit_candidate(
            experiment_id=experiment_id, label="a", execution_sha256="1" * 64)

    exit_code = ledger_main(
        _ledger_args(experiment_id, task_id, run_id, artifacts), session_factory=factory)

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Supply the run record" in captured.err
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ExperimentTaskResult)) == 0


def test_the_ledger_cli_summary_is_read_only(experiment_session_factory, capsys):
    factory = experiment_session_factory
    experiment_id = _opened(factory, [_task(factory)])
    with factory.begin() as session:
        ExperimentService(session).admit_candidate(
            experiment_id=experiment_id, label="a", execution_sha256="1" * 64)

    exit_code = ledger_main(["summary", "--experiment", str(experiment_id)],
                            session_factory=factory)

    printed = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert [item["label"] for item in printed["candidates"]] == ["a"]
    assert printed["candidates"][0]["execution_binding"] == "no_verified_run"
    assert printed["candidate_execution_binding"] == "per_candidate"
    assert ledger_main(["summary", "--experiment", str(uuid4())],
                       session_factory=factory) == 2


# ------------------------------------------------ acceptance diagnosis (read-only)


def test_explaining_acceptance_lists_every_missing_condition(
        experiment_session_factory):
    factory = experiment_session_factory
    experiment_id = _opened(factory, [_task(factory)],
                            limits=_limits(promotion_min_formal_passes=1))
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a",
                                execution_sha256="1" * 64)
        explained = service.explain_acceptance(experiment_id, "a")
        # Read-only and honest: the checks are reported, and acceptance is still refused.
        assert explained["acceptable"] is False
        assert explained["blocked_by"]
        assert [item["condition"] for item in explained["conditions"]] == [
            "budget_within_limit", "accounting_complete", "every_declared_task_recorded",
            "distinct_formal_passes_meet_minimum", "failures_within_maximum",
            "promotable_passes_have_verified_baseline",
            "promotable_passes_have_registered_records"]
        assert any("no result" in problem for problem in explained["problems"])
        assert any("0 < 1" in problem for problem in explained["problems"])
        with pytest.raises(ExperimentLimitError) as refusal:
            service.decide(experiment_id=experiment_id, candidate_label="a", outcome="accepted",
                           reason="not yet", decided_by="operator-a")
        # The decision path and the explanation cannot drift apart.
        for problem in explained["problems"]:
            assert problem in str(refusal.value)


def test_explaining_acceptance_stays_false_even_when_every_check_passes(
        experiment_session_factory, tmp_path):
    factory = experiment_session_factory
    first, first_run, first_artifacts, first_verification = _formal_attempt(
        factory, tmp_path, "explain one", execution_sha256="1" * 64)
    second, second_run, second_artifacts, second_verification = _formal_attempt(
        factory, tmp_path, "explain two", execution_sha256="1" * 64)
    _checkout_tool_call(factory, first_run, BASE)
    _checkout_tool_call(factory, second_run, BASE)
    experiment_id = _opened(factory, [first, second],
                            limits=_limits(promotion_min_formal_passes=2))
    with factory.begin() as session:
        service = ExperimentService(session)
        service.admit_candidate(experiment_id=experiment_id, label="a",
                                execution_sha256="1" * 64)
        for task_id, run_id, artifacts, verification in (
                (first, first_run, first_artifacts, first_verification),
                (second, second_run, second_artifacts, second_verification)):
            record_path = _attempt_record(tmp_path, task_id=task_id, run_id=run_id,
                                          artifacts=artifacts,
                                          verification_id=verification)
            _registered(service, experiment_id, label="a", task_id=task_id,
                        record_path=record_path)
            _record_classified(service, experiment_id, label="a", task_id=task_id,
                               run_id=run_id, artifacts_root=artifacts,
                               context_source="run_record", record_path=record_path)
        explained = service.explain_acceptance(experiment_id, "a")
        assert explained["problems"] == []
        assert [item["met"] for item in explained["conditions"]] == [True] * 7
        assert explained["conditions"][-1]["condition"] == (
            "promotable_passes_have_registered_records")
        assert explained["acceptable"] is False
        assert any("caller_supplied" in item for item in explained["blocked_by"])
        assert any("attested" in item for item in explained["blocked_by"])


def test_the_ledger_cli_registers_a_record_without_copying_it(
        experiment_session_factory, tmp_path, capsys):
    factory = experiment_session_factory
    task_id, run_id, artifacts, verification_id = _formal_attempt(
        factory, tmp_path, "cli register", execution_sha256="1" * 64)
    record_path = _attempt_record(tmp_path, task_id=task_id, run_id=run_id,
                                  artifacts=artifacts, verification_id=verification_id)
    experiment_id = _opened(factory, [task_id])
    with factory.begin() as session:
        ExperimentService(session).admit_candidate(
            experiment_id=experiment_id, label="a", execution_sha256="1" * 64)

    exit_code = ledger_main(
        ["register-record", "--experiment", str(experiment_id), "--candidate", "a",
         "--task-id", str(task_id), "--record", str(record_path),
         "--operator", "operator-a"], session_factory=factory)

    printed = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert printed["record_sha256"] == sha256(Path(record_path).read_bytes()).hexdigest()
    assert printed["run_id"] == str(run_id)
    assert ledger_main(
        ["register-record", "--experiment", str(experiment_id), "--candidate", "missing",
         "--task-id", str(task_id), "--record", str(record_path),
         "--operator", "operator-a"], session_factory=factory) == 2


def test_the_ledger_cli_explains_acceptance_without_concluding_anything(
        experiment_session_factory, capsys):
    factory = experiment_session_factory
    experiment_id = _opened(factory, [_task(factory)])
    with factory.begin() as session:
        ExperimentService(session).admit_candidate(
            experiment_id=experiment_id, label="a", execution_sha256="1" * 64)

    exit_code = ledger_main(["explain-acceptance", "--experiment", str(experiment_id),
                             "--candidate", "a"], session_factory=factory)

    printed = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert printed["acceptable"] is False
    assert printed["candidate"] == "a"
    assert printed["problems"]
    assert ledger_main(["explain-acceptance", "--experiment", str(experiment_id),
                        "--candidate", "missing"], session_factory=factory) == 2
    with factory() as session:
        row = session.scalar(select(ExperimentCandidate))
        # Diagnosis does not conclude the candidate.
        assert row.state == "prepared" and row.outcome is None
