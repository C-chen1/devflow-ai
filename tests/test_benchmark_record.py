import json

import pytest
from pydantic import ValidationError

from contextlib import nullcontext
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

from devflow_ai.benchmark_record import (
    AttemptContext, AttemptKind, BenchmarkRunRecord, FormalEvidence, RunOutcome,
    load_run_record, summarize_run_records, write_run_record,
)
from devflow_ai.benchmark_record import main as record_main


def _record(**overrides):
    payload = {
        "pack_id": "click-3019",
        "base_commit": "00fadb8904387158ce6e9aa1573be770446895c1",
        "attempt_index": 1,
        "attempt_kind": "formal",
        "started_at": "2026-11-12T10:00:00+08:00",
        "finished_at": "2026-11-12T10:20:00+08:00",
        "outcome": "formal_pass",
        "model_id": "deepseek-flash",
        "policy_version": "benchmark-v1",
        "operator": "operator-a",
        "public_checks": {"target_failed": 2, "target_passed": 2},
        "formal": {"verification_id": "v-1", "independent_suite_version": "suite-1"},
        "human_review": {"conclusion": "accepted", "reviewer": "reviewer-b",
                         "reviewed_at": "2026-11-12T11:00:00+08:00"},
        "artifacts": ["runs/click-3019/attempt-1"],
    }
    payload.update(overrides)
    return payload


def test_a_reviewed_formal_pass_counts_as_solved():
    record = BenchmarkRunRecord.model_validate(_record())

    assert record.counts_as_solved is True
    assert summarize_run_records([record])["solved_over_sample"] == "1/1"


def test_a_formal_pass_awaiting_review_is_not_solved():
    record = BenchmarkRunRecord.model_validate(_record(
        human_review={"conclusion": "pending"}))

    assert record.counts_as_solved is False
    summary = summarize_run_records([record])
    assert summary["solved"] == 0
    assert summary["formal_pass_pending_review"] == 1
    assert summary["solved_over_sample"] == "0/1"


def test_a_rejected_formal_pass_is_not_solved():
    record = BenchmarkRunRecord.model_validate(_record(
        human_review={"conclusion": "rejected", "reviewer": "reviewer-b"}))

    assert record.counts_as_solved is False
    summary = summarize_run_records([record])
    assert summary["formal_pass_pending_review"] == 0
    assert summary["formal_pass_rejected"] == 1
    assert summary["solved"] == 0


def _public_only_fields(reason="independent evaluation could not run"):
    """The two halves a public-only pass must record, so a test can vary one at a time."""
    return {
        "formal": None,
        "public_checks": {"target_failed": 2, "target_passed": 2, "verified_outcome": "passed",
                          "verifier_execution_id": "v-1", "draft_artifact_id": "d-1"},
        "independent_evaluation": {"outcome": "unavailable", "verified": True, "reason": reason},
    }


def test_a_formal_pass_needs_its_verification_references():
    with pytest.raises(ValidationError):
        BenchmarkRunRecord.model_validate(_record(formal=None))


@pytest.mark.parametrize("outcome", ["formal_pass", "public_only_pass"])
def test_a_passing_run_must_point_at_its_artifacts(outcome):
    overrides = {"outcome": outcome, "artifacts": []}
    if outcome == "public_only_pass":
        overrides.update(_public_only_fields())
    with pytest.raises(ValidationError):
        BenchmarkRunRecord.model_validate(_record(**overrides))


def test_summary_reports_every_outcome_with_its_sample_size():
    records = [
        BenchmarkRunRecord.model_validate(_record()),
        BenchmarkRunRecord.model_validate(_record(
            attempt_index=2, outcome="public_only_pass", human_review={"conclusion": "pending"},
            **_public_only_fields())),
        BenchmarkRunRecord.model_validate(_record(
            attempt_index=3, outcome="failed", formal=None, artifacts=[],
            notes="public checks still failing")),
        BenchmarkRunRecord.model_validate(_record(
            attempt_index=4, outcome="timeout", formal=None, artifacts=[],
            notes="exceeded the per-run budget")),
        BenchmarkRunRecord.model_validate(_record(
            attempt_index=5, outcome="incomplete", formal=None, artifacts=[],
            notes="the run was still waiting for its next stage")),
    ]

    summary = summarize_run_records(records)

    assert summary["sample_size"] == 5
    assert summary["solved"] == 1
    assert summary["solved_over_sample"] == "1/5"
    assert summary["outcomes"] == {
        "formal_pass": 1, "public_only_pass": 1, "failed": 1, "timeout": 1,
        "incomplete": 1}


def test_unknown_fields_cannot_carry_hidden_material():
    with pytest.raises(ValidationError):
        BenchmarkRunRecord.model_validate(_record(hidden_cases=[{"input": "x"}]))


@pytest.mark.parametrize("review", [
    {"conclusion": "accepted"},
    {"conclusion": "accepted", "reviewer": "reviewer-b"},
    {"conclusion": "accepted", "reviewed_at": "2026-11-12T11:00:00+08:00"},
    {"conclusion": "accepted", "reviewer": "   ", "reviewed_at": "2026-11-12T11:00:00+08:00"},
])
def test_an_acceptance_needs_a_named_reviewer_and_a_time(review):
    with pytest.raises(ValidationError):
        BenchmarkRunRecord.model_validate(_record(human_review=review))


def test_an_acceptance_with_both_is_accepted():
    record = BenchmarkRunRecord.model_validate(_record(
        human_review={"conclusion": "accepted", "reviewer": "reviewer-b",
                      "reviewed_at": "2026-11-12T11:00:00+08:00"}))

    assert record.counts_as_solved is True


def test_a_record_round_trips_and_is_never_overwritten(tmp_path):
    record = BenchmarkRunRecord.model_validate(_record())
    path = tmp_path / "records" / "attempt-1.json"

    write_run_record(record, path)
    assert load_run_record(path) == record

    with pytest.raises(ValueError, match="Refusing to overwrite"):
        write_run_record(record, path)
    assert json.loads(path.read_text(encoding="utf-8"))["pack_id"] == "click-3019"


def _pending_record(tmp_path, **overrides):
    payload = _record(human_review={"conclusion": "pending"}, **overrides)
    return write_run_record(BenchmarkRunRecord.model_validate(payload), tmp_path / "record.json")


def test_a_review_is_written_to_a_new_file_and_never_rewrites_the_record(tmp_path, capsys):
    original = _pending_record(tmp_path)
    before = original.read_bytes()

    exit_code = record_main([
        "review", "--record", str(original), "--conclusion", "accepted",
        "--reviewer", "reviewer-b", "--at", "2026-11-12T11:00:00+00:00"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["counts_as_solved"] is True and payload["conclusion"] == "accepted"
    # The record itself is evidence and keeps its bytes; the review lives beside it.
    assert original.read_bytes() == before
    reviewed = load_run_record(tmp_path / "record.reviewed.json")
    assert reviewed.human_review.reviewer == "reviewer-b"
    assert reviewed.counts_as_solved is True


def test_an_acceptance_without_a_reviewer_is_refused(tmp_path, capsys):
    original = _pending_record(tmp_path)

    exit_code = record_main(["review", "--record", str(original), "--conclusion", "accepted"])

    assert exit_code == 2
    assert "reviewer" in capsys.readouterr().err
    assert not (tmp_path / "record.reviewed.json").exists()


def test_a_review_refuses_to_overwrite_an_existing_copy(tmp_path, capsys):
    original = _pending_record(tmp_path)
    (tmp_path / "record.reviewed.json").write_text("{}", encoding="utf-8")

    exit_code = record_main([
        "review", "--record", str(original), "--conclusion", "rejected"])

    assert exit_code == 2
    assert "overwrite" in capsys.readouterr().err


def test_summarize_reports_the_sample_size_beside_the_solved_count(tmp_path, capsys):
    accepted = _pending_record(tmp_path)
    assert record_main([
        "review", "--record", str(accepted), "--conclusion", "accepted",
        "--reviewer", "reviewer-b", "--at", "2026-11-12T11:00:00+00:00"]) == 0
    capsys.readouterr()
    failed = write_run_record(BenchmarkRunRecord.model_validate(_record(
        attempt_index=2, outcome="failed", formal=None)), tmp_path / "failed.json")

    exit_code = record_main(["summarize", str(tmp_path / "record.reviewed.json"), str(failed)])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["sample_size"] == 2
    assert payload["solved_over_sample"] == "1/2"
    assert payload["outcomes"]["failed"] == 1 and payload["outcomes"]["formal_pass"] == 1


def test_only_formal_attempts_enter_the_sample(tmp_path, capsys):
    formal = _pending_record(tmp_path)
    assert record_main([
        "review", "--record", str(formal), "--conclusion", "accepted",
        "--reviewer", "reviewer-b", "--at", "2026-11-12T11:00:00+00:00"]) == 0
    capsys.readouterr()
    rehearsal = write_run_record(BenchmarkRunRecord.model_validate(_record(
        attempt_index=2, outcome="failed", formal=None, attempt_kind="rehearsal")),
        tmp_path / "rehearsal.json")
    unknown = write_run_record(BenchmarkRunRecord.model_validate(_record(
        attempt_index=3, outcome="failed", formal=None, attempt_kind="unknown")),
        tmp_path / "unknown.json")
    reviewed_formal = tmp_path / "record.reviewed.json"

    exit_code = record_main(["summarize", str(reviewed_formal), str(rehearsal), str(unknown)])
    excluded = json.loads(capsys.readouterr().out.strip())
    assert exit_code == 0
    assert excluded["sample_size"] == 1
    assert excluded["rehearsals_excluded"] == 1 and excluded["unknown_kind_excluded"] == 1
    assert excluded["non_formal_excluded"] == 2
    assert excluded["solved_over_sample"] == "1/1"

    included = record_main(["summarize", str(reviewed_formal), str(rehearsal), str(unknown),
                            "--include-non-formal"])
    payload = json.loads(capsys.readouterr().out.strip())
    assert included == 0
    assert payload["sample_size"] == 3 and payload["non_formal_excluded"] == 0
    assert payload["solved_over_sample"] == "1/3"


def test_a_record_written_before_the_field_existed_reads_as_unknown(tmp_path):
    """Old records must stay readable — and must not be assumed to be formal samples."""
    legacy = _record(outcome="failed", formal=None)
    legacy.pop("attempt_kind")
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")

    record = load_run_record(path)

    assert record.attempt_kind is AttemptKind.UNKNOWN
    assert record.counts_as_solved is False
    assert summarize_run_records([record])["sample_size"] == 0
    assert summarize_run_records([record])["unknown_kind_excluded"] == 1


def test_a_rehearsal_that_formally_passed_still_does_not_count_as_solved():
    record = BenchmarkRunRecord.model_validate(_record(attempt_kind="rehearsal"))

    assert record.outcome is RunOutcome.FORMAL_PASS
    assert record.human_review.conclusion == "accepted"
    assert record.counts_as_solved is False


def test_a_public_only_pass_needs_the_verified_public_result():
    """The label may not stand in for the evidence that the public checks actually passed."""
    fields = _public_only_fields()
    fields["public_checks"] = {"target_failed": 2, "target_passed": 2}

    with pytest.raises(ValidationError, match="verified public result"):
        BenchmarkRunRecord.model_validate(
            _record(outcome="public_only_pass", **fields))


def test_a_public_only_pass_needs_the_reason_the_evaluation_was_unavailable():
    fields = _public_only_fields()
    fields["independent_evaluation"] = {"outcome": "unavailable", "verified": True}

    with pytest.raises(ValidationError, match="reason"):
        BenchmarkRunRecord.model_validate(
            _record(outcome="public_only_pass", **fields))

    # A reason alone is not enough: the attempt has to be verified, not merely described.
    fields["independent_evaluation"] = {"outcome": "unavailable",
                                        "reason": "docker is absent"}
    with pytest.raises(ValidationError, match="verified"):
        BenchmarkRunRecord.model_validate(
            _record(outcome="public_only_pass", **fields))

    fields["independent_evaluation"] = {"outcome": "unavailable", "verified": True,
                                        "reason": "docker is absent"}
    record = BenchmarkRunRecord.model_validate(_record(outcome="public_only_pass", **fields))
    assert record.independent_evaluation.reason == "docker is absent"


def test_an_independent_failure_without_calibration_is_not_a_verdict():
    uncalibrated = {**_public_only_fields(), "outcome": "failed", "artifacts": [],
                    "independent_evaluation": {"outcome": "failed", "verified": True,
                                               "calibrated": False}}
    with pytest.raises(ValidationError, match="calibrated"):
        BenchmarkRunRecord.model_validate(_record(**uncalibrated))

    unverified = {**_public_only_fields(), "outcome": "failed", "artifacts": [],
                  "independent_evaluation": {"outcome": "failed", "calibrated": True}}
    with pytest.raises(ValidationError, match="verified"):
        BenchmarkRunRecord.model_validate(_record(**unverified))

    calibrated = {**_public_only_fields(), "outcome": "failed", "artifacts": [],
                  "independent_evaluation": {"outcome": "failed", "verified": True,
                                             "calibrated": True,
                                             "receipt_artifact_id": "r-1",
                                             "suite_version": "suite-1"}}
    record = BenchmarkRunRecord.model_validate(_record(**calibrated))
    assert record.outcome is RunOutcome.FAILED
    # Both halves stay on the record: the public pass and the independent failure.
    assert record.public_checks.verified_outcome == "passed"
    assert record.independent_evaluation.outcome == "failed"


def test_a_failure_without_independent_evidence_is_still_a_failure():
    """A run that failed before any evaluation ran is not forced to invent one."""
    record = BenchmarkRunRecord.model_validate(_record(
        outcome="failed", formal=None, artifacts=[], notes="public checks failed"))

    assert record.independent_evaluation is None


def _reviewed_copy(**overrides):
    """A record that already carries a human decision, as the review CLI writes it."""
    return BenchmarkRunRecord.model_validate(_record(
        public_checks={"target_failed": None, "target_passed": None,
                       "notes": "summary written before the formal transaction replaced the checkpoint"},
        **overrides))


def _repaired_decision(*, outcome="formal_pass"):
    from devflow_ai.benchmark_record import IndependentEvaluationEvidence, PublicCheckSummary
    from devflow_ai.benchmark_run import OutcomeDecision

    return OutcomeDecision(
        outcome=RunOutcome(outcome),
        formal=FormalEvidence(verification_id="v-1", independent_suite_version="suite-1"),
        public_checks=PublicCheckSummary(
            verified_outcome="passed", verifier_execution_id="exec-1", draft_artifact_id="d-1"),
        independent=IndependentEvaluationEvidence(
            outcome="passed", verified=True, calibrated=True,
            receipt_artifact_id="r-1", suite_version="suite-1"),
        reason="")


def test_a_correction_keeps_the_human_decision_and_links_both_sources(tmp_path):
    from devflow_ai.benchmark_record import corrected_record

    original = _pending_record(tmp_path)
    reviewed = write_run_record(_reviewed_copy(), tmp_path / "reviewed.json")
    decision = _repaired_decision()

    corrected = corrected_record(
        load_run_record(original), load_run_record(reviewed),
        public_checks=decision.public_checks, independent=decision.independent,
        basis="Verification v-1; draft d-1; receipt r-1",
        corrected_by="reviewer-b", corrected_at="2026-11-12T12:00:00+00:00",
        corrects_sha256=sha256(original.read_bytes()).hexdigest(),
        reviewed_sha256=sha256(reviewed.read_bytes()).hexdigest(),
        note="summary re-derived after the formal transaction")

    assert corrected.human_review.conclusion == "accepted"
    assert corrected.human_review.reviewer == "reviewer-b"
    assert corrected.public_checks.verified_outcome == "passed"
    assert corrected.independent_evaluation.outcome == "passed"
    assert corrected.correction.corrects_sha256 == sha256(original.read_bytes()).hexdigest()
    assert "summary re-derived after the formal transaction" in corrected.notes
    # The pure builder writes nothing: the original file still has no correction block.
    assert json.loads(original.read_text(encoding="utf-8"))["correction"] is None


def test_a_correction_needs_an_existing_human_decision(tmp_path):
    from devflow_ai.benchmark_record import corrected_record

    original = _pending_record(tmp_path)
    decision = _repaired_decision()

    with pytest.raises(ValueError, match="human decision"):
        corrected_record(
            load_run_record(original), load_run_record(original),
            public_checks=decision.public_checks, independent=decision.independent,
            basis="b", corrected_by="reviewer-b",
            corrected_at="2026-11-12T12:00:00+00:00",
            corrects_sha256="0" * 64, reviewed_sha256="1" * 64)


def test_a_correction_cannot_retarget_another_attempt(tmp_path):
    from devflow_ai.benchmark_record import corrected_record

    original = _pending_record(tmp_path)
    reviewed = write_run_record(_reviewed_copy(attempt_index=3), tmp_path / "reviewed.json")
    decision = _repaired_decision()

    with pytest.raises(ValueError, match="attempt_index"):
        corrected_record(
            load_run_record(original), load_run_record(reviewed),
            public_checks=decision.public_checks, independent=decision.independent,
            basis="b", corrected_by="reviewer-b",
            corrected_at="2026-11-12T12:00:00+00:00",
            corrects_sha256="0" * 64, reviewed_sha256="1" * 64)


def test_a_correction_rejects_different_run_and_task_ids(tmp_path):
    from devflow_ai.benchmark_record import corrected_record
    first_run, second_run, task_id = str(uuid4()), str(uuid4()), str(uuid4())
    original = _pending_record(tmp_path, run_id=first_run, task_id=task_id)
    decision = _repaired_decision()
    for reviewed in (_reviewed_copy(run_id=second_run, task_id=task_id),
                     _reviewed_copy(run_id=first_run, task_id=str(uuid4()))):
        with pytest.raises(ValueError, match="run_id|task_id"):
            corrected_record(load_run_record(original), reviewed, public_checks=decision.public_checks,
                independent=decision.independent, basis="b", corrected_by="reviewer-b",
                corrected_at="2026-11-12T12:00:00+00:00", corrects_sha256="0" * 64,
                reviewed_sha256="1" * 64)


def test_correction_refuses_explicit_other_run_before_querying(tmp_path, monkeypatch, capsys):
    import devflow_ai.benchmark_run as benchmark_run_module
    selected, other = str(uuid4()), str(uuid4())
    original = _pending_record(tmp_path, run_id=selected)
    reviewed = write_run_record(_reviewed_copy(run_id=selected), tmp_path / "reviewed.json")
    queried = []
    monkeypatch.setattr(benchmark_run_module, "classify_outcome",
                        lambda *args, **kwargs: queried.append(True) or _repaired_decision())
    out = tmp_path / "corrected.json"
    assert record_main(["correct", "--record", str(original), "--reviewed", str(reviewed),
        "--out", str(out), "--run", other, "--artifacts-root", str(tmp_path),
        "--basis", "b", "--corrected-by", "reviewer", "--at", "2026-11-12T12:00:00+00:00"],
        session_factory=lambda: nullcontext(None)) == 2
    assert not queried and not out.exists()
    assert "run_id" in capsys.readouterr().err


def test_correction_rejects_task_mismatch_with_database_run(tmp_path, monkeypatch, capsys):
    import devflow_ai.benchmark_run as benchmark_run_module
    run_id, task_id = uuid4(), uuid4()
    original = _pending_record(tmp_path, run_id=str(run_id), task_id=str(task_id))
    reviewed = write_run_record(_reviewed_copy(run_id=str(run_id), task_id=str(task_id)), tmp_path / "reviewed.json")
    queried = []
    monkeypatch.setattr(benchmark_run_module, "classify_outcome",
                        lambda *args, **kwargs: queried.append(True) or _repaired_decision())
    session = SimpleNamespace(get=lambda model, selected: SimpleNamespace(task_id=uuid4()))
    assert record_main(["correct", "--record", str(original), "--reviewed", str(reviewed),
        "--out", str(tmp_path / "out.json"), "--artifacts-root", str(tmp_path), "--basis", "b",
        "--corrected-by", "reviewer", "--at", "2026-11-12T12:00:00+00:00"],
        session_factory=lambda: nullcontext(session)) == 2
    assert not queried and "task_id" in capsys.readouterr().err


def test_correction_checks_task_run_binding_in_postgresql(run_session_factory, tmp_path, monkeypatch, capsys):
    import devflow_ai.benchmark_run as benchmark_run_module
    from devflow_ai.enums import TaskStatus
    from devflow_ai.services import AdmissionRecord, TaskService
    from devflow_ai.worker import Worker

    factory = run_session_factory
    with factory() as session:
        task, _ = TaskService(session).create_task(title="fixture", description="fixture", repo="fixture",
                                                   idempotency_key=uuid4())
        task_id = task.id
    with factory() as session:
        TaskService(session).apply_admission_decision(task_id, TaskStatus.ELIGIBLE, base_commit="a" * 40,
            admission_record=AdmissionRecord(reason="fixture", evidence=["fixture"]))
    run_id = Worker(factory, "correction-binding-worker").claim_once().run_id
    declared_other_task = str(uuid4())
    original = _pending_record(tmp_path, run_id=str(run_id), task_id=declared_other_task)
    reviewed = write_run_record(_reviewed_copy(run_id=str(run_id), task_id=declared_other_task),
                                tmp_path / "reviewed.json")
    queried = []
    monkeypatch.setattr(benchmark_run_module, "classify_outcome",
                        lambda *args, **kwargs: queried.append(True) or _repaired_decision())
    assert record_main(["correct", "--record", str(original), "--reviewed", str(reviewed),
        "--out", str(tmp_path / "out.json"), "--artifacts-root", str(tmp_path), "--basis", "b",
        "--corrected-by", "reviewer", "--at", "2026-11-12T12:00:00+00:00"], session_factory=factory) == 2
    assert not queried and "task_id" in capsys.readouterr().err


def test_correction_rejects_different_formal_verification(tmp_path, monkeypatch, capsys):
    import devflow_ai.benchmark_run as benchmark_run_module
    from dataclasses import replace
    original = _pending_record(tmp_path)
    reviewed = write_run_record(_reviewed_copy(), tmp_path / "reviewed.json")
    wrong = replace(_repaired_decision(), formal=FormalEvidence(
        verification_id="other-verification", independent_suite_version="suite-1"))
    monkeypatch.setattr(benchmark_run_module, "classify_outcome", lambda *args, **kwargs: wrong)
    assert record_main(["correct", "--record", str(original), "--reviewed", str(reviewed),
        "--out", str(tmp_path / "out.json"), "--run", str(uuid4()), "--artifacts-root", str(tmp_path),
        "--basis", "b", "--corrected-by", "reviewer", "--at", "2026-11-12T12:00:00+00:00"],
        session_factory=lambda: nullcontext(None)) == 2
    assert "formal verification" in capsys.readouterr().err


def test_correction_refuses_source_changed_during_evidence_lookup(tmp_path, monkeypatch, capsys):
    import devflow_ai.benchmark_run as benchmark_run_module
    original = _pending_record(tmp_path)
    reviewed = write_run_record(_reviewed_copy(), tmp_path / "reviewed.json")
    out = tmp_path / "corrected.json"
    def classify(*args, **kwargs):
        payload = json.loads(original.read_bytes())
        payload["model_id"] = "changed-after-load"
        original.write_text(json.dumps(payload), encoding="utf-8")
        return _repaired_decision()
    monkeypatch.setattr(benchmark_run_module, "classify_outcome", classify)
    assert record_main(["correct", "--record", str(original), "--reviewed", str(reviewed),
        "--out", str(out), "--run", str(uuid4()), "--artifacts-root", str(tmp_path),
        "--basis", "b", "--corrected-by", "reviewer", "--at", "2026-11-12T12:00:00+00:00"],
        session_factory=lambda: nullcontext(None)) == 2
    assert not out.exists() and "changed during correction" in capsys.readouterr().err


def test_summarize_refuses_record_changed_after_parse(tmp_path, monkeypatch, capsys):
    import devflow_ai.benchmark_record as module
    path = write_run_record(BenchmarkRunRecord.model_validate(_record()), tmp_path / "record.json")
    original_load = module.load_run_record
    def load_then_change(selected):
        result = original_load(selected)
        payload = json.loads(path.read_bytes())
        payload["model_id"] = "changed-after-load"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return result
    monkeypatch.setattr(module, "load_run_record", load_then_change)
    assert record_main(["summarize", str(path)]) == 1
    assert "run_record_unreadable" in capsys.readouterr().err


def test_summarize_refuses_repeated_record_path(tmp_path, capsys):
    path = write_run_record(BenchmarkRunRecord.model_validate(_record()), tmp_path / "record.json")
    assert record_main(["summarize", str(path), str(path)]) == 1
    assert "run_record_unreadable" in capsys.readouterr().err


def test_correction_records_digest_of_exact_parsed_bytes(tmp_path, monkeypatch):
    import devflow_ai.benchmark_run as benchmark_run_module
    original = _pending_record(tmp_path)
    reviewed = write_run_record(_reviewed_copy(), tmp_path / "reviewed.json")
    original_bytes, reviewed_bytes = original.read_bytes(), reviewed.read_bytes()
    monkeypatch.setattr(benchmark_run_module, "classify_outcome", lambda *args, **kwargs: _repaired_decision())
    out = tmp_path / "corrected.json"
    assert record_main(["correct", "--record", str(original), "--reviewed", str(reviewed),
        "--out", str(out), "--run", str(uuid4()), "--artifacts-root", str(tmp_path),
        "--basis", "b", "--corrected-by", "reviewer", "--at", "2026-11-12T12:00:00+00:00"],
        session_factory=lambda: nullcontext(None)) == 0
    record = load_run_record(out)
    assert record.correction.corrects_sha256 == sha256(original_bytes).hexdigest()
    assert record.correction.reviewed_sha256 == sha256(reviewed_bytes).hexdigest()


def test_correction_cannot_bind_unidentified_nonformal_legacy_record(tmp_path, capsys):
    original = _pending_record(tmp_path, outcome="incomplete", formal=None, artifacts=[])
    reviewed = write_run_record(_reviewed_copy(outcome="incomplete", formal=None, artifacts=[]),
                                tmp_path / "reviewed.json")
    assert record_main(["correct", "--record", str(original), "--reviewed", str(reviewed),
        "--out", str(tmp_path / "out.json"), "--run", str(uuid4()), "--artifacts-root", str(tmp_path),
        "--basis", "b", "--corrected-by", "reviewer", "--at", "2026-11-12T12:00:00+00:00"],
        session_factory=lambda: nullcontext(None)) == 2
    assert "needs formal verification" in capsys.readouterr().err


def test_the_cli_writes_a_correction_and_leaves_both_inputs_untouched(
    tmp_path, monkeypatch, capsys,
):
    import devflow_ai.benchmark_run as benchmark_run_module

    original = _pending_record(tmp_path)
    reviewed = write_run_record(_reviewed_copy(), tmp_path / "reviewed.json")
    before = (original.read_bytes(), reviewed.read_bytes())
    monkeypatch.setattr(benchmark_run_module, "classify_outcome",
                        lambda *args, **kwargs: _repaired_decision())
    out = tmp_path / "corrected.json"

    exit_code = record_main([
        "correct", "--record", str(original), "--reviewed", str(reviewed),
        "--out", str(out), "--run", str(uuid4()), "--artifacts-root", str(tmp_path),
        "--basis", "Verification v-1; draft d-1; receipt r-1",
        "--corrected-by", "reviewer-b", "--at", "2026-11-12T12:00:00+00:00"],
        session_factory=lambda: nullcontext(None))

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["event"] == "run_record_corrected"
    assert payload["counts_as_solved"] is True
    assert (original.read_bytes(), reviewed.read_bytes()) == before
    saved = load_run_record(out)
    assert saved.human_review.conclusion == "accepted"
    assert saved.public_checks.verified_outcome == "passed"
    assert saved.correction.corrected_by == "reviewer-b"


def test_the_cli_refuses_to_correct_into_an_existing_or_input_file(
    tmp_path, monkeypatch, capsys,
):
    import devflow_ai.benchmark_run as benchmark_run_module

    original = _pending_record(tmp_path)
    reviewed = write_run_record(_reviewed_copy(), tmp_path / "reviewed.json")
    monkeypatch.setattr(benchmark_run_module, "classify_outcome",
                        lambda *args, **kwargs: _repaired_decision())

    for target in (original, reviewed):
        assert record_main([
            "correct", "--record", str(original), "--reviewed", str(reviewed),
            "--out", str(target), "--run", str(uuid4()),
            "--artifacts-root", str(tmp_path), "--basis", "b",
            "--corrected-by", "reviewer-b", "--at", "2026-11-12T12:00:00+00:00"],
            session_factory=lambda: nullcontext(None)) == 2
    assert "new file" in capsys.readouterr().err


def test_the_cli_will_not_use_a_correction_to_change_the_outcome(
    tmp_path, monkeypatch, capsys,
):
    import devflow_ai.benchmark_run as benchmark_run_module

    original = _pending_record(tmp_path)
    reviewed = write_run_record(_reviewed_copy(), tmp_path / "reviewed.json")
    monkeypatch.setattr(benchmark_run_module, "classify_outcome",
                        lambda *args, **kwargs: _repaired_decision(outcome="public_only_pass"))

    exit_code = record_main([
        "correct", "--record", str(original), "--reviewed", str(reviewed),
        "--out", str(tmp_path / "corrected.json"), "--run", str(uuid4()),
        "--artifacts-root", str(tmp_path), "--basis", "b",
        "--corrected-by", "reviewer-b", "--at", "2026-11-12T12:00:00+00:00"],
        session_factory=lambda: nullcontext(None))

    assert exit_code == 2
    assert "cannot change" in capsys.readouterr().err
    assert not (tmp_path / "corrected.json").exists()


def test_summarizing_counts_a_corrected_copy_instead_of_the_record_it_supersedes(
    tmp_path, monkeypatch, capsys,
):
    """Counting both would double one attempt's sample; the correction link prevents that."""
    import devflow_ai.benchmark_run as benchmark_run_module

    original = _pending_record(tmp_path)
    reviewed = write_run_record(_reviewed_copy(), tmp_path / "reviewed.json")
    monkeypatch.setattr(benchmark_run_module, "classify_outcome",
                        lambda *args, **kwargs: _repaired_decision())
    corrected = tmp_path / "corrected.json"
    assert record_main([
        "correct", "--record", str(original), "--reviewed", str(reviewed),
        "--out", str(corrected), "--run", str(uuid4()), "--artifacts-root", str(tmp_path),
        "--basis", "b", "--corrected-by", "reviewer-b",
        "--at", "2026-11-12T12:00:00+00:00"],
        session_factory=lambda: nullcontext(None)) == 0
    capsys.readouterr()

    assert record_main(["summarize", str(original), str(corrected)]) == 0
    summary = json.loads(capsys.readouterr().out.strip())
    assert summary["sample_size"] == 1 and summary["superseded_excluded"] == 1
    assert summary["solved_over_sample"] == "1/1"

    assert record_main(["summarize", str(original), str(corrected),
                        "--include-superseded"]) == 0
    everything = json.loads(capsys.readouterr().out.strip())
    assert everything["sample_size"] == 2 and everything["superseded_excluded"] == 0


def test_the_correction_can_read_the_run_id_from_the_record(tmp_path, monkeypatch, capsys):
    """A record that names its Run needs no --run on the command line."""
    import devflow_ai.benchmark_run as benchmark_run_module

    run_id = uuid4()
    task_id = uuid4()
    original = _pending_record(tmp_path, run_id=str(run_id), task_id=str(task_id))
    reviewed = write_run_record(_reviewed_copy(run_id=str(run_id), task_id=str(task_id)), tmp_path / "reviewed.json")
    captured = {}

    def classify(session, selected_run_id, **kwargs):
        captured["run_id"] = selected_run_id
        return _repaired_decision()

    monkeypatch.setattr(benchmark_run_module, "classify_outcome", classify)

    assert record_main([
        "correct", "--record", str(original), "--reviewed", str(reviewed),
        "--out", str(tmp_path / "corrected.json"), "--artifacts-root", str(tmp_path),
        "--basis", "b", "--corrected-by", "reviewer-b",
        "--at", "2026-11-12T12:00:00+00:00"],
        session_factory=lambda: nullcontext(SimpleNamespace(get=lambda model, selected: SimpleNamespace(task_id=task_id)))) == 0
    assert captured["run_id"] == run_id


def test_a_correction_without_any_run_id_is_refused(tmp_path, capsys):
    original = _pending_record(tmp_path)
    reviewed = write_run_record(_reviewed_copy(), tmp_path / "reviewed.json")

    exit_code = record_main([
        "correct", "--record", str(original), "--reviewed", str(reviewed),
        "--out", str(tmp_path / "corrected.json"), "--artifacts-root", str(tmp_path),
        "--basis", "b", "--corrected-by", "reviewer-b",
        "--at", "2026-11-12T12:00:00+00:00"],
        session_factory=lambda: nullcontext(None))

    assert exit_code == 2
    assert "run_id" in capsys.readouterr().err


def _record_with_correction(tmp_path, *, name, corrects, reviewed_sha, **overrides):
    payload = _record(
        correction={"corrects_sha256": corrects, "reviewed_sha256": reviewed_sha,
                    "basis": "test basis", "corrected_at": "2026-11-12T12:00:00+00:00",
                    "corrected_by": "reviewer-b"},
        **overrides)
    return write_run_record(BenchmarkRunRecord.model_validate(payload), tmp_path / name)


def test_foreign_correction_cannot_hide_a_different_run(tmp_path):
    from devflow_ai.benchmark_record import deduplicate_records
    first_id, second_id = str(uuid4()), str(uuid4())
    first = write_run_record(BenchmarkRunRecord.model_validate(_record(run_id=first_id)), tmp_path / "first.json")
    second = write_run_record(BenchmarkRunRecord.model_validate(_record(run_id=second_id)), tmp_path / "second.json")
    bad = _record_with_correction(tmp_path, name="bad.json", run_id=first_id,
        corrects=sha256(second.read_bytes()).hexdigest(), reviewed_sha=sha256(first.read_bytes()).hexdigest())
    records, details = deduplicate_records([(path, load_run_record(path)) for path in (first, second, bad)])
    assert [record.run_id for record in records] == [second_id]
    assert details["conflicts_excluded"] == 1


def test_reviewed_copy_cannot_hide_conflicting_immutable_fields(tmp_path):
    from devflow_ai.benchmark_record import deduplicate_records
    run_id = str(uuid4())
    original = write_run_record(BenchmarkRunRecord.model_validate(_record(
        run_id=run_id, human_review={"conclusion": "pending"})), tmp_path / "pending.json")
    altered = write_run_record(BenchmarkRunRecord.model_validate(_record(
        run_id=run_id, model_id="different-model")), tmp_path / "reviewed.json")
    records, details = deduplicate_records([(path, load_run_record(path)) for path in (original, altered)])
    assert records == []
    assert details["conflicts_excluded"] == 1


def test_an_original_its_review_copy_and_its_correction_count_once(tmp_path, capsys):
    """The exact shape that produced 2/2: all three files describe one attempt."""
    original = _pending_record(tmp_path)
    reviewed = write_run_record(_reviewed_copy(), tmp_path / "reviewed.json")
    corrected = _record_with_correction(
        tmp_path, name="corrected.json",
        corrects=sha256(original.read_bytes()).hexdigest(),
        reviewed_sha=sha256(reviewed.read_bytes()).hexdigest())

    exit_code = record_main([
        "summarize", str(original), str(reviewed), str(corrected)])

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out.strip())
    assert summary["sample_size"] == 1
    assert summary["superseded_excluded"] == 2
    assert summary["solved_over_sample"] == "1/1"
    assert summary["conflicts"] == []


def test_two_corrections_for_the_same_attempt_are_reported_as_a_conflict(tmp_path, capsys):
    original = _pending_record(tmp_path)
    reviewed = write_run_record(_reviewed_copy(), tmp_path / "reviewed.json")
    reviewed_sha = sha256(reviewed.read_bytes()).hexdigest()
    first = _record_with_correction(
        tmp_path, name="corrected-a.json",
        corrects=sha256(original.read_bytes()).hexdigest(), reviewed_sha=reviewed_sha)
    second = _record_with_correction(
        tmp_path, name="corrected-b.json",
        corrects=sha256(original.read_bytes()).hexdigest(), reviewed_sha=reviewed_sha)

    assert record_main(["summarize", str(original), str(reviewed),
                        str(first), str(second)]) == 0
    summary = json.loads(capsys.readouterr().out.strip())

    # Neither copy may be picked: guessing would invent which correction is current.
    assert summary["sample_size"] == 0
    assert summary["conflicts_excluded"] == 1
    assert "more than one correction" in summary["conflicts"][0]["reason"]
    assert sorted(summary["conflicts"][0]["files"]) == ["corrected-a.json", "corrected-b.json"]


def test_a_chain_of_corrections_counts_only_the_latest(tmp_path, capsys):
    original = _pending_record(tmp_path)
    reviewed = write_run_record(_reviewed_copy(), tmp_path / "reviewed.json")
    first = _record_with_correction(
        tmp_path, name="corrected-a.json",
        corrects=sha256(original.read_bytes()).hexdigest(),
        reviewed_sha=sha256(reviewed.read_bytes()).hexdigest())
    second = _record_with_correction(
        tmp_path, name="corrected-b.json",
        corrects=sha256(first.read_bytes()).hexdigest(),
        reviewed_sha=sha256(reviewed.read_bytes()).hexdigest())

    assert record_main(["summarize", str(original), str(reviewed), str(first),
                        str(second)]) == 0
    summary = json.loads(capsys.readouterr().out.strip())

    assert summary["sample_size"] == 1
    assert summary["superseded_excluded"] == 3
    assert summary["conflicts"] == []


def test_a_record_without_a_schema_version_reads_as_version_one(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(_record()), encoding="utf-8")

    record = load_run_record(path)

    assert record.schema_version == 1
    # Older files are not silently upgraded: the report says they carry no version.
    assert summarize_run_records([record])["records_without_schema_version"] == 1


def test_a_record_from_a_newer_schema_is_refused(tmp_path):
    from devflow_ai.benchmark_record import SCHEMA_VERSION

    path = tmp_path / "future.json"
    path.write_text(json.dumps(_record(schema_version=SCHEMA_VERSION + 1)), encoding="utf-8")

    with pytest.raises(ValueError, match="newer than this code"):
        load_run_record(path)


def test_a_corrected_copy_is_written_under_the_current_schema(tmp_path, monkeypatch, capsys):
    import devflow_ai.benchmark_run as benchmark_run_module
    from devflow_ai.benchmark_record import SCHEMA_VERSION

    original = _pending_record(tmp_path)
    reviewed = write_run_record(_reviewed_copy(), tmp_path / "reviewed.json")
    monkeypatch.setattr(benchmark_run_module, "classify_outcome",
                        lambda *args, **kwargs: _repaired_decision())
    out = tmp_path / "corrected.json"

    assert record_main([
        "correct", "--record", str(original), "--reviewed", str(reviewed),
        "--out", str(out), "--run", str(uuid4()), "--artifacts-root", str(tmp_path),
        "--basis", "b", "--corrected-by", "reviewer-b",
        "--at", "2026-11-12T12:00:00+00:00"],
        session_factory=lambda: nullcontext(None)) == 0

    assert load_run_record(out).schema_version == SCHEMA_VERSION
    assert json.loads(out.read_text(encoding="utf-8"))["schema_version"] == SCHEMA_VERSION


def test_an_attempt_context_round_trips_through_a_record(tmp_path):
    payload = _record(attempt_context={
        "deadline": "fired", "finalization": "recorded",
        "finalization_outcome": "not_finalized", "source": "benchmark_attempt"})
    record = BenchmarkRunRecord.model_validate(payload)
    path = write_run_record(record, tmp_path / "record.json")

    loaded = load_run_record(path)

    assert loaded.attempt_context is not None
    assert loaded.attempt_context.deadline == "fired"
    assert loaded.attempt_context.finalization == "recorded"
    assert loaded.attempt_context.finalization_outcome == "not_finalized"
    assert loaded.attempt_context.source == "benchmark_attempt"
    assert loaded == record


def test_a_record_without_an_attempt_context_reads_as_absent_not_as_quiet(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(_record()), encoding="utf-8")

    record = load_run_record(path)

    # Absent is not "no deadline, no finalization": a reader has to treat it as unknown.
    assert record.attempt_context is None
    unknown = AttemptContext.unknown(source="backfill_reconstruction")
    assert unknown.deadline == "unknown"
    assert unknown.finalization == "unknown"


def test_only_a_recorded_finalization_carries_an_outcome():
    with pytest.raises(ValidationError):
        AttemptContext(deadline="fired", finalization="not_recorded",
                       finalization_outcome="finalized", source="benchmark_attempt")


def test_an_attempt_context_can_name_the_execution_version_it_ran(tmp_path):
    digest = "a" * 64
    context = AttemptContext.observed(
        deadline_fired=False, finalization=None, execution_sha256=digest,
        source="worker_run")
    assert context.execution_sha256 == digest

    path = write_run_record(BenchmarkRunRecord.model_validate(
        _record(attempt_context=context.model_dump(mode="json"))),
        tmp_path / "record.json")
    assert load_run_record(path).attempt_context.execution_sha256 == digest

    with pytest.raises(ValidationError):
        AttemptContext(deadline="unknown", finalization="unknown",
                       execution_sha256="not-a-digest", source="worker_run")


def test_a_live_attempt_context_separates_fired_from_recorded():
    fired = AttemptContext.observed(deadline_fired=True, finalization={"outcome": "finalized"},
                                    source="benchmark_attempt")
    assert (fired.deadline, fired.finalization, fired.finalization_outcome) == (
        "fired", "recorded", "finalized")

    quiet = AttemptContext.observed(deadline_fired=False, finalization=None, source="worker_run")
    assert (quiet.deadline, quiet.finalization, quiet.finalization_outcome) == (
        "not_fired", "not_recorded", None)


def test_an_older_declared_schema_is_not_reported_as_unversioned(tmp_path):
    from devflow_ai.benchmark_record import SCHEMA_VERSION

    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps(_record()), encoding="utf-8")
    older = tmp_path / "older.json"
    older.write_text(json.dumps(_record(schema_version=2)), encoding="utf-8")
    current = tmp_path / "current.json"
    current.write_text(json.dumps(_record(schema_version=SCHEMA_VERSION)), encoding="utf-8")

    # A file that declares no version reads as version 1; a v2 file declared its version and
    # must not be counted as unversioned just because this code is newer.
    assert summarize_run_records(
        [load_run_record(legacy)])["records_without_schema_version"] == 1
    summary = summarize_run_records([load_run_record(older)])
    assert summary["records_without_schema_version"] == 0
    assert summary["records_below_current_schema"] == 1
    assert summarize_run_records(
        [load_run_record(current)])["records_below_current_schema"] == 0
