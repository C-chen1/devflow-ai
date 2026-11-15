"""The scheduled attempt leaves a reviewable record; a person reviews it afterwards.

The record is composed by the same classifier and written by the same exclusive-create path
as the benchmark entry, so both entries are labelled by one set of rules. Nothing here fills
in a human decision: the record is exactly what a reviewer is asked to read.

Fixtures are shared with ``test_worker_finalize_chain`` (isolated schema, controlled git
repository, model stubs); a constructed draft stands in for the Verifier stage where that
would need a Windows Job.
"""
import json
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from time import sleep

import pytest
from sqlalchemy import select

import devflow_ai.benchmark_record as benchmark_record_module
from devflow_ai.benchmark_record import load_run_record
from devflow_ai.benchmark_run import BenchmarkLimits
from devflow_ai.coordinator import ModelTokenUsage
from devflow_ai.enums import RunStatus
from devflow_ai.model_gateway import ModelReply
from devflow_ai.model_gateways import model_gateways_from_env
from devflow_ai.models import Run, Verification
from devflow_ai.worker_run import run_worker_once
from devflow_ai.worker_scheduler import (
    SchedulerManifestError, load_scheduler_manifest, run_scheduler,
)

from devflow_ai.worker_scheduler import SchedulerRequest
from test_worker_finalize_chain import (
    _drafting_run_once, _formal_prepare, _gateways, _heartbeat, _manifest, _policy_file,
    _Registered, _request, _material_file, _write_draft,
)


def _request_with_limits(tmp_path, manifest, limits):
    base = _request(tmp_path, manifest)
    return SchedulerRequest(
        manifest_path=base.manifest_path, operator=base.operator,
        python_executable=base.python_executable, workspaces_root=base.workspaces_root,
        artifacts_root=base.artifacts_root, attempt_kind=base.attempt_kind,
        poll_interval_seconds=base.poll_interval_seconds, limits=limits)


def _record_path(tmp_path, name):
    return tmp_path / "records" / name


def _entry(registered, record, *, material=False):
    payload = dict(registered.entry(material=material))
    payload["record"] = str(record)
    return payload


def _schedule(request, factory, *, run_once=None, prepare=True):
    with _gateways(factory, "scheduler-a") as gateways:
        return run_scheduler(
            request, session_factory=factory, worker_id="scheduler-a", gateways=gateways,
            heartbeat_config=_heartbeat(),
            **({"prepare": _formal_prepare(factory)} if prepare else {}),
            run_once=run_once)


def _runs(factory, task_id):
    with factory() as session:
        return session.scalars(select(Run).where(Run.task_id == task_id)).all()


def test_a_finished_attempt_writes_a_record_a_reviewer_can_read(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    registered = _Registered(tmp_path, factory, monkeypatch, "a")
    record_path = _record_path(tmp_path, "attempt-a.json")
    manifest = _manifest(tmp_path, [_entry(registered, record_path)])

    summary = _schedule(_request(tmp_path, manifest), factory,
                        run_once=_drafting_run_once(factory, registered.task_id))

    entry = summary["entries"][0]
    assert entry["outcome"] == "stopped"
    assert entry["record_written"] is True
    assert entry["record_error"] is None
    record = load_run_record(record_path)
    # Identity and provenance a reviewer needs to find the rows again.
    assert record.task_id == str(registered.task_id)
    assert record.run_id == entry["run_id"]
    assert record.attempt_kind.value == "formal"
    assert record.policy_version and record.model_id and record.operator == "operator-a"
    assert record.pack_id == "chain-pack-a"
    assert record.base_commit == registered.head
    assert record.artifacts
    # No human decision was invented for the reviewer to rubber-stamp.
    assert record.human_review.conclusion == "pending"
    assert record.human_review.reviewer is None
    # This attempt stopped at its draft: the record says so and claims nothing more.
    assert record.outcome.value == "incomplete"
    assert record.formal is None
    assert "finalization=" in record.notes


def test_a_rehearsal_is_recorded_as_a_rehearsal(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    material = _material_file(tmp_path)
    registered = _Registered(tmp_path, factory, monkeypatch, "a", material=material)
    record_path = _record_path(tmp_path, "rehearsal.json")
    manifest = _manifest(tmp_path, [_entry(registered, record_path, material=True)])

    summary = _schedule(_request(tmp_path, manifest, attempt_kind="rehearsal"), factory,
                        run_once=_drafting_run_once(factory, registered.task_id),
                        prepare=False)

    entry = summary["entries"][0]
    assert entry["record_written"] is True
    record = load_run_record(record_path)
    assert record.attempt_kind.value == "rehearsal"
    assert record.outcome.value != "formal_pass"
    assert record.formal is None
    assert "attempt_kind_not_formal" in record.notes


def test_a_failed_attempt_is_recorded_before_the_error_is_raised(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    registered = _Registered(tmp_path, factory, monkeypatch, "a")
    record_path = _record_path(tmp_path, "failed.json")
    manifest = _manifest(tmp_path, [_entry(registered, record_path)])

    @contextmanager
    def failing_gateways(session_factory, worker_id):
        def generate(_prompt, _limit):
            raise RuntimeError("provider unavailable")

        with model_gateways_from_env(session_factory, worker_id,
                                     generate=generate) as built:
            yield built

    with failing_gateways(factory, "scheduler-a") as gateways:
        summary = run_scheduler(
            _request(tmp_path, manifest), session_factory=factory,
            worker_id="scheduler-a", gateways=gateways, heartbeat_config=_heartbeat(),
            prepare=_formal_prepare(factory))

    entry = summary["entries"][0]
    assert entry["outcome"] == "failed" and entry["detail"] == "RuntimeError"
    assert entry["record_written"] is True
    record = load_run_record(record_path)
    assert record.outcome.value == "incomplete"
    assert "execution_error:RuntimeError" in record.notes
    assert record.formal is None


def test_a_timeout_is_recorded_as_a_timeout(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    registered = _Registered(tmp_path, factory, monkeypatch, "a")
    record_path = _record_path(tmp_path, "timeout.json")
    manifest = _manifest(tmp_path, [_entry(registered, record_path)])

    def run_once(request, **kwargs):
        def drive(_directories, _stop):
            sleep(1.1)  # the attempt's one-second budget expires inside the drive

        return run_worker_once(request, drive=drive, **kwargs)

    summary = _schedule(
        _request_with_limits(tmp_path, manifest,
                             BenchmarkLimits(max_duration_seconds=1)),
        factory, run_once=run_once)

    entry = summary["entries"][0]
    assert entry["record_written"] is True
    record = load_run_record(record_path)
    assert record.outcome.value == "timeout"
    assert "deadline_fired=True" in record.notes


def test_an_existing_record_path_is_refused_before_the_run(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    registered = _Registered(tmp_path, factory, monkeypatch, "a")
    record_path = _record_path(tmp_path, "taken.json")
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text("earlier evidence\n", encoding="utf-8")
    manifest = _manifest(tmp_path, [_entry(registered, record_path)])

    summary = _schedule(_request(tmp_path, manifest), factory,
                        run_once=_drafting_run_once(factory, registered.task_id))

    entry = summary["entries"][0]
    assert entry["outcome"] == "refused"
    assert "already exists" in entry["detail"]
    assert _runs(factory, registered.task_id) == []
    assert record_path.read_text(encoding="utf-8") == "earlier evidence\n"


def test_a_replay_writes_no_second_record(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    registered = _Registered(tmp_path, factory, monkeypatch, "a")
    record_path = _record_path(tmp_path, "attempt-a.json")
    manifest = _manifest(tmp_path, [_entry(registered, record_path)])
    request = _request(tmp_path, manifest)

    first = _schedule(request, factory,
                      run_once=_drafting_run_once(factory, registered.task_id))
    original = record_path.read_bytes()
    second = _schedule(request, factory)

    assert first["entries"][0]["record_written"] is True
    entry = second["entries"][0]
    assert entry["outcome"] == "existing_run"
    assert entry["record_written"] is False and entry["record_path"] is None
    assert record_path.read_bytes() == original
    assert len(_runs(factory, registered.task_id)) == 1


def test_two_entries_keep_two_records(
    worker_session_factory, tmp_path, monkeypatch,
):
    factory = worker_session_factory
    first = _Registered(tmp_path, factory, monkeypatch, "a")
    second = _Registered(tmp_path, factory, monkeypatch, "b")
    first_record = _record_path(tmp_path, "first.json")
    second_record = _record_path(tmp_path, "second.json")
    manifest = _manifest(tmp_path, [_entry(first, first_record),
                                    _entry(second, second_record)])

    def run_once(request, **kwargs):
        task_id = request.task_id

        def drive(_directories, _stop):
            _write_draft(factory, task_id)

        return run_worker_once(request, drive=drive, **kwargs)

    summary = _schedule(_request(tmp_path, manifest), factory, run_once=run_once)

    assert summary["writes_record"] is True
    assert [entry["record_written"] for entry in summary["entries"]] == [True, True]
    assert load_run_record(first_record).task_id == str(first.task_id)
    # Two attempts, two files: neither record was folded into or written over the other.
    assert load_run_record(second_record).task_id == str(second.task_id)


def test_the_manifest_refuses_a_shared_record_path(tmp_path):
    task_a, task_b = uuid_pair()
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"schema_version": 1, "tasks": [
        {"task_id": task_a, "pack": "p", "policy": "q", "record": "records/one.json"},
        {"task_id": task_b, "pack": "p", "policy": "q", "record": "records/one.json"},
    ]}), encoding="utf-8")

    with pytest.raises(SchedulerManifestError, match="same record path"):
        load_scheduler_manifest(path)


def uuid_pair():
    from uuid import uuid4
    return str(uuid4()), str(uuid4())


def test_a_human_review_of_the_generated_record_leaves_it_untouched(
    worker_session_factory, tmp_path, monkeypatch, capsys,
):
    """The generated record is what a reviewer reads; the review is a separate copy."""
    factory = worker_session_factory
    registered = _Registered(tmp_path, factory, monkeypatch, "a")
    record_path = _record_path(tmp_path, "attempt-a.json")
    manifest = _manifest(tmp_path, [_entry(registered, record_path)])
    _schedule(_request(tmp_path, manifest), factory,
              run_once=_drafting_run_once(factory, registered.task_id))
    original = record_path.read_bytes()
    reviewed_path = tmp_path / "records" / "attempt-a.reviewed.json"

    exit_code = benchmark_record_module.main([
        "review", "--record", str(record_path), "--out", str(reviewed_path),
        "--conclusion", "accepted", "--reviewer", "reviewer-a",
        "--at", "2026-11-16T00:00:00+00:00"])

    assert exit_code == 0
    assert record_path.read_bytes() == original  # the evidence is never rewritten
    reviewed = load_run_record(reviewed_path)
    assert reviewed.human_review.conclusion == "accepted"
    assert reviewed.human_review.reviewer == "reviewer-a"
    assert reviewed.run_id == load_run_record(record_path).run_id
    capsys.readouterr()
