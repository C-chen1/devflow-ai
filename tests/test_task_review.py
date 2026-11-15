from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import select, text

from devflow_ai.enums import RunStatus, TaskStatus, VerificationResult
from devflow_ai.models import Run, Task, TaskReview, Verification
from devflow_ai.services import (
    AdmissionRecord, TaskReviewConflictError, TaskReviewEvidenceError, TaskReviewError,
    TaskReviewService, TaskService, TaskStateConflictError,
)
from devflow_ai.worker import Worker

REVISIONS = ("c7e1a29b4d63", "d4f63c192ba8", "e8c27b14a693", "62eae84cebd1",
             "97f3a25cb190", "a43cb50f67e9", "f154b6e82639", "f83ab2d16c49",
             "ab6e1c30f42d", "4b79c6e21a50", "b7f2c31d8a90")


@pytest.fixture
def review_session_factory(run_session_factory):
    """Add the verification and task-review tables inside this test's isolated schema only."""
    engine = run_session_factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations"))
    with engine.begin() as connection:
        connection.execution_options(schema_translate_map=None)
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        with Operations.context(MigrationContext.configure(connection)):
            for revision_id in REVISIONS:
                scripts.get_revision(revision_id).module.upgrade()
    return run_session_factory


def _passed_run(factory, *, task_status=TaskStatus.AWAITING_REVIEW, run_status=RunStatus.PASSED,
                result=VerificationResult.PASSED, scope="trusted_v1", suite_version="suite-1"):
    """One Task whose Run passed and whose trusted Verification is recorded."""
    with factory() as session:
        task, _ = TaskService(session).create_task(
            title="Reviewed task", description="Review it", repo="example-repo",
            idempotency_key=uuid4())
        task_id = task.id
    with factory() as session:
        TaskService(session).apply_admission_decision(
            task_id, TaskStatus.ELIGIBLE, base_commit="a" * 40,
            admission_record=AdmissionRecord(reason="fixture", evidence=["fixture"]))
    worker = Worker(factory, "review-worker")
    run_id = worker.claim_once().run_id
    with factory.begin() as session:
        session.get(Run, run_id).status = run_status
        evidence = {"scope": scope, "draft_artifact_id": str(uuid4())}
        if suite_version is not None:
            evidence["suite_version"] = suite_version
        verification = Verification(run_id=run_id, result=result, evidence=evidence)
        session.add(verification)
        session.flush()
        verification_id = verification.id
        session.get(Task, task_id).status = task_status
    return {"task_id": task_id, "run_id": run_id, "verification_id": verification_id}


def _submit(factory, fixture, **overrides):
    arguments = {
        "task_id": fixture["task_id"], "run_id": fixture["run_id"],
        "verification_id": fixture["verification_id"], "record_sha256": "a" * 64,
        "conclusion": "accepted", "reviewer": "reviewer-b",
        "reviewed_at": "2026-11-13T10:00:00+00:00",
        "independent_suite_version": "suite-1",
    }
    arguments.update(overrides)
    with factory() as session:
        return TaskReviewService(session).record(**arguments)


def _reviews(factory, task_id):
    with factory() as session:
        return session.scalars(select(TaskReview).where(TaskReview.task_id == task_id)).all()


def test_an_accepted_review_completes_the_task_in_one_transaction(review_session_factory):
    factory = review_session_factory
    fixture = _passed_run(factory)

    result = _submit(factory, fixture)

    assert result["conclusion"] == "accepted" and result["idempotent"] is False
    assert result["task_status"] == "COMPLETED"
    review, = _reviews(factory, fixture["task_id"])
    assert str(review.id) == result["review_id"]
    assert (review.run_id, review.verification_id) == (fixture["run_id"], fixture["verification_id"])
    assert (review.record_sha256, review.reviewer) == ("a" * 64, "reviewer-b")
    # Both times are kept and come from different sources.
    assert review.reviewed_at == "2026-11-13T10:00:00+00:00"
    assert review.created_at is not None
    with factory() as session:
        assert session.get(Task, fixture["task_id"]).status == TaskStatus.COMPLETED


def test_a_rejected_review_uses_the_existing_rejected_state(review_session_factory):
    factory = review_session_factory
    fixture = _passed_run(factory)

    result = _submit(factory, fixture, conclusion="rejected")

    assert result["task_status"] == "REJECTED"
    with factory() as session:
        assert session.get(Task, fixture["task_id"]).status == TaskStatus.REJECTED


def test_the_same_submission_is_idempotent(review_session_factory):
    factory = review_session_factory
    fixture = _passed_run(factory)

    first = _submit(factory, fixture)
    second = _submit(factory, fixture)

    assert second["idempotent"] is True and second["review_id"] == first["review_id"]
    assert len(_reviews(factory, fixture["task_id"])) == 1


def test_a_different_conclusion_or_record_for_the_same_task_conflicts(review_session_factory):
    factory = review_session_factory
    fixture = _passed_run(factory)
    _submit(factory, fixture)

    with pytest.raises(TaskReviewConflictError):
        _submit(factory, fixture, conclusion="rejected")
    with pytest.raises(TaskReviewConflictError):
        _submit(factory, fixture, record_sha256="b" * 64)

    assert len(_reviews(factory, fixture["task_id"])) == 1
    with factory() as session:
        assert session.get(Task, fixture["task_id"]).status == TaskStatus.COMPLETED


@pytest.mark.parametrize(("overrides", "error"), [
    ({"run_status": RunStatus.FAILED}, TaskReviewEvidenceError),
    ({"result": VerificationResult.FAILED}, TaskReviewEvidenceError),
    ({"scope": "no_verification"}, TaskReviewEvidenceError),
    ({"task_status": TaskStatus.RUNNING}, TaskStateConflictError),
])
def test_evidence_that_does_not_support_the_review_is_refused(
    review_session_factory, overrides, error,
):
    factory = review_session_factory
    fixture = _passed_run(factory, **overrides)

    with pytest.raises(error):
        _submit(factory, fixture)

    assert _reviews(factory, fixture["task_id"]) == []
    with factory() as session:
        assert session.get(Task, fixture["task_id"]).status == overrides.get(
            "task_status", TaskStatus.AWAITING_REVIEW)


def test_a_verification_from_another_run_is_refused(review_session_factory):
    factory = review_session_factory
    fixture = _passed_run(factory)
    other = _passed_run(factory)

    with pytest.raises(TaskReviewEvidenceError, match="does not belong"):
        _submit(factory, fixture, verification_id=other["verification_id"])

    assert _reviews(factory, fixture["task_id"]) == []


@pytest.mark.parametrize("overrides", [
    {"reviewer": " "}, {"reviewed_at": ""}, {"record_sha256": "not-a-digest"},
    {"conclusion": "pending"},
])
def test_an_unusable_submission_is_refused_before_any_write(review_session_factory, overrides):
    factory = review_session_factory
    fixture = _passed_run(factory)

    with pytest.raises(TaskReviewError):
        _submit(factory, fixture, **overrides)

    assert _reviews(factory, fixture["task_id"]) == []
    with factory() as session:
        assert session.get(Task, fixture["task_id"]).status == TaskStatus.AWAITING_REVIEW


def test_a_failure_after_the_write_leaves_nothing_behind(review_session_factory, monkeypatch):
    """The row and the Task status are one transaction, so a late failure rolls both back."""
    factory = review_session_factory
    fixture = _passed_run(factory)
    original = TaskReviewService.record

    def fail_after_flush(self, **arguments):
        def explode(_review, _task, *, idempotent):
            raise RuntimeError("simulated failure after the row was written")
        monkeypatch.setattr(TaskReviewService, "_result", staticmethod(explode))
        return original(self, **arguments)

    monkeypatch.setattr(TaskReviewService, "record", fail_after_flush)
    with pytest.raises(RuntimeError, match="simulated failure"):
        _submit(factory, fixture)

    assert _reviews(factory, fixture["task_id"]) == []
    with factory() as session:
        assert session.get(Task, fixture["task_id"]).status == TaskStatus.AWAITING_REVIEW


def test_two_reviewers_submitting_the_same_review_produce_one_row(review_session_factory):
    factory = review_session_factory
    fixture = _passed_run(factory)
    barrier = Barrier(2)

    def submit():
        barrier.wait(timeout=10)
        return _submit(factory, fixture)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(submit), pool.submit(submit)]]

    assert len(_reviews(factory, fixture["task_id"])) == 1
    assert {result["review_id"] for result in results} == {results[0]["review_id"]}
    assert sorted(result["idempotent"] for result in results) == [False, True]


def _record_file(tmp_path, fixture, *, conclusion="accepted", reviewer="reviewer-b",
                 reviewed_at="2026-11-13T10:00:00+00:00", outcome="formal_pass", **overrides):
    from devflow_ai.benchmark_record import BenchmarkRunRecord, write_run_record

    payload = {
        "schema_version": 2,
        "pack_id": "click-3084",
        "base_commit": "7f7bbe4569ea68e8dabee232eade069ef3310aea",
        "task_id": str(fixture["task_id"]),
        "run_id": str(fixture["run_id"]),
        "attempt_index": 1,
        "attempt_kind": "formal",
        "started_at": "2026-11-13T09:00:00+00:00",
        "finished_at": "2026-11-13T09:20:00+00:00",
        "outcome": outcome,
        "model_id": "deepseek-flash",
        "policy_version": "benchmark-click3084-v1",
        "operator": "operator-a",
        "public_checks": {"verified_outcome": "passed"},
        "formal": {"verification_id": str(fixture["verification_id"]),
                   "independent_suite_version": "suite-1"},
        "human_review": {"conclusion": conclusion, "reviewer": reviewer,
                         "reviewed_at": reviewed_at},
        "artifacts": ["runs/click-3084/attempt-1"],
    }
    payload.update(overrides)
    if payload["outcome"] != "formal_pass":
        # A non-passing record cannot carry formal evidence references.
        payload["formal"] = None
    path = tmp_path / "record.json"
    write_run_record(BenchmarkRunRecord.model_validate(payload), path)
    return path


def _review_cli(factory, argv):
    from devflow_ai.task_review import main as review_main

    return review_main(argv, session_factory=factory)


def test_the_cli_records_the_review_from_the_record_file(review_session_factory, tmp_path, capsys):
    factory = review_session_factory
    fixture = _passed_run(factory)
    record = _record_file(tmp_path, fixture)

    exit_code = _review_cli(factory, ["--record", str(record), "--reviewer", "reviewer-b"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["event"] == "task_review_recorded"
    assert payload["task_status"] == "COMPLETED" and payload["idempotent"] is False
    review, = _reviews(factory, fixture["task_id"])
    assert review.record_sha256 == sha256(record.read_bytes()).hexdigest()
    assert review.reviewed_at == "2026-11-13T10:00:00+00:00"


def test_the_cli_is_idempotent_for_the_same_record(review_session_factory, tmp_path, capsys):
    factory = review_session_factory
    fixture = _passed_run(factory)
    record = _record_file(tmp_path, fixture)
    argv = ["--record", str(record), "--reviewer", "reviewer-b"]

    assert _review_cli(factory, argv) == 0
    capsys.readouterr()
    assert _review_cli(factory, argv) == 0

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["idempotent"] is True
    assert len(_reviews(factory, fixture["task_id"])) == 1


@pytest.mark.parametrize(("record_kwargs", "message"), [
    ({"reviewer": "someone-else"}, "does not match the reviewer"),
    ({"conclusion": "pending"}, "no human decision"),
    ({"outcome": "failed"}, "Only a formally passed record"),
])
def test_the_cli_refuses_a_record_that_does_not_match_the_submission(
    review_session_factory, tmp_path, capsys, record_kwargs, message,
):
    factory = review_session_factory
    fixture = _passed_run(factory)
    record = _record_file(tmp_path, fixture, **record_kwargs)

    exit_code = _review_cli(factory, ["--record", str(record), "--reviewer", "reviewer-b"])

    assert exit_code == 2
    assert message in capsys.readouterr().err
    assert _reviews(factory, fixture["task_id"]) == []


def test_the_cli_needs_ids_when_the_record_carries_none(
    review_session_factory, tmp_path, capsys,
):
    factory = review_session_factory
    fixture = _passed_run(factory)
    record = _record_file(tmp_path, fixture, task_id=None, run_id=None)

    assert _review_cli(factory, ["--record", str(record), "--reviewer", "reviewer-b"]) == 2
    assert "task id" in capsys.readouterr().err

    assert _review_cli(factory, [
        "--record", str(record), "--reviewer", "reviewer-b",
        "--task-id", str(fixture["task_id"]), "--run-id", str(fixture["run_id"])]) == 0
    assert len(_reviews(factory, fixture["task_id"])) == 1


def test_the_cli_refuses_a_supplied_id_that_contradicts_the_record(
    review_session_factory, tmp_path, capsys,
):
    factory = review_session_factory
    fixture = _passed_run(factory)
    record = _record_file(tmp_path, fixture)

    exit_code = _review_cli(factory, [
        "--record", str(record), "--reviewer", "reviewer-b", "--run-id", str(uuid4())])

    assert exit_code == 2
    assert "does not match" in capsys.readouterr().err
    assert _reviews(factory, fixture["task_id"]) == []


def test_the_cli_refuses_a_verification_id_that_contradicts_the_record(
    review_session_factory, tmp_path, capsys,
):
    """An override may fill a gap; it must never replace the id the record already binds."""
    factory = review_session_factory
    fixture = _passed_run(factory)
    other = _passed_run(factory)
    record = _record_file(tmp_path, fixture)

    exit_code = _review_cli(factory, [
        "--record", str(record), "--reviewer", "reviewer-b",
        "--verification-id", str(other["verification_id"])])

    assert exit_code == 2
    assert "verification id does not match" in capsys.readouterr().err
    assert _reviews(factory, fixture["task_id"]) == []


def test_the_cli_reads_the_record_once_and_binds_those_bytes(
    review_session_factory, tmp_path, monkeypatch, capsys,
):
    """The digest and the parsed record must come from the same read."""
    factory = review_session_factory
    fixture = _passed_run(factory)
    record = _record_file(tmp_path, fixture)
    original = record.read_bytes()
    reads = []
    real_read_bytes = Path.read_bytes

    def counting_read_bytes(self):
        data = real_read_bytes(self)
        if Path(self) == record:
            reads.append(1)
            if len(reads) > 1:  # a second read would see a mutated file
                record.write_bytes(original.replace(b"reviewer-b", b"reviewer-z"))
        return data

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
    exit_code = _review_cli(factory, ["--record", str(record), "--reviewer", "reviewer-b"])

    assert exit_code == 0
    capsys.readouterr()
    assert len(reads) == 1
    review, = _reviews(factory, fixture["task_id"])
    assert review.record_sha256 == sha256(original).hexdigest()
    assert review.reviewer == "reviewer-b"


@pytest.mark.parametrize(("overrides", "field"), [
    ({"reviewer": "reviewer-c"}, "reviewer"),
    ({"reviewed_at": "2026-11-13T11:00:00+00:00"}, "reviewed_at"),
    ({"conclusion": "rejected"}, "conclusion"),
])
def test_the_same_digest_with_different_content_is_a_conflict_not_idempotent(
    review_session_factory, overrides, field,
):
    factory = review_session_factory
    fixture = _passed_run(factory)
    _submit(factory, fixture)

    with pytest.raises(TaskReviewConflictError, match=field):
        _submit(factory, fixture, **overrides)

    review, = _reviews(factory, fixture["task_id"])
    assert review.conclusion == "accepted" and review.reviewer == "reviewer-b"


def test_a_replay_cannot_point_at_another_verification(review_session_factory):
    """Evidence is checked before idempotency, so a replay cannot swap what it cites."""
    factory = review_session_factory
    fixture = _passed_run(factory)
    other = _passed_run(factory)
    _submit(factory, fixture)

    with pytest.raises(TaskReviewEvidenceError, match="does not belong"):
        _submit(factory, fixture, verification_id=other["verification_id"])

    assert len(_reviews(factory, fixture["task_id"])) == 1


def test_a_matching_suite_version_is_required_and_accepted(review_session_factory):
    factory = review_session_factory
    fixture = _passed_run(factory, suite_version="suite-1")

    result = _submit(factory, fixture, independent_suite_version="suite-1")

    assert result["task_status"] == "COMPLETED"
    review, = _reviews(factory, fixture["task_id"])
    assert review.conclusion == "accepted"


@pytest.mark.parametrize(("declared", "recorded", "message"), [
    (None, "suite-1", "does not declare the independent suite version"),
    ("suite-1", None, "does not record which independent suite"),
    ("suite-1", "suite-2", "but the Verification records 'suite-2'"),
])
def test_a_suite_version_that_cannot_be_matched_refuses_the_review(
    review_session_factory, declared, recorded, message,
):
    factory = review_session_factory
    fixture = _passed_run(factory, suite_version=recorded)

    with pytest.raises(TaskReviewEvidenceError, match=message):
        _submit(factory, fixture, independent_suite_version=declared)

    # Refused means refused: no review row, and the Task has not moved.
    assert _reviews(factory, fixture["task_id"]) == []
    with factory() as session:
        assert session.get(Task, fixture["task_id"]).status == TaskStatus.AWAITING_REVIEW


def test_an_idempotent_replay_still_checks_the_suite_version(review_session_factory):
    """A replay with matching content is idempotent, but not by skipping the evidence check."""
    factory = review_session_factory
    fixture = _passed_run(factory, suite_version="suite-1")
    first = _submit(factory, fixture)
    assert first["idempotent"] is False

    # Same submission: still fine.
    assert _submit(factory, fixture)["idempotent"] is True

    # Now the Verification records a different suite: the replay must fail, not succeed by
    # remembering the earlier outcome.
    with factory.begin() as session:
        verification = session.get(Verification, fixture["verification_id"])
        verification.evidence = {**verification.evidence, "suite_version": "suite-2"}
    with pytest.raises(TaskReviewEvidenceError, match="but the Verification records"):
        _submit(factory, fixture)

    assert len(_reviews(factory, fixture["task_id"])) == 1


def test_the_cli_trims_the_reviewer_so_the_stored_value_matches_a_repeat_submission(
    review_session_factory, tmp_path, capsys,
):
    """The service stores a trimmed reviewer; the CLI must submit the same form."""
    factory = review_session_factory
    fixture = _passed_run(factory)
    record = _record_file(tmp_path, fixture, reviewer="  reviewer-b  ")

    assert _review_cli(factory, ["--record", str(record), "--reviewer", "reviewer-b"]) == 0
    capsys.readouterr()
    assert _review_cli(factory, ["--record", str(record), "--reviewer", "reviewer-b"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["idempotent"] is True

    review, = _reviews(factory, fixture["task_id"])
    assert review.reviewer == "reviewer-b"


def test_an_older_record_shape_still_carries_the_suite_version(
    review_session_factory, tmp_path, capsys,
):
    """Records written before schema_version existed already carried
    formal.independent_suite_version; they must keep working without it being inferred."""
    factory = review_session_factory
    fixture = _passed_run(factory, suite_version="suite-1")
    record = _record_file(tmp_path, fixture)
    payload = json.loads(record.read_text(encoding="utf-8"))
    # The fields version 2 introduced did not exist when this record was written.
    payload.pop("schema_version")
    payload.pop("attempt_kind")
    payload.pop("independent_evaluation")
    record.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = _review_cli(factory, ["--record", str(record), "--reviewer", "reviewer-b"])

    assert exit_code == 0
    capsys.readouterr()
    review, = _reviews(factory, fixture["task_id"])
    assert review.conclusion == "accepted"
