"""可回退版本的最小验证：Task 记录的版本能在一份旧工件副本上重新核验。

Rollback, as the project uses the word, is not a hot switch: an **older, still verifiable**
artifact is chosen for a **new** Task, while an existing Task keeps the version its Runs
recorded. What is checked here is the read-only half of that: a Task's recorded digest must be
unambiguous, and the artifact copy the operator points at must still match it. Unknown, mixed,
changed, missing and "a different manifest" are all refusals — nothing falls back to whatever
artifact happens to be around.

The manifests point at temporary directories, not at an installed package, so this does not
prove that any process ran those bytes; it proves the bytes at that directory match the manifest
whose digest the Task recorded. Entries still verify the *running* package and record the digest
themselves, and the workspace and digest are still not a sandbox.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from devflow_ai.coordinator import RunCoordinator
from devflow_ai.enums import RunStatus, TaskStatus
from devflow_ai.execution_binding import ExecutionBindingError, verify_task_version
from devflow_ai.execution_manifest import build_manifest, manifest_sha256
from devflow_ai.models import Run
from devflow_ai.services import AdmissionRecord, TaskService


def _package(tmp_path, name, *, body="VALUE = 1\n"):
    """One artifact copy plus the manifest and digest a control plane would pin."""
    root = tmp_path / name
    root.mkdir()
    (root / "module.py").write_text(body, encoding="utf-8")
    manifest = build_manifest(root, ["module.py"])
    path = tmp_path / f"{name}-manifest.json"
    path.write_text(manifest.model_dump_json(), encoding="utf-8")
    return root, path, manifest_sha256(manifest)


def _task(factory):
    with factory() as session:
        task, _ = TaskService(session).create_task(
            title="Rollback task", description="Constructed fixture", repo="example-repo",
            idempotency_key=uuid4())
        task_id = task.id
    with factory() as session:
        TaskService(session).apply_admission_decision(
            task_id, TaskStatus.ELIGIBLE, base_commit="a" * 40,
            admission_record=AdmissionRecord(reason="fixture", evidence=["fixture"]))
    return task_id


def _claim(factory, task_id, digest, *, worker_id="rollback-worker"):
    with factory() as session:
        run, _ = RunCoordinator(session).start_initial_run(
            task_id, worker_id, f"key-{uuid4()}", execution_sha256=digest)
        return run.id


def _run_digests(factory, task_id):
    with factory() as session:
        return [run.execution_sha256 for run in session.scalars(
            select(Run).where(Run.task_id == task_id).order_by(Run.attempt_number)).all()]


def test_the_recorded_version_can_be_reverified_from_an_artifact_copy(
        worker_session_factory, tmp_path):
    factory = worker_session_factory
    copy, manifest, digest = _package(tmp_path, "v0")
    task_id = _task(factory)
    _claim(factory, task_id, digest)

    with factory() as session:
        binding = verify_task_version(
            session, task_id, manifest_path=manifest, artifact_root=copy)

    assert binding.sha256 == digest and binding.file_count == 1
    assert binding.root == str(copy.resolve())


def test_a_task_without_a_recorded_version_cannot_be_selected(
        worker_session_factory, tmp_path):
    factory = worker_session_factory
    copy, manifest, _digest = _package(tmp_path, "v0")
    never_run = _task(factory)
    unknown = _task(factory)
    _claim(factory, unknown, None)

    with factory() as session, pytest.raises(ExecutionBindingError, match="unknown version"):
        verify_task_version(session, never_run, manifest_path=manifest, artifact_root=copy)
    with factory() as session, pytest.raises(ExecutionBindingError, match="unknown version"):
        verify_task_version(session, unknown, manifest_path=manifest, artifact_root=copy)


def test_a_task_whose_history_records_two_versions_is_ambiguous(
        worker_session_factory, tmp_path):
    factory = worker_session_factory
    copy, manifest, first = _package(tmp_path, "v0")
    _second_copy, _second_manifest, second = _package(tmp_path, "v1", body="VALUE = 2\n")
    task_id = _task(factory)
    _claim(factory, task_id, first)
    # One extra attempt row, constructed directly: the claim path hands out one Run per Task and
    # this Rule must hold however a second row came to exist.
    with factory.begin() as session:
        session.add(Run(task_id=task_id, attempt_number=2, status=RunStatus.FAILED,
                        creation_idempotency_key=f"fixture-{uuid4()}",
                        worker_id="fixture-worker", execution_sha256=second,
                        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                        finished_at=datetime.now(timezone.utc)))

    with factory() as session, pytest.raises(ExecutionBindingError, match="ambiguous"):
        verify_task_version(session, task_id, manifest_path=manifest, artifact_root=copy)


def test_a_known_version_does_not_hide_an_unknown_run(worker_session_factory, tmp_path):
    factory = worker_session_factory
    copy, manifest, digest = _package(tmp_path, "v0")
    task_id = _task(factory)
    _claim(factory, task_id, digest)
    with factory.begin() as session:
        session.add(Run(task_id=task_id, attempt_number=2, status=RunStatus.FAILED,
                        creation_idempotency_key=f"fixture-{uuid4()}",
                        worker_id="fixture-worker", execution_sha256=None,
                        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                        finished_at=datetime.now(timezone.utc)))

    with factory() as session, pytest.raises(ExecutionBindingError, match="unknown.*ambiguous"):
        verify_task_version(session, task_id, manifest_path=manifest, artifact_root=copy)


def test_changed_missing_or_another_version_is_refused(worker_session_factory, tmp_path):
    factory = worker_session_factory
    copy, manifest, digest = _package(tmp_path, "v0")
    other_copy, other_manifest, other_digest = _package(tmp_path, "v1", body="VALUE = 2\n")
    task_id = _task(factory)
    _claim(factory, task_id, digest)
    assert digest != other_digest

    # A different but perfectly valid manifest is not this Task's version.
    with factory() as session, pytest.raises(ExecutionBindingError, match="pinned digest"):
        verify_task_version(session, task_id, manifest_path=other_manifest,
                            artifact_root=other_copy)
    # Changed bytes under the recorded manifest.
    (copy / "module.py").write_text("VALUE = 3\n", encoding="utf-8")
    with factory() as session, pytest.raises(ExecutionBindingError, match="digest does not match"):
        verify_task_version(session, task_id, manifest_path=manifest, artifact_root=copy)
    # A listed file that is gone.
    (copy / "module.py").unlink()
    with factory() as session, pytest.raises(ExecutionBindingError,
                                             match="cannot be read|missing"):
        verify_task_version(session, task_id, manifest_path=manifest, artifact_root=copy)


def test_a_new_task_may_select_an_older_version_and_an_existing_one_may_not_switch(
        worker_session_factory, tmp_path):
    factory = worker_session_factory
    v0_copy, v0_manifest, v0 = _package(tmp_path, "v0")
    v1_copy, v1_manifest, v1 = _package(tmp_path, "v1", body="VALUE = 2\n")
    old_task = _task(factory)
    candidate_task = _task(factory)
    _claim(factory, old_task, v0)
    _claim(factory, candidate_task, v1)

    # Explicit rollback: a *new* Task re-verifies the older artifact and selects that digest.
    rollback_task = _task(factory)
    with factory() as session:
        binding = verify_task_version(
            session, old_task, manifest_path=v0_manifest, artifact_root=v0_copy)
    assert binding.sha256 == v0
    _claim(factory, rollback_task, binding.sha256)

    assert _run_digests(factory, old_task) == [v0]
    assert _run_digests(factory, candidate_task) == [v1]
    assert _run_digests(factory, rollback_task) == [v0]

    # The same preflight will not hand an existing Task a different version's artifact, and the
    # candidate's own version stays untouched by the rollback.
    with factory() as session, pytest.raises(ExecutionBindingError, match="pinned digest"):
        verify_task_version(session, old_task, manifest_path=v1_manifest, artifact_root=v1_copy)
    with factory() as session:
        assert verify_task_version(
            session, candidate_task, manifest_path=v1_manifest, artifact_root=v1_copy
        ).sha256 == v1
