"""Explicit memory ingestion against real PostgreSQL rows in an isolated schema.

Every source here is a constructed formal record (Task, Run, Verification, Task review,
Artifact) written into this test's own schema: they exist so the admission rules can be
exercised, and they are not evidence that a real task passed. What is real in these tests is
the database: the rows the ingestion path reads, the constraints it relies on, and the
concurrent writers it serialises against.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from threading import Barrier, Event
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError

from devflow_ai.enums import (
    AgentExecutionStatus, AgentRole, RunStatus, TaskStatus, VerificationResult,
)
from devflow_ai.benchmark_task import create_benchmark_task
from devflow_ai.context import ContextBuilder
from devflow_ai.memory import (
    MemoryCandidate, MemoryConflictError, MemoryError, MemoryEvidenceError, MemoryRecallRequest, MemoryScopeError,
    MemoryService, MemoryStateError, RepositoryScope, main as memory_main,
)
from devflow_ai.models import (
    AgentExecution, Artifact, MemoryConflict, MemoryItem, MemoryReview, MemoryRevision, MemoryUse, Run, Task, TaskReview, Verification,
)
from devflow_ai.policy import build_default_policy
from devflow_ai.services import AdmissionRecord, TaskService
from devflow_ai.task_pack import load_manifest
from devflow_ai.worker import Worker

REVISIONS = ("c7e1a29b4d63", "d4f63c192ba8", "e8c27b14a693", "62eae84cebd1",
             "97f3a25cb190", "a43cb50f67e9", "f154b6e82639", "f83ab2d16c49",
             "ab6e1c30f42d", "4b79c6e21a50", "b7f2c31d8a90", "c4a7e2f19b30",
             "d18e42f70a91", "ec624a31d740", "f6a92c3184d2", "a5810e46f2c9", "b41e9d2c8370")


@pytest.fixture
def memory_session_factory(run_session_factory):
    """Add the verification, review and memory tables inside this test's isolated schema."""
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


def _passed_run(factory, *, repo="example-repo", run_status=RunStatus.PASSED,
                result=VerificationResult.PASSED, scope="trusted_v1",
                suite_version="suite-1", task_status=TaskStatus.AWAITING_REVIEW):
    """One Task whose Run passed and whose trusted Verification is recorded."""
    with factory() as session:
        task, _ = TaskService(session).create_task(
            title="Memory source", description="Support one memory", repo=repo,
            idempotency_key=uuid4())
        task_id = task.id
    with factory() as session:
        TaskService(session).apply_admission_decision(
            task_id, TaskStatus.ELIGIBLE, base_commit="a" * 40,
            admission_record=AdmissionRecord(reason="fixture", evidence=["fixture"]))
    worker = Worker(factory, f"memory-worker-{uuid4().hex[:8]}")
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
    return {"task_id": task_id, "run_id": run_id, "verification_id": verification_id,
            "repo": repo}


def _candidate(fixture, **overrides):
    arguments = {
        "identity_key": "memory-one",
        "scope": RepositoryScope(repository=fixture["repo"]),
        "source_kind": "verification",
        "task_id": fixture["task_id"],
        "run_id": fixture["run_id"],
        "verification_id": fixture["verification_id"],
        "content": "An empty prompt suffix must still be honoured exactly.",
        "created_by": "operator-a",
        "applicable_base_commit": "a" * 40,
        "applicable_environment": "fixture-python-3.11",
    }
    arguments.update(overrides)
    return MemoryCandidate(**arguments)


def _running_target(factory, repo="example-repo"):
    with factory() as session:
        task, _ = TaskService(session).create_task(title="Target", description="Development task", repo=repo,
                                                   idempotency_key=uuid4())
        task_id = task.id
    with factory() as session:
        TaskService(session).apply_admission_decision(task_id, TaskStatus.ELIGIBLE, base_commit="a" * 40,
            admission_record=AdmissionRecord(reason="fixture", evidence=["fixture"]))
    run_id = Worker(factory, f"target-{uuid4().hex[:8]}").claim_once().run_id
    return task_id, run_id


def _ingest(factory, candidate):
    with factory() as session:
        return MemoryService(session).ingest(candidate)


def _read(factory, *, repository, memory_id=None, identity=None):
    with factory() as session:
        return MemoryService(session).read(
            scope=RepositoryScope(repository=repository), memory_id=memory_id,
            identity_key=identity)


def test_known_benchmark_source_cannot_be_ingested_or_replayed(memory_session_factory):
    factory = memory_session_factory
    source = _passed_run(factory)
    candidate = _candidate(source, identity_key="benchmark-source")
    inserted = _ingest(factory, candidate)
    with factory.begin() as session:
        session.get(Task, source["task_id"]).admission_record = {
            "reason": "constructed benchmark", "evidence": ["pack:fixture-pack"]}
    with pytest.raises(MemoryScopeError, match="benchmark"):
        _ingest(factory, candidate)
    with pytest.raises(MemoryScopeError, match="benchmark"):
        _ingest(factory, _candidate(source, identity_key="another"))
    with factory() as session:
        rows = session.scalars(select(MemoryItem)).all()
        assert len(rows) == 1 and str(rows[0].id) == inserted["id"]


def test_ingestion_waits_for_source_benchmark_classification(memory_session_factory):
    factory = memory_session_factory
    source = _passed_run(factory)
    locked, release = Event(), Event()
    writer_pid = []

    def classify():
        with factory.begin() as session:
            task = session.scalar(select(Task).where(Task.id == source["task_id"]).with_for_update())
            task.admission_record = {"reason": "constructed", "evidence": ["pack:fixture-pack"]}
            session.flush()
            writer_pid.append(session.scalar(select(func.pg_backend_pid())))
            locked.set()
            assert release.wait(15)

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(classify)
        try:
            assert locked.wait(10)
            reader = pool.submit(_ingest, factory, _candidate(source))
            deadline = monotonic() + 10
            blocked = False
            while monotonic() < deadline and not reader.done():
                with factory() as session:
                    blocked = session.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE :writer = ANY(pg_blocking_pids(pid)))"), {"writer": writer_pid[0]})
                if blocked:
                    break
                sleep(0.02)
            assert blocked, "ingestion did not wait for the source Task lock"
        finally:
            release.set()
        writer.result(timeout=15)
        with pytest.raises(MemoryScopeError, match="benchmark"):
            reader.result(timeout=15)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(MemoryItem)) == 0


def test_reclassified_benchmark_source_is_excluded_from_preview_and_use(memory_session_factory):
    factory = memory_session_factory
    source = _passed_run(factory)
    target_id, target_run = _running_target(factory)
    item = _ingest(factory, _candidate(source, topic="diagnostics",
        valid_until=datetime.now(timezone.utc) + timedelta(days=1)))
    memory_id = UUID(item["id"])
    shown = _read(factory, repository="example-repo", memory_id=memory_id)
    with factory() as session:
        MemoryService(session).review(memory_id=memory_id, scope=RepositoryScope("example-repo"),
            candidate_sha256=shown["candidate_sha256"], conclusion="accepted", reviewer="person", reason="reviewed")
    use_key = uuid4()
    def declare(key):
        with factory() as session:
            return MemoryService(session).record_use(idempotency_key=key, scope=RepositoryScope("example-repo"),
                task_id=target_id, run_id=target_run, memory_id=memory_id,
                candidate_sha256=shown["candidate_sha256"], operator="person", purpose="diagnosis",
                target_environment="fixture-python-3.11")
    old_use = declare(use_key)
    with factory.begin() as session:
        session.get(Task, source["task_id"]).admission_record = {
            "reason": "constructed benchmark", "evidence": ["pack:fixture-pack"]}
    with factory() as session:
        assert MemoryService(session).inspect_candidates(scope=RepositoryScope("example-repo"),
            target_task_id=target_id, topic="diagnostics") == []
    for key in (use_key, uuid4()):
        with pytest.raises(MemoryScopeError, match="benchmark"):
            declare(key)
    with factory() as session:
        assert MemoryService(session).read_use(scope=RepositoryScope("example-repo"),
            use_id=UUID(old_use["id"])) is not None


@pytest.mark.parametrize("operation", ["preview", "use"])
def test_existing_candidate_waits_for_source_classification(memory_session_factory, operation):
    factory = memory_session_factory
    source = _passed_run(factory)
    target_id, target_run = _running_target(factory)
    item = _ingest(factory, _candidate(source, topic="diagnostics",
        valid_until=datetime.now(timezone.utc) + timedelta(days=1)))
    locked, release = Event(), Event()
    writer_pid = []
    def classify():
        with factory.begin() as session:
            task = session.scalar(select(Task).where(Task.id == source["task_id"]).with_for_update())
            task.admission_record = {"reason": "constructed", "evidence": ["pack:fixture-pack"]}
            session.flush()
            writer_pid.append(session.scalar(select(func.pg_backend_pid())))
            locked.set()
            assert release.wait(15)
    def attempt():
        with factory() as session:
            service = MemoryService(session)
            if operation == "preview":
                return service.inspect_candidates(scope=RepositoryScope("example-repo"),
                    target_task_id=target_id, topic="diagnostics")
            return service.record_use(idempotency_key=uuid4(), scope=RepositoryScope("example-repo"),
                task_id=target_id, run_id=target_run, memory_id=UUID(item["id"]),
                candidate_sha256="a" * 64, operator="person", purpose="diagnosis",
                target_environment="fixture-python-3.11")
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(classify)
        try:
            assert locked.wait(10)
            reader = pool.submit(attempt)
            deadline = monotonic() + 10
            blocked = False
            while monotonic() < deadline and not reader.done():
                with factory() as session:
                    blocked = session.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE :writer = ANY(pg_blocking_pids(pid)))"), {"writer": writer_pid[0]})
                if blocked:
                    break
                sleep(0.02)
            assert blocked, "operation did not wait for the source Task lock"
        finally:
            release.set()
        writer.result(timeout=15)
        if operation == "preview":
            assert reader.result(timeout=15) == []
        else:
            with pytest.raises(MemoryScopeError, match="benchmark"):
                reader.result(timeout=15)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(MemoryUse)) == 0


def test_use_and_run_finalization_do_not_deadlock(memory_session_factory):
    factory = memory_session_factory
    source = _passed_run(factory)
    target_id, run_id = _running_target(factory)
    item = _ingest(factory, _candidate(source, topic="diagnostics",
        valid_until=datetime.now(timezone.utc) + timedelta(days=1)))
    memory_id = UUID(item["id"])
    shown = _read(factory, repository="example-repo", memory_id=memory_id)
    with factory() as session:
        MemoryService(session).review(memory_id=memory_id, scope=RepositoryScope("example-repo"),
            candidate_sha256=shown["candidate_sha256"], conclusion="accepted", reviewer="person", reason="reviewed")
    locked, finish = Event(), Event()
    writer_pid = []
    def finalize():
        with factory() as session, session.begin():
            run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            writer_pid.append(session.scalar(select(func.pg_backend_pid())))
            locked.set()
            assert finish.wait(15)
            session.get(Task, target_id).status = TaskStatus.AWAITING_REVIEW
            run.status = RunStatus.PASSED
            session.flush()
    def declare():
        with factory() as session:
            return MemoryService(session).record_use(idempotency_key=uuid4(), scope=RepositoryScope("example-repo"),
                task_id=target_id, run_id=run_id, memory_id=memory_id, candidate_sha256=shown["candidate_sha256"],
                operator="person", purpose="diagnosis", target_environment="fixture-python-3.11")
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(finalize)
        try:
            assert locked.wait(10)
            reader = pool.submit(declare)
            deadline = monotonic() + 10
            blocked = False
            while monotonic() < deadline and not reader.done():
                with factory() as session:
                    blocked = session.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE :writer = ANY(pg_blocking_pids(pid)))"), {"writer": writer_pid[0]})
                if blocked:
                    break
                sleep(0.02)
            assert blocked, "declaration did not wait for the Run lock"
        finally:
            finish.set()
        writer.result(timeout=15)
        with pytest.raises(MemoryStateError, match="running Run"):
            reader.result(timeout=15)
    with factory() as session:
        assert session.get(Run, run_id).status is RunStatus.PASSED
        assert session.scalar(select(func.count()).select_from(MemoryUse)) == 0


def test_use_refuses_busy_task_without_deadlocking_review_order(memory_session_factory):
    factory = memory_session_factory
    source = _passed_run(factory)
    target_id, run_id = _running_target(factory)
    item = _ingest(factory, _candidate(source, topic="diagnostics",
        valid_until=datetime.now(timezone.utc) + timedelta(days=1)))
    memory_id = UUID(item["id"])
    shown = _read(factory, repository="example-repo", memory_id=memory_id)
    locked, proceed = Event(), Event()
    writer_pid = []
    def review_order():
        with factory() as session, session.begin():
            session.scalar(select(Task).where(Task.id == target_id).with_for_update())
            writer_pid.append(session.scalar(select(func.pg_backend_pid())))
            locked.set()
            assert proceed.wait(15)
            session.scalar(select(Run).where(Run.id == run_id).with_for_update())
    def declare():
        with factory() as session:
            return MemoryService(session).record_use(idempotency_key=uuid4(), scope=RepositoryScope("example-repo"),
                task_id=target_id, run_id=run_id, memory_id=memory_id, candidate_sha256=shown["candidate_sha256"],
                operator="person", purpose="diagnosis", target_environment="fixture-python-3.11")
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(review_order)
        try:
            assert locked.wait(10)
            reader = pool.submit(declare)
            deadline = monotonic() + 10
            blocked = False
            while monotonic() < deadline and not reader.done():
                with factory() as session:
                    blocked = session.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE :writer = ANY(pg_blocking_pids(pid)))"), {"writer": writer_pid[0]})
                if blocked:
                    break
                sleep(0.02)
            assert reader.done() or blocked, "declaration did not reach the Task lock"
        finally:
            proceed.set()
        writer.result(timeout=15)
        with pytest.raises(MemoryStateError, match="busy; retry"):
            reader.result(timeout=15)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(MemoryUse)) == 0


def test_operator_preview_filters_scope_expiry_retirement_and_preserves_same_topic(memory_session_factory):
    factory = memory_session_factory
    source = _passed_run(factory)
    target = _passed_run(factory)
    other = _passed_run(factory, repo="other-repo")
    future = datetime.now(timezone.utc) + timedelta(days=1)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    for identity, fixture, expiry, content in (
        ("a", source, future, "one"), ("b", source, future, "contradiction"),
        ("expired", source, past, "expired"), ("own", target, future, "same task"),
        ("outside", other, future, "other repo"),
    ):
        _ingest(factory, _candidate(fixture, identity_key=identity, topic="diagnostics",
                                    valid_until=expiry, content=content))
    retired = _ingest(factory, _candidate(source, identity_key="retired", topic="diagnostics",
                                          valid_until=future))
    with factory() as session:
        MemoryService(session).retire(UUID(retired["id"]), scope=RepositoryScope("example-repo"),
                                      operator="operator", reason="obsolete")
    with factory() as session:
        items = MemoryService(session).inspect_candidates(
            scope=RepositoryScope("example-repo"), target_task_id=target["task_id"],
            topic="diagnostics")
    assert {item["identity_key"] for item in items} == {"a", "b"}
    assert all(item["content_status"] == "unverified_candidate" for item in items)
    assert all(item["candidate_checks"]["review_state"] == "missing" and
               not item["candidate_checks"]["eligible_to_request_use"] for item in items)
    with factory() as session:
        with pytest.raises(MemoryError, match="exceeds preview limit"):
            MemoryService(session).inspect_candidates(scope=RepositoryScope("example-repo"),
                target_task_id=target["task_id"], topic="diagnostics", limit=1)
    with factory() as session:
        with pytest.raises(MemoryScopeError):
            MemoryService(session).inspect_candidates(scope=RepositoryScope("other-repo"),
                target_task_id=target["task_id"], topic="diagnostics")


@pytest.mark.parametrize("kind", ["conflict", "revision", "review"])
def test_preview_waits_for_inflight_metadata_declaration(memory_session_factory, kind):
    factory = memory_session_factory
    source, target = _passed_run(factory), _passed_run(factory)
    expiry = datetime.now(timezone.utc) + timedelta(days=1)
    first = _ingest(factory, _candidate(source, identity_key="first", topic="checks", valid_until=expiry))
    second = _ingest(factory, _candidate(source, identity_key="second", topic="checks", valid_until=expiry))
    ids = sorted((UUID(first["id"]), UUID(second["id"])), key=lambda value: value.int)
    digests = {mid: _read(factory, repository="example-repo", memory_id=mid)["candidate_sha256"] for mid in ids}
    locked, release = Event(), Event()
    writer_pid, conflict_ids = [], []
    def writer():
        with factory() as session:
            def pause_before_commit(current):
                writer_pid.append(current.scalar(select(func.pg_backend_pid())))
                model = {"conflict": MemoryConflict, "revision": MemoryRevision, "review": MemoryReview}[kind]
                conflict_ids.append(str(current.scalar(select(model.id))))
                locked.set()
                assert release.wait(15)
            event.listen(session, "before_commit", pause_before_commit, once=True)
            if kind == "conflict":
                MemoryService(session).declare_conflict(scope=RepositoryScope("example-repo"),
                    first_id=ids[0], second_id=ids[1], first_sha256=digests[ids[0]],
                    second_sha256=digests[ids[1]], reporter="operator", reason="inconsistent")
            elif kind == "revision":
                previous, successor = UUID(first["id"]), UUID(second["id"])
                MemoryService(session).declare_revision(scope=RepositoryScope("example-repo"),
                    previous_id=previous, next_id=successor, previous_sha256=digests[previous],
                    next_sha256=digests[successor], operator="operator", reason="update")
            else:
                mid = UUID(first["id"])
                MemoryService(session).review(memory_id=mid, scope=RepositoryScope("example-repo"),
                    candidate_sha256=digests[mid], conclusion="accepted", reviewer="operator", reason="reviewed")
    def preview():
        with factory() as session:
            return MemoryService(session).inspect_candidates(scope=RepositoryScope("example-repo"),
                target_task_id=target["task_id"], topic="checks")
    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(writer)
        try:
            assert locked.wait(10)
            reading = pool.submit(preview)
            deadline = monotonic() + 10
            blocked = False
            while monotonic() < deadline and not reading.done():
                with factory() as session:
                    blocked = session.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE :writer = ANY(pg_blocking_pids(pid)))"), {"writer": writer_pid[0]})
                if blocked:
                    break
                sleep(0.02)
            assert blocked, "preview did not wait for the in-flight metadata declaration"
        finally:
            release.set()
        writing.result(timeout=15)
        result = reading.result(timeout=15)
    assert len(result) == 2
    if kind == "review":
        reviewed = next(row for row in result if row["id"] == first["id"])
        assert reviewed["candidate_checks"]["review_state"] == "accepted"
        assert reviewed["candidate_checks"]["review_id"] == conflict_ids[0]
    else:
        key = "open_conflict_ids" if kind == "conflict" else "revision_ids"
        assert all(row[key] == conflict_ids for row in result)


def test_preview_excludes_candidate_expiring_while_waiting_for_lock(memory_session_factory):
    factory = memory_session_factory
    source, target = _passed_run(factory), _passed_run(factory)
    expiry = datetime.now(timezone.utc) + timedelta(seconds=4)
    item = _ingest(factory, _candidate(source, topic="expiring", valid_until=expiry))
    locked, release = Event(), Event()
    writer_pid = []

    def writer():
        with factory.begin() as session:
            session.scalar(select(MemoryItem).where(MemoryItem.id == UUID(item["id"])).with_for_update())
            writer_pid.append(session.scalar(select(func.pg_backend_pid())))
            locked.set()
            assert release.wait(15)

    def preview():
        with factory() as session:
            return MemoryService(session).inspect_candidates(scope=RepositoryScope("example-repo"),
                target_task_id=target["task_id"], topic="expiring")

    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(writer)
        try:
            assert locked.wait(10)
            reading = pool.submit(preview)
            deadline = monotonic() + 10
            blocked = False
            while monotonic() < deadline and not reading.done():
                with factory() as session:
                    blocked = session.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE :writer = ANY(pg_blocking_pids(pid)))"), {"writer": writer_pid[0]})
                if blocked:
                    break
                sleep(0.02)
            assert blocked, "preview did not wait for the candidate lock"
            with factory() as session:
                session.execute(text("SELECT pg_sleep(GREATEST(0, EXTRACT(EPOCH FROM "
                    "(:expiry - clock_timestamp())) + 0.05))"), {"expiry": expiry})
        finally:
            release.set()
        writing.result(timeout=15)
        assert reading.result(timeout=15) == []


def test_preview_metadata_is_paired_and_idempotency_checked(memory_session_factory):
    factory = memory_session_factory
    source = _passed_run(factory)
    future = datetime.now(timezone.utc) + timedelta(days=1)
    with pytest.raises(MemoryError, match="provided together"):
        _ingest(factory, _candidate(source, topic="diagnostics"))
    with pytest.raises(MemoryError, match="timezone-aware"):
        _ingest(factory, _candidate(source, topic="diagnostics", valid_until=datetime.now()))
    _ingest(factory, _candidate(source, topic="diagnostics", valid_until=future))
    with pytest.raises(MemoryConflictError, match="topic"):
        _ingest(factory, _candidate(source, topic="other", valid_until=future))
    with pytest.raises(MemoryConflictError, match="applicable_environment"):
        _ingest(factory, _candidate(source, topic="diagnostics", valid_until=future,
                                    applicable_environment="other"))
    with pytest.raises(MemoryError, match="provided together"):
        _ingest(factory, _candidate(source, identity_key="unpaired", applicable_environment=None))
    with pytest.raises(MemoryError, match="full lowercase commit"):
        _ingest(factory, _candidate(source, identity_key="bad-commit", applicable_base_commit="A" * 40))
    with pytest.raises(MemoryError, match="Environment label"):
        _ingest(factory, _candidate(source, identity_key="bad-environment", applicable_environment=" bad "))


def test_prepare_recall_returns_candidate_only_with_new_committed_declaration(memory_session_factory):
    factory = memory_session_factory
    source = _passed_run(factory)
    target_id, target_run = _running_target(factory)
    item = _ingest(factory, _candidate(source, topic="diagnostics",
        valid_until=datetime.now(timezone.utc) + timedelta(days=1)))
    memory_id = UUID(item["id"])
    shown = _read(factory, repository="example-repo", memory_id=memory_id)
    with factory() as session:
        MemoryService(session).review(memory_id=memory_id, scope=RepositoryScope("example-repo"),
            candidate_sha256=shown["candidate_sha256"], conclusion="accepted", reviewer="person", reason="reviewed")
    key = uuid4()
    args = dict(idempotency_key=key, scope=RepositoryScope("example-repo"), task_id=target_id,
                run_id=target_run, memory_id=memory_id, candidate_sha256=shown["candidate_sha256"],
                operator="person", purpose="diagnosis", target_environment="fixture-python-3.11")
    with factory() as session:
        result = MemoryService(session).prepare_recall(**args)
    assert result["candidate"]["content"] == shown["content"]
    assert result["candidate"]["content_status"] == "unverified_candidate"
    assert result["candidate"]["candidate_sha256"] == result["use"]["candidate_sha256"]
    assert result["use"]["kind"] == "operator_declared"
    with factory() as session:
        assert session.get(MemoryUse, UUID(result["use"]["id"])) is not None
    from devflow_ai.coordinator import RunCoordinator
    with factory() as session, session.begin():
        # An operator declaration alone does not mean the Agent received the candidate.
        RunCoordinator(session).assert_independent_memory_isolation(target_run)
    with factory() as session:
        with pytest.raises(MemoryStateError, match="historical declaration"):
            MemoryService(session).prepare_recall(**args)
    with factory() as session:
        MemoryService(session).retire(memory_id, scope=RepositoryScope("example-repo"),
                                      operator="person", reason="withdrawn")
    with factory() as session:
        with pytest.raises(MemoryStateError):
            MemoryService(session).prepare_recall(**{**args, "idempotency_key": uuid4()})


def test_explicit_recall_reaches_explorer_but_disables_independent_evaluation(memory_session_factory, tmp_path, monkeypatch):
    from devflow_ai.context import MEMORY_RECALL_CONTEXT_KEY
    from devflow_ai.coordinator import ModelTokenUsage, RunCoordinator
    from devflow_ai.model_gateway import ModelGateway, ModelReply
    from devflow_ai.verification_config import VerificationConfig
    from devflow_ai.verifier import VerificationEvidenceError
    from devflow_ai.worker import RunDirectories

    factory = memory_session_factory
    source = _passed_run(factory)
    target_id, run_id = _running_target(factory)
    item = _ingest(factory, _candidate(source, topic="diagnostics",
        content="Look at the fixture readme before deciding.", valid_until=datetime.now(timezone.utc) + timedelta(days=1)))
    memory_id = UUID(item["id"])
    shown = _read(factory, repository="example-repo", memory_id=memory_id)
    with factory() as session:
        MemoryService(session).review(memory_id=memory_id, scope=RepositoryScope("example-repo"),
            candidate_sha256=shown["candidate_sha256"], conclusion="accepted", reviewer="person", reason="reviewed")
    with factory.begin() as session:
        task = session.get(Task, target_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {**snapshot, "permissions": {"allowed_tools_by_role": {
            **snapshot["permissions"]["allowed_tools_by_role"], "EXPLORER": ["read_file"]}}}
        worker_id = session.get(Run, run_id).worker_id
    workspace, artifacts = tmp_path / "workspace", tmp_path / "artifacts"
    workspace.mkdir()
    artifacts.mkdir()
    (workspace / "README.md").write_text("fixture code evidence\n", encoding="utf-8")
    turns = []
    def generate(prompt, _limit):
        assert "unverified past experience" in prompt
        data = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
        context = data if not turns else data["agent_input"]
        turns.append(context)
        if len(turns) == 1:
            result = {"path": "README.md"}
        else:
            result = {"summary": "The readme contains fixture code evidence.",
                      "read_evidence": {"path": "README.md", "tool_call_id": data["read"]["tool_call_id"]}}
        return ModelReply(json.dumps(result), ModelTokenUsage(input_tokens=10, output_tokens=5))
    worker = Worker(factory, worker_id)
    gateway = ModelGateway(session_factory=factory, worker_id=worker_id,
        estimate_input_tokens=lambda _: 100, generate=generate, max_output_tokens=200)
    recall = MemoryRecallRequest(idempotency_key=uuid4(), memory_id=memory_id,
        candidate_sha256=shown["candidate_sha256"], operator="person", purpose="diagnosis",
        target_environment="fixture-python-3.11")
    execution_id = worker.execute_explorer(run_id, directories=RunDirectories(workspace, artifacts),
        agent_input={"issue": "inspect the fixture"}, max_bytes=4096, stop_event=Event(),
        model_gateway=gateway, memory_recall=recall)
    assert len(turns) == 2
    recalled = turns[0][MEMORY_RECALL_CONTEXT_KEY]
    assert recalled["content_status"] == "unverified_candidate"
    assert recalled["source_task_id"] == str(source["task_id"])
    assert recalled["source_verification_id"] == str(source["verification_id"])
    assert recalled["source_state"] == "trusted_passed" and recalled["review_id"]
    assert recalled["applicable_base_commit"] == "a" * 40
    assert recalled["applicable_environment"] == "fixture-python-3.11" and recalled["valid_until"]
    with factory() as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution.input[MEMORY_RECALL_CONTEXT_KEY] == recalled
        assert session.get(MemoryUse, UUID(recalled["use_id"])) is not None
    with factory() as session:
        with pytest.raises(VerificationEvidenceError, match="Memory-assisted"):
            RunCoordinator(session).run_independent_evaluation(run_id, worker_id,
                agent_execution_id=execution_id, draft_artifact_id=uuid4(), workspace_root=workspace,
                artifact_root=artifacts, image="unused", command=(), cases=(), suite_version="fixture",
                max_diff_bytes=1024, max_patch_bytes=1024, timeout_seconds=1, stop_event=Event())
    config = VerificationConfig(task_id=target_id, base_commit="a" * 40, command_name="test",
        command=("test",), target_tests=("test",), external_connections="none", independent_suite_version="fixture")
    with factory() as session:
        with pytest.raises(VerificationEvidenceError, match="Memory-assisted"):
            RunCoordinator(session).finalize_passing_verification(run_id, worker_id,
                agent_execution_id=execution_id, draft_artifact_id=uuid4(), independent_artifact_id=uuid4(),
                verification_config=config, workspace_root=workspace, artifact_root=artifacts,
                max_diff_bytes=1024, max_patch_bytes=1024, stop_event=Event())
    monkeypatch.setattr(worker, "_independent_evaluation_material_is_valid", lambda *_: True)
    recovery = worker._commit_rediagnosis_independent_evaluation(run_id,
        result={"outcome": "verifier_blocked", "preliminary_public_outcome": "passed"},
        directories=RunDirectories(workspace, artifacts), verification_config=config, material=object(),
        max_diff_bytes=1024, max_patch_bytes=1024, stop_event=Event())
    assert recovery == {"outcome": "verifier_blocked", "reason": "memory_context_precludes_independent_evaluation"}
    # Exercise the orchestration entry as well as the coordinator gates above. The Verifier
    # checkpoint is constructed; isolation must reject before independent evidence is read.
    from types import SimpleNamespace
    from devflow_ai.orchestration import finalize_benchmark_run
    with factory.begin() as session:
        session.add(AgentExecution(run_id=run_id, agent_role="verifier",
            status=AgentExecutionStatus.RUNNING, input={}, output={
                "outcome": "VERIFICATION_BLOCKED", "preliminary_public_outcome": "passed",
                "draft_artifact_id": str(uuid4())}))
    before = set(artifacts.rglob("*"))
    material = SimpleNamespace(image="unused", command=(), cases=(),
        calibration_workspaces=(workspace, workspace, "unused"), timeout_seconds=1)
    with pytest.raises(VerificationEvidenceError, match="Memory-assisted"):
        finalize_benchmark_run(worker, run_id, directories=RunDirectories(workspace, artifacts),
            verification_config=config, material=material, max_diff_bytes=1024,
            max_patch_bytes=1024, stop_event=Event())
    assert set(artifacts.rglob("*")) == before


def test_prepare_recall_concurrent_duplicate_returns_candidate_once(memory_session_factory):
    factory = memory_session_factory
    source = _passed_run(factory)
    target_id, target_run = _running_target(factory)
    item = _ingest(factory, _candidate(source, topic="diagnostics",
        valid_until=datetime.now(timezone.utc) + timedelta(days=1)))
    memory_id = UUID(item["id"])
    shown = _read(factory, repository="example-repo", memory_id=memory_id)
    with factory() as session:
        MemoryService(session).review(memory_id=memory_id, scope=RepositoryScope("example-repo"),
            candidate_sha256=shown["candidate_sha256"], conclusion="accepted", reviewer="person", reason="reviewed")
    barrier = Barrier(2)
    key = uuid4()
    def prepare():
        with factory() as session:
            barrier.wait(timeout=10)
            try:
                return MemoryService(session).prepare_recall(idempotency_key=key,
                    scope=RepositoryScope("example-repo"), task_id=target_id, run_id=target_run,
                    memory_id=memory_id, candidate_sha256=shown["candidate_sha256"],
                    operator="person", purpose="diagnosis", target_environment="fixture-python-3.11")
            except MemoryStateError:
                return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: prepare(), range(2)))
    assert sum(result is not None for result in results) == 1
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(MemoryUse)) == 1


def test_prepare_recall_does_not_return_on_failed_commit(memory_session_factory):
    factory = memory_session_factory
    source = _passed_run(factory)
    target_id, target_run = _running_target(factory)
    item = _ingest(factory, _candidate(source, topic="diagnostics",
        valid_until=datetime.now(timezone.utc) + timedelta(days=1)))
    memory_id = UUID(item["id"])
    shown = _read(factory, repository="example-repo", memory_id=memory_id)
    with factory() as session:
        MemoryService(session).review(memory_id=memory_id, scope=RepositoryScope("example-repo"),
            candidate_sha256=shown["candidate_sha256"], conclusion="accepted", reviewer="person", reason="reviewed")
    with factory() as session:
        def fail_commit(_session):
            raise RuntimeError("constructed commit failure")
        event.listen(session, "before_commit", fail_commit, once=True)
        with pytest.raises(RuntimeError, match="constructed commit failure"):
            MemoryService(session).prepare_recall(idempotency_key=uuid4(), scope=RepositoryScope("example-repo"),
                task_id=target_id, run_id=target_run, memory_id=memory_id,
                candidate_sha256=shown["candidate_sha256"], operator="person", purpose="diagnosis",
                target_environment="fixture-python-3.11")
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(MemoryUse)) == 0


def test_prior_run_memory_context_blocks_independent_evaluation_for_same_task(memory_session_factory):
    from devflow_ai.context import MEMORY_RECALL_CONTEXT_KEY
    from devflow_ai.coordinator import RunCoordinator
    from devflow_ai.verifier import VerificationEvidenceError

    factory = memory_session_factory
    target_id, current_run = _running_target(factory)
    other_id, other_run = _running_target(factory)
    with factory.begin() as session:
        session.get(Run, current_run).attempt_number = 2
        session.flush()
        previous = Run(task_id=target_id, attempt_number=1, status=RunStatus.FAILED,
            creation_idempotency_key=str(uuid4()), worker_id="old-worker",
            lease_expires_at=datetime.now(timezone.utc))
        session.add(previous)
        session.flush()
        session.add(AgentExecution(run_id=previous.id, agent_role="explorer",
            status=AgentExecutionStatus.FAILED, input={MEMORY_RECALL_CONTEXT_KEY: {"use_id": str(uuid4())}}))
    with factory() as session, session.begin():
        with pytest.raises(VerificationEvidenceError, match="Memory-assisted"):
            RunCoordinator(session).assert_independent_memory_isolation(current_run)
    with factory() as session, session.begin():
        RunCoordinator(session).assert_independent_memory_isolation(other_run)


def test_explicit_recall_follows_first_approved_explorer_handoff(memory_session_factory, tmp_path):
    from devflow_ai.context import MEMORY_RECALL_CONTEXT_KEY
    from types import SimpleNamespace
    from devflow_ai.coordinator import ModelTokenUsage
    from devflow_ai.model_gateway import ModelGateway, ModelReply
    from devflow_ai.orchestration import drive_run_stages
    from devflow_ai.worker import RunDirectories

    factory = memory_session_factory
    source = _passed_run(factory)
    target_id, run_id = _running_target(factory)
    item = _ingest(factory, _candidate(source, topic="diagnostics",
        valid_until=datetime.now(timezone.utc) + timedelta(days=1)))
    memory_id = UUID(item["id"])
    shown = _read(factory, repository="example-repo", memory_id=memory_id)
    with factory() as session:
        MemoryService(session).review(memory_id=memory_id, scope=RepositoryScope("example-repo"),
            candidate_sha256=shown["candidate_sha256"], conclusion="accepted", reviewer="person", reason="reviewed")
    with factory.begin() as session:
        task = session.get(Task, target_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {**snapshot, "permissions": {"allowed_tools_by_role": {
            **snapshot["permissions"]["allowed_tools_by_role"], "EXPLORER": ["read_file"]}}}
        worker_id = session.get(Run, run_id).worker_id
    workspace, artifacts = tmp_path / "workspace", tmp_path / "artifacts"
    workspace.mkdir()
    artifacts.mkdir()
    (workspace / "README.md").write_text("fixture evidence\n", encoding="utf-8")
    turns = []
    def generate(prompt, _limit):
        data = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
        turns.append(data)
        if len(turns) % 2:
            output = {"path": "README.md"}
        else:
            output = {"summary": "Read the fixture.", "read_evidence": {
                "path": "README.md", "tool_call_id": data["read"]["tool_call_id"]}}
        return ModelReply(json.dumps(output), ModelTokenUsage(input_tokens=10, output_tokens=5))
    worker = Worker(factory, worker_id)
    gateway = ModelGateway(session_factory=factory, worker_id=worker_id, estimate_input_tokens=lambda _: 100,
                           generate=generate, max_output_tokens=200)
    selection = MemoryRecallRequest(idempotency_key=uuid4(), memory_id=memory_id,
        candidate_sha256=shown["candidate_sha256"], operator="person", purpose="diagnosis",
        target_environment="fixture-python-3.11")
    decisions = iter([
        {"outcome": "EXPLORE", "reason": "Need evidence", "exploration_focus": "Inspect the fixture"},
        {"outcome": "RE_EXPLORE", "reason": "Need another look", "next_goal": "Inspect the fixture again"},
        {"outcome": "BLOCKED", "reason": "Demonstration complete", "decision_required": "Stop"},
    ])
    supervisor = ModelGateway(session_factory=factory, worker_id=worker_id, estimate_input_tokens=lambda _: 100,
        generate=lambda *_: ModelReply(next(decisions), ModelTokenUsage(input_tokens=10, output_tokens=5)),
        max_output_tokens=200)
    gateways = SimpleNamespace(supervisor=supervisor, explorer=gateway, planner=None, coder=None, verifier=None)
    result = drive_run_stages(factory, run_id, worker=worker, gateways=gateways,
        directories=RunDirectories(workspace, artifacts), stop_event=Event(), verification_config=None,
        max_stages=3, max_read_bytes=4096, memory_recall=selection)
    assert result["reason"] == "supervisor_did_not_route"
    assert len(turns) == 4
    assert turns[0][MEMORY_RECALL_CONTEXT_KEY]["candidate_id"] == str(memory_id)
    assert MEMORY_RECALL_CONTEXT_KEY not in turns[2]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(MemoryUse)) == 1
        explorations = session.scalars(select(AgentExecution).where(AgentExecution.run_id == run_id,
            AgentExecution.agent_role == "explorer").order_by(AgentExecution.created_at)).all()
        assert len(explorations) == 2
        assert MEMORY_RECALL_CONTEXT_KEY in explorations[0].input
        assert MEMORY_RECALL_CONTEXT_KEY not in explorations[1].input


def test_independent_check_and_memory_execution_serialize_on_run(memory_session_factory):
    from devflow_ai.context import MEMORY_RECALL_CONTEXT_KEY
    from devflow_ai.coordinator import RunCoordinator
    from devflow_ai.verifier import VerificationEvidenceError

    factory = memory_session_factory
    _, run_id = _running_target(factory)
    with factory() as session:
        worker_id = session.get(Run, run_id).worker_id
    locked, release = Event(), Event()
    writer_pid = []
    def evaluate_check():
        with factory() as session, session.begin():
            coordinator = RunCoordinator(session)
            coordinator.assert_execution_right(run_id, worker_id)
            coordinator.assert_independent_memory_isolation(run_id)
            writer_pid.append(session.scalar(select(func.pg_backend_pid())))
            locked.set()
            assert release.wait(15)
    def start_memory_execution():
        with factory() as session:
            return RunCoordinator(session).start_agent_execution(run_id, worker_id, role=AgentRole.EXPLORER,
                agent_input={MEMORY_RECALL_CONTEXT_KEY: {"use_id": str(uuid4())}})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(evaluate_check)
        try:
            assert locked.wait(10)
            second = pool.submit(start_memory_execution)
            deadline = monotonic() + 10
            blocked = False
            while monotonic() < deadline and not second.done():
                with factory() as session:
                    blocked = session.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE :writer = ANY(pg_blocking_pids(pid)))"), {"writer": writer_pid[0]})
                if blocked:
                    break
                sleep(0.02)
            assert blocked, "memory execution did not wait for the evaluation Run lock"
        finally:
            release.set()
        first.result(timeout=15)
        second.result(timeout=15)
    with factory() as session, session.begin():
        with pytest.raises(VerificationEvidenceError, match="Memory-assisted"):
            RunCoordinator(session).assert_independent_memory_isolation(run_id)


def test_driver_does_not_declare_memory_without_an_explorer_handoff(memory_session_factory, tmp_path):
    from types import SimpleNamespace
    from devflow_ai.coordinator import ModelTokenUsage
    from devflow_ai.model_gateway import ModelGateway, ModelReply
    from devflow_ai.orchestration import drive_run_stages
    from devflow_ai.worker import RunDirectories

    factory = memory_session_factory
    source = _passed_run(factory)
    _, run_id = _running_target(factory)
    item = _ingest(factory, _candidate(source, topic="diagnostics",
        valid_until=datetime.now(timezone.utc) + timedelta(days=1)))
    shown = _read(factory, repository="example-repo", memory_id=UUID(item["id"]))
    with factory() as session:
        worker_id = session.get(Run, run_id).worker_id
    worker = Worker(factory, worker_id)
    gateway = ModelGateway(session_factory=factory, worker_id=worker_id, estimate_input_tokens=lambda _: 100,
        generate=lambda *_: ModelReply({"outcome": "BLOCKED", "reason": "Stop", "decision_required": "Review"},
                                       ModelTokenUsage(input_tokens=10, output_tokens=5)), max_output_tokens=200)
    selection = MemoryRecallRequest(idempotency_key=uuid4(), memory_id=UUID(item["id"]),
        candidate_sha256=shown["candidate_sha256"], operator="person", purpose="diagnosis",
        target_environment="fixture-python-3.11")
    result = drive_run_stages(factory, run_id, worker=worker,
        gateways=SimpleNamespace(supervisor=gateway, explorer=None, planner=None, coder=None, verifier=None),
        directories=RunDirectories(tmp_path, tmp_path), stop_event=Event(), verification_config=None,
        memory_recall=selection)
    assert result["reason"] == "supervisor_did_not_route"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(MemoryUse)) == 0
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == run_id, AgentExecution.agent_role == "explorer")) == 0


def test_applicability_requires_exact_task_base_and_declared_environment(memory_session_factory):
    factory = memory_session_factory
    source = _passed_run(factory)
    target_id, target_run = _running_target(factory)
    expiry = datetime.now(timezone.utc) + timedelta(days=1)
    variants = {
        "matching": ("a" * 40, "fixture-python-3.11"),
        "other_base": ("b" * 40, "fixture-python-3.11"),
        "other_environment": ("a" * 40, "another-environment"),
        "legacy": (None, None),
    }
    items = {}
    for name, (commit, environment) in variants.items():
        record = _ingest(factory, _candidate(source, identity_key=name, content=name, topic="applicability",
            valid_until=expiry, applicable_base_commit=commit, applicable_environment=environment))
        memory_id = UUID(record["id"])
        shown = _read(factory, repository="example-repo", memory_id=memory_id)
        with factory() as session:
            MemoryService(session).review(memory_id=memory_id, scope=RepositoryScope("example-repo"),
                candidate_sha256=shown["candidate_sha256"], conclusion="accepted", reviewer="person", reason="reviewed")
        items[name] = shown
    with factory() as session:
        preview = MemoryService(session).inspect_candidates(scope=RepositoryScope("example-repo"),
            target_task_id=target_id, topic="applicability", target_environment="fixture-python-3.11")
    checks = {row["identity_key"]: row["candidate_checks"] for row in preview}
    assert checks["matching"]["base_commit_matches"] is True
    assert checks["matching"]["environment_declaration_matches"] is True
    assert checks["matching"]["eligible_to_request_use"] is True
    assert checks["other_base"]["base_commit_matches"] is False
    assert checks["other_environment"]["environment_declaration_matches"] is False
    assert checks["legacy"]["base_commit_matches"] is None
    assert all(not checks[name]["eligible_to_request_use"] for name in ("other_base", "other_environment", "legacy"))
    with factory() as session:
        preview_without_environment = MemoryService(session).inspect_candidates(scope=RepositoryScope("example-repo"),
            target_task_id=target_id, topic="applicability")
    assert not any(row["candidate_checks"]["eligible_to_request_use"] for row in preview_without_environment)
    key = uuid4()
    def declare(name, environment, *, key_value=None):
        with factory() as session:
            return MemoryService(session).record_use(idempotency_key=key_value or uuid4(),
                scope=RepositoryScope("example-repo"), task_id=target_id, run_id=target_run,
                memory_id=UUID(items[name]["id"]), candidate_sha256=items[name]["candidate_sha256"],
                operator="operator", purpose="diagnosis", target_environment=environment)
    for name in ("other_base", "other_environment"):
        with pytest.raises(MemoryScopeError, match="applicability"):
            declare(name, "fixture-python-3.11")
    with pytest.raises(MemoryStateError, match="applicability"):
        declare("legacy", "fixture-python-3.11")
    with pytest.raises(MemoryStateError, match="applicability"):
        declare("matching", None)
    recorded = declare("matching", "fixture-python-3.11", key_value=key)
    assert recorded["target_base_commit"] == "a" * 40
    assert recorded["target_environment"] == "fixture-python-3.11"
    with factory.begin() as session:
        session.get(Task, target_id).base_commit = "c" * 40  # constructed external change only
    replay = declare("matching", "fixture-python-3.11", key_value=key)
    assert replay["idempotent"] and replay["target_base_commit"] == "a" * 40
    with pytest.raises(MemoryScopeError, match="applicability"):
        declare("matching", "fixture-python-3.11")


def test_legacy_digest_and_use_survive_applicability_migration(memory_session_factory):
    factory = memory_session_factory
    source = _passed_run(factory)
    target_id, target_run = _running_target(factory)
    expiry = datetime.now(timezone.utc) + timedelta(days=1)
    item = _ingest(factory, _candidate(source, topic="legacy", valid_until=expiry,
        applicable_base_commit=None, applicable_environment=None))
    memory_id = UUID(item["id"])
    shown = _read(factory, repository="example-repo", memory_id=memory_id)
    legacy_data = dict(shown)
    for key in ("candidate_sha256", "candidate_digest_version", "idempotent", "status",
                "applicable_base_commit", "applicable_environment"):
        legacy_data.pop(key)
    encoded = json.dumps({"version": 1, "candidate": legacy_data}, sort_keys=True,
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert shown["candidate_digest_version"] == 1
    assert shown["candidate_sha256"] == sha256(encoded).hexdigest()
    with factory() as session:
        review = MemoryService(session).review(memory_id=memory_id, scope=RepositoryScope("example-repo"),
            candidate_sha256=shown["candidate_sha256"], conclusion="accepted", reviewer="person", reason="reviewed")
    use_key = uuid4()
    with factory.begin() as session:
        row = MemoryUse(idempotency_key=use_key, memory_id=memory_id, review_id=UUID(review["id"]),
            task_id=target_id, run_id=target_run, candidate_sha256=shown["candidate_sha256"],
            operator="person", purpose="legacy declaration")
        session.add(row)
        session.flush()
        use_id = row.id
    engine = factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations"))
    def migrate(up):
        with engine.begin() as connection:
            connection.execution_options(schema_translate_map=None)
            connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            with Operations.context(MigrationContext.configure(connection)):
                revision = scripts.get_revision("b41e9d2c8370").module
                (revision.upgrade if up else revision.downgrade)()
    migrate(False)
    migrate(True)
    assert _read(factory, repository="example-repo", memory_id=memory_id)["candidate_sha256"] == shown["candidate_sha256"]
    with factory() as session:
        assert MemoryService(session).read_review(memory_id=memory_id,
            scope=RepositoryScope("example-repo"))["candidate_matches"] is True
    with factory() as session:
        replay = MemoryService(session).record_use(idempotency_key=use_key, scope=RepositoryScope("example-repo"),
            task_id=target_id, run_id=target_run, memory_id=memory_id, candidate_sha256=shown["candidate_sha256"],
            operator="person", purpose="legacy declaration")
    assert replay["id"] == str(use_id) and replay["idempotent"]
    assert replay["target_base_commit"] is None and replay["target_environment"] is None


def test_preview_explains_candidate_checks_without_claiming_use_or_truth(memory_session_factory):
    factory = memory_session_factory
    source, target = _passed_run(factory), _passed_run(factory)
    expiry = datetime.now(timezone.utc) + timedelta(days=1)
    ids, hashes = {}, {}
    for name in ("pending", "accepted", "rejected", "stale", "conflicted", "old", "successor"):
        item = _ingest(factory, _candidate(source, identity_key=name, content=name, topic="review", valid_until=expiry))
        ids[name] = UUID(item["id"])
        hashes[name] = _read(factory, repository="example-repo", memory_id=ids[name])["candidate_sha256"]
    for name in ("accepted", "rejected", "stale", "conflicted", "old", "successor"):
        with factory() as session:
            MemoryService(session).review(memory_id=ids[name], scope=RepositoryScope("example-repo"),
                candidate_sha256=hashes[name], conclusion="rejected" if name == "rejected" else "accepted",
                reviewer="operator", reason="reviewed")
    with factory.begin() as session:
        session.get(MemoryItem, ids["stale"]).content = "changed outside the service"
    with factory() as session:
        MemoryService(session).declare_conflict(scope=RepositoryScope("example-repo"),
            first_id=ids["conflicted"], second_id=ids["pending"], first_sha256=hashes["conflicted"],
            second_sha256=hashes["pending"], reporter="operator", reason="inconsistent")
    with factory() as session:
        MemoryService(session).declare_revision(scope=RepositoryScope("example-repo"),
            previous_id=ids["old"], next_id=ids["successor"], previous_sha256=hashes["old"],
            next_sha256=hashes["successor"], operator="operator", reason="updated")
    with factory() as session:
        rows = MemoryService(session).inspect_candidates(scope=RepositoryScope("example-repo"),
            target_task_id=target["task_id"], topic="review", target_environment="fixture-python-3.11")
    rows = {row["identity_key"]: row for row in rows}
    assert rows["pending"]["candidate_checks"]["review_state"] == "missing"
    assert rows["rejected"]["candidate_checks"]["review_state"] == "rejected"
    assert rows["stale"]["candidate_checks"]["review_state"] == "stale"
    assert rows["conflicted"]["candidate_checks"]["has_open_conflict"] is True
    assert rows["old"]["candidate_checks"]["has_declared_successor"] is True
    assert rows["successor"]["candidate_checks"]["has_declared_successor"] is False
    assert {name for name, row in rows.items() if row["candidate_checks"]["eligible_to_request_use"]} == {
        "accepted", "successor"}
    assert all(row["content_status"] == "unverified_candidate" for row in rows.values())
    # Candidate prechecks do not authorize use: this target Run is already PASSED.
    with factory() as session:
        with pytest.raises(MemoryStateError, match="running"):
            MemoryService(session).record_use(idempotency_key=uuid4(), scope=RepositoryScope("example-repo"),
                task_id=target["task_id"], run_id=target["run_id"], memory_id=ids["accepted"],
                candidate_sha256=hashes["accepted"], operator="operator", purpose="diagnosis",
                target_environment="fixture-python-3.11")


def test_preview_cli_returns_only_explicitly_scoped_candidates(memory_session_factory, capsys):
    factory = memory_session_factory
    source = _passed_run(factory)
    target = _passed_run(factory)
    _ingest(factory, _candidate(source, identity_key="legacy"))
    expiry = datetime.now(timezone.utc) + timedelta(days=1)
    _ingest(factory, _candidate(source, identity_key="previewable", topic="tests", valid_until=expiry))
    assert memory_main(["preview", "--scope-repository", "example-repo", "--target-task-id",
                        str(target["task_id"]), "--topic", "tests"], session_factory=factory) == 0
    result = json.loads(capsys.readouterr().out)
    assert [item["identity_key"] for item in result["candidates"]] == ["previewable"]


def test_memory_review_binds_candidate_and_preserves_original(memory_session_factory):
    factory = memory_session_factory
    source = _passed_run(factory)
    stored = _ingest(factory, _candidate(source))
    memory_id = UUID(stored["id"])
    displayed = _read(factory, repository="example-repo", memory_id=memory_id)
    scope = RepositoryScope("example-repo")
    args = dict(memory_id=memory_id, scope=scope, candidate_sha256=displayed["candidate_sha256"],
                conclusion="accepted", reviewer="operator", reason="Reviewed source and scope")
    with factory() as session:
        first = MemoryService(session).review(**args)
    with factory() as session:
        replay = MemoryService(session).review(**args)
    assert replay["id"] == first["id"] and replay["idempotent"] is True
    with factory() as session:
        with pytest.raises(MemoryConflictError):
            MemoryService(session).review(**{**args, "conclusion": "rejected"})
    with factory() as session:
        assert MemoryService(session).read_review(memory_id=memory_id, scope=scope)["id"] == first["id"]
    assert _read(factory, repository="example-repo", memory_id=memory_id)["content_status"] == "unverified_candidate"
    with factory() as session:
        assert MemoryService(session).read_review(memory_id=memory_id, scope=RepositoryScope("other")) is None
    with factory.begin() as session:
        session.get(MemoryItem, memory_id).content = "changed after display"
    with factory() as session:
        with pytest.raises(MemoryConflictError, match="changed"):
            MemoryService(session).review(**args)
    with factory() as session:
        assert MemoryService(session).read_review(memory_id=memory_id, scope=scope)["candidate_matches"] is False


def test_concurrent_conflicting_memory_reviews_have_one_winner(memory_session_factory):
    factory = memory_session_factory
    source = _passed_run(factory)
    stored = _ingest(factory, _candidate(source))
    memory_id = UUID(stored["id"])
    digest = _read(factory, repository="example-repo", memory_id=memory_id)["candidate_sha256"]
    barrier = Barrier(2, timeout=15)
    def submit(conclusion):
        barrier.wait()
        with factory() as session:
            try:
                return MemoryService(session).review(memory_id=memory_id, scope=RepositoryScope("example-repo"),
                    candidate_sha256=digest, conclusion=conclusion, reviewer="operator", reason="reviewed")
            except MemoryConflictError:
                return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, ("accepted", "rejected")))
    assert sum(result is not None for result in outcomes) == 1


def test_review_cli_and_retired_candidate(memory_session_factory, capsys):
    factory = memory_session_factory
    source = _passed_run(factory)
    stored = _ingest(factory, _candidate(source))
    memory_id = UUID(stored["id"])
    digest = _read(factory, repository="example-repo", memory_id=memory_id)["candidate_sha256"]
    assert memory_main(["review", "--scope-repository", "example-repo", "--memory-id", str(memory_id),
        "--candidate-sha256", digest, "--conclusion", "rejected", "--reviewer", "person",
        "--reason", "Unclear applicability"], session_factory=factory) == 0
    assert json.loads(capsys.readouterr().out)["conclusion"] == "rejected"
    assert memory_main(["read-review", "--scope-repository", "example-repo", "--memory-id", str(memory_id)],
                       session_factory=factory) == 0
    assert json.loads(capsys.readouterr().out)["candidate_matches"] is True
    with factory() as session:
        MemoryService(session).retire(memory_id, scope=RepositoryScope("example-repo"),
                                      operator="operator", reason="retired")
    with factory() as session:
        with pytest.raises(MemoryStateError, match="retired"):
            MemoryService(session).review(memory_id=memory_id, scope=RepositoryScope("example-repo"),
                candidate_sha256=digest, conclusion="rejected", reviewer="person", reason="Unclear applicability")


def test_declared_conflict_visible_in_preview_and_resolution_preserves_candidates(memory_session_factory):
    factory = memory_session_factory
    source, target = _passed_run(factory), _passed_run(factory)
    expiry = datetime.now(timezone.utc) + timedelta(days=1)
    a = _ingest(factory, _candidate(source, identity_key="a", topic="tests", valid_until=expiry, content="A"))
    b = _ingest(factory, _candidate(source, identity_key="b", topic="tests", valid_until=expiry, content="B"))
    a_id, b_id = UUID(a["id"]), UUID(b["id"])
    a_hash = _read(factory, repository="example-repo", memory_id=a_id)["candidate_sha256"]
    b_hash = _read(factory, repository="example-repo", memory_id=b_id)["candidate_sha256"]
    scope = RepositoryScope("example-repo")
    args = dict(scope=scope, first_id=a_id, second_id=b_id, first_sha256=a_hash, second_sha256=b_hash,
                reporter="operator", reason="Different expected behavior")
    with factory() as session:
        result = MemoryService(session).declare_conflict(**args)
    with factory() as session:
        replay = MemoryService(session).declare_conflict(**{**args, "first_id": b_id, "second_id": a_id,
                                                          "first_sha256": b_hash, "second_sha256": a_hash})
    assert result["id"] == replay["id"] and replay["idempotent"]
    with factory() as session:
        preview = MemoryService(session).inspect_candidates(scope=scope, target_task_id=target["task_id"], topic="tests")
    assert all(row["open_conflict_ids"] == [result["id"]] for row in preview)
    with factory() as session:
        assert MemoryService(session).read_conflicts(scope=RepositoryScope("other"), memory_id=a_id) == []
    with factory() as session:
        resolved = MemoryService(session).resolve_conflict(scope=scope, conflict_id=UUID(result["id"]),
                                                            operator="operator", resolution="Different versions")
    assert resolved["status"] == "resolved"
    with factory() as session:
        assert MemoryService(session).resolve_conflict(scope=scope, conflict_id=UUID(result["id"]),
            operator="operator", resolution="Different versions")["idempotent"]
    with factory() as session:
        with pytest.raises(MemoryConflictError):
            MemoryService(session).resolve_conflict(scope=scope, conflict_id=UUID(result["id"]),
                operator="operator", resolution="Another result")
    assert _read(factory, repository="example-repo", memory_id=a_id)["content"] == "A"
    with factory() as session:
        assert MemoryService(session).read_conflicts(scope=scope, memory_id=b_id)[0]["status"] == "resolved"


def test_conflict_declaration_race_and_stale_snapshot(memory_session_factory):
    factory = memory_session_factory
    source = _passed_run(factory)
    a = _ingest(factory, _candidate(source, identity_key="a", content="A"))
    b = _ingest(factory, _candidate(source, identity_key="b", content="B"))
    a_id, b_id = UUID(a["id"]), UUID(b["id"])
    a_hash = _read(factory, repository="example-repo", memory_id=a_id)["candidate_sha256"]
    b_hash = _read(factory, repository="example-repo", memory_id=b_id)["candidate_sha256"]
    barrier = Barrier(2, timeout=15)
    def submit(reverse):
        barrier.wait()
        ids = (b_id, a_id, b_hash, a_hash) if reverse else (a_id, b_id, a_hash, b_hash)
        with factory() as session:
            return MemoryService(session).declare_conflict(scope=RepositoryScope("example-repo"),
                first_id=ids[0], second_id=ids[1], first_sha256=ids[2], second_sha256=ids[3],
                reporter="operator", reason="inconsistent")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, (False, True)))
    assert results[0]["id"] == results[1]["id"]
    assert sorted(row["idempotent"] for row in results) == [False, True]
    with factory.begin() as session:
        session.get(MemoryItem, a_id).content = "changed"
    with factory() as session:
        assert MemoryService(session).read_conflicts(scope=RepositoryScope("example-repo"),
                                                    memory_id=a_id)[0]["snapshots_match"] is False
    with factory() as session:
        with pytest.raises(MemoryConflictError, match="changed"):
            MemoryService(session).resolve_conflict(scope=RepositoryScope("example-repo"),
                conflict_id=UUID(results[0]["id"]), operator="operator", resolution="obsolete")


def test_conflict_cli_requires_matching_scope(memory_session_factory, capsys):
    factory = memory_session_factory
    source = _passed_run(factory)
    first = _ingest(factory, _candidate(source, identity_key="a"))
    second = _ingest(factory, _candidate(source, identity_key="b"))
    a = _read(factory, repository="example-repo", memory_id=UUID(first["id"]))
    b = _read(factory, repository="example-repo", memory_id=UUID(second["id"]))
    arguments = ["declare-conflict", "--scope-repository", "example-repo", "--first-id", a["id"],
        "--second-id", b["id"], "--first-sha256", a["candidate_sha256"], "--second-sha256",
        b["candidate_sha256"], "--reporter", "operator", "--reason", "inconsistent"]
    assert memory_main(arguments, session_factory=factory) == 0
    conflict_id = json.loads(capsys.readouterr().out)["id"]
    assert memory_main(["resolve-conflict", "--scope-repository", "other", "--conflict-id", conflict_id,
                        "--operator", "operator", "--resolution", "different contexts"], session_factory=factory) == 2
    capsys.readouterr()
    assert memory_main(["read-conflicts", "--scope-repository", "example-repo", "--memory-id", a["id"]],
                       session_factory=factory) == 0
    assert json.loads(capsys.readouterr().out)["conflicts"][0]["status"] == "open"


def test_revision_preserves_history_and_checks_snapshots(memory_session_factory):
    factory = memory_session_factory
    source, target = _passed_run(factory), _passed_run(factory)
    expiry = datetime.now(timezone.utc) + timedelta(days=1)
    old = _ingest(factory, _candidate(source, identity_key="old", content="old", topic="topic", valid_until=expiry))
    new = _ingest(factory, _candidate(source, identity_key="new", content="new", topic="topic", valid_until=expiry))
    old_id, new_id = UUID(old["id"]), UUID(new["id"])
    old_hash = _read(factory, repository="example-repo", memory_id=old_id)["candidate_sha256"]
    new_hash = _read(factory, repository="example-repo", memory_id=new_id)["candidate_sha256"]
    args = dict(scope=RepositoryScope("example-repo"), previous_id=old_id, next_id=new_id,
                previous_sha256=old_hash, next_sha256=new_hash, operator="operator", reason="Version changed")
    with factory() as session:
        record = MemoryService(session).declare_revision(**args)
    with factory() as session:
        assert MemoryService(session).declare_revision(**args)["idempotent"]
    with factory() as session:
        with pytest.raises(MemoryError, match="later creation"):
            MemoryService(session).declare_revision(**{**args, "previous_id": new_id, "next_id": old_id,
                                                       "previous_sha256": new_hash, "next_sha256": old_hash})
    with factory() as session:
        preview = MemoryService(session).inspect_candidates(scope=RepositoryScope("example-repo"),
            target_task_id=target["task_id"], topic="topic")
    assert {row["content"] for row in preview} == {"old", "new"}
    assert all(row["revision_ids"] == [record["id"]] for row in preview)
    with factory() as session:
        assert MemoryService(session).read_revisions(scope=RepositoryScope("other"), memory_id=old_id) == []
    with factory.begin() as session:
        session.get(MemoryItem, old_id).content = "externally changed"
    with factory() as session:
        assert not MemoryService(session).read_revisions(scope=RepositoryScope("example-repo"),
                                                        memory_id=new_id)[0]["snapshots_match"]


def test_revision_concurrent_successors_do_not_fork(memory_session_factory):
    factory = memory_session_factory
    source = _passed_run(factory)
    entries = [_ingest(factory, _candidate(source, identity_key=key, content=key)) for key in ("old", "a", "b")]
    ids = [UUID(entry["id"]) for entry in entries]
    hashes = [_read(factory, repository="example-repo", memory_id=mid)["candidate_sha256"] for mid in ids]
    barrier = Barrier(2, timeout=15)
    def submit(i):
        barrier.wait()
        with factory() as session:
            try:
                return MemoryService(session).declare_revision(scope=RepositoryScope("example-repo"),
                    previous_id=ids[0], next_id=ids[i], previous_sha256=hashes[0], next_sha256=hashes[i],
                    operator="operator", reason="update")
            except MemoryConflictError:
                return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, (1, 2)))
    assert sum(value is not None for value in outcomes) == 1


def test_revision_cli_rejects_cross_scope_and_retired_successor(memory_session_factory, capsys):
    factory = memory_session_factory
    source, other = _passed_run(factory), _passed_run(factory, repo="other-repo")
    old = _ingest(factory, _candidate(source, identity_key="old"))
    new = _ingest(factory, _candidate(source, identity_key="new"))
    foreign = _ingest(factory, _candidate(other, identity_key="foreign"))
    old_hash = _read(factory, repository="example-repo", memory_id=UUID(old["id"]))["candidate_sha256"]
    new_hash = _read(factory, repository="example-repo", memory_id=UUID(new["id"]))["candidate_sha256"]
    foreign_hash = _read(factory, repository="other-repo", memory_id=UUID(foreign["id"]))["candidate_sha256"]
    with factory() as session:
        with pytest.raises(MemoryScopeError):
            MemoryService(session).declare_revision(scope=RepositoryScope("example-repo"),
                previous_id=UUID(old["id"]), next_id=UUID(foreign["id"]), previous_sha256=old_hash,
                next_sha256=foreign_hash, operator="operator", reason="update")
    args = ["declare-revision", "--scope-repository", "example-repo", "--previous-id", old["id"],
            "--next-id", new["id"], "--previous-sha256", old_hash, "--next-sha256", new_hash,
            "--operator", "operator", "--reason", "update"]
    assert memory_main(args, session_factory=factory) == 0
    record = json.loads(capsys.readouterr().out)
    assert memory_main(["read-revisions", "--scope-repository", "example-repo", "--memory-id", old["id"]],
                       session_factory=factory) == 0
    assert json.loads(capsys.readouterr().out)["revisions"][0]["id"] == record["id"]
    with factory() as session:
        MemoryService(session).retire(UUID(new["id"]), scope=RepositoryScope("example-repo"),
                                      operator="operator", reason="no longer valid")
    assert memory_main(args, session_factory=factory) == 2
    assert "successor must be active" in capsys.readouterr().err


def test_operator_use_record_is_idempotent_and_does_not_modify_run(memory_session_factory):
    factory = memory_session_factory
    source = _passed_run(factory)
    expiry = datetime.now(timezone.utc) + timedelta(days=1)
    item = _ingest(factory, _candidate(source, topic="tests", valid_until=expiry))
    memory_id = UUID(item["id"])
    digest = _read(factory, repository="example-repo", memory_id=memory_id)["candidate_sha256"]
    with factory() as session:
        MemoryService(session).review(memory_id=memory_id, scope=RepositoryScope("example-repo"),
            candidate_sha256=digest, conclusion="accepted", reviewer="reviewer", reason="reviewed")
    task_id, run_id = _running_target(factory)
    args = dict(idempotency_key=uuid4(), scope=RepositoryScope("example-repo"), task_id=task_id, run_id=run_id,
                memory_id=memory_id, candidate_sha256=digest, operator="operator", purpose="diagnosis",
                target_environment="fixture-python-3.11")
    barrier = Barrier(2, timeout=15)
    def submit(_):
        barrier.wait()
        with factory() as session:
            return MemoryService(session).record_use(**args)
    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(submit, range(2)))
    assert records[0]["id"] == records[1]["id"]
    assert sorted(record["idempotent"] for record in records) == [False, True]
    assert records[0]["kind"] == "operator_declared"
    with factory() as session:
        with pytest.raises(MemoryConflictError):
            MemoryService(session).record_use(**{**args, "purpose": "changed"})
    with factory() as session:
        assert MemoryService(session).read_use(scope=RepositoryScope("other"), use_id=UUID(records[0]["id"])) is None
    with factory() as session:
        assert session.get(Run, run_id).status is RunStatus.RUNNING
    with factory.begin() as session:
        session.get(Run, run_id).status = RunStatus.FAILED
    with factory() as session:
        assert MemoryService(session).record_use(**args)["idempotent"]
    with factory() as session:
        with pytest.raises(MemoryStateError, match="running"):
            MemoryService(session).record_use(**{**args, "idempotency_key": uuid4()})


def test_operator_use_rejects_unreviewed_expired_conflicted_and_replaced(memory_session_factory):
    factory = memory_session_factory
    source = _passed_run(factory)
    future = datetime.now(timezone.utc) + timedelta(days=1)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    task_id, run_id = _running_target(factory)
    def candidate(name, expiry):
        row = _ingest(factory, _candidate(source, identity_key=name, topic="tests", valid_until=expiry, content=name))
        mid = UUID(row["id"])
        digest = _read(factory, repository="example-repo", memory_id=mid)["candidate_sha256"]
        return mid, digest
    def review(mid, digest):
        with factory() as session:
            MemoryService(session).review(memory_id=mid, scope=RepositoryScope("example-repo"),
                candidate_sha256=digest, conclusion="accepted", reviewer="reviewer", reason="reviewed")
    def use(mid, digest):
        with factory() as session:
            return MemoryService(session).record_use(idempotency_key=uuid4(), scope=RepositoryScope("example-repo"),
                task_id=task_id, run_id=run_id, memory_id=mid, candidate_sha256=digest,
                operator="operator", purpose="diagnosis", target_environment="fixture-python-3.11")
    unreviewed, unreviewed_hash = candidate("unreviewed", future)
    with pytest.raises(MemoryEvidenceError, match="review"):
        use(unreviewed, unreviewed_hash)
    expired, expired_hash = candidate("expired", past)
    review(expired, expired_hash)
    with pytest.raises(MemoryStateError, match="expired"):
        use(expired, expired_hash)
    old, old_hash = candidate("old", future)
    other, other_hash = candidate("other", future)
    review(old, old_hash)
    with factory() as session:
        conflict = MemoryService(session).declare_conflict(scope=RepositoryScope("example-repo"),
            first_id=old, second_id=other, first_sha256=old_hash, second_sha256=other_hash,
            reporter="operator", reason="inconsistent")
    with pytest.raises(MemoryStateError, match="conflict"):
        use(old, old_hash)
    with factory() as session:
        MemoryService(session).resolve_conflict(scope=RepositoryScope("example-repo"),
            conflict_id=UUID(conflict["id"]), operator="operator", resolution="version difference")
    with factory() as session:
        MemoryService(session).declare_revision(scope=RepositoryScope("example-repo"), previous_id=old,
            next_id=other, previous_sha256=old_hash, next_sha256=other_hash, operator="operator", reason="updated")
    with pytest.raises(MemoryStateError, match="successor"):
        use(old, old_hash)


def test_operator_can_preview_review_prepare_and_trace_recall(memory_session_factory, capsys):
    factory = memory_session_factory
    source = _passed_run(factory)
    target_id, run_id = _running_target(factory)
    item = _ingest(factory, _candidate(source, topic="diagnostics",
        valid_until=datetime.now(timezone.utc) + timedelta(days=1)))
    assert memory_main(["preview", "--scope-repository", "example-repo", "--target-task-id",
        str(target_id), "--topic", "diagnostics", "--target-environment", "fixture-python-3.11"],
        session_factory=factory) == 0
    preview = json.loads(capsys.readouterr().out)["candidates"]
    assert len(preview) == 1 and preview[0]["id"] == item["id"]
    digest = preview[0]["candidate_sha256"]
    assert memory_main(["review", "--scope-repository", "example-repo", "--memory-id", item["id"],
        "--candidate-sha256", digest, "--conclusion", "accepted", "--reviewer", "person", "--reason", "reviewed"],
        session_factory=factory) == 0
    capsys.readouterr()
    args = ["prepare-recall", "--scope-repository", "example-repo", "--idempotency-key", str(uuid4()),
        "--task-id", str(target_id), "--run-id", str(run_id), "--memory-id", item["id"],
        "--candidate-sha256", digest, "--operator", "person", "--purpose", "diagnosis",
        "--target-environment", "fixture-python-3.11"]
    assert memory_main(args, session_factory=factory) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["candidate"]["content_status"] == "unverified_candidate"
    assert prepared["candidate"]["task_id"] == str(source["task_id"])
    assert prepared["candidate"]["verification_id"] == str(source["verification_id"])
    assert prepared["use"]["candidate_sha256"] == digest
    assert memory_main(["read-use", "--scope-repository", "example-repo", "--use-id", prepared["use"]["id"]],
        session_factory=factory) == 0
    assert json.loads(capsys.readouterr().out)["id"] == prepared["use"]["id"]
    assert memory_main(args, session_factory=factory) == 2
    assert capsys.readouterr().out == ""


def test_list_task_uses_preserves_history_and_scope(memory_session_factory, capsys):
    factory = memory_session_factory
    source = _passed_run(factory)
    target_id, run_id = _running_target(factory)
    other_id, other_run = _running_target(factory)
    expiry = datetime.now(timezone.utc) + timedelta(days=1)
    records = []
    for name in ("first", "second"):
        item = _ingest(factory, _candidate(source, identity_key=name, topic="tests", valid_until=expiry))
        memory_id = UUID(item["id"])
        shown = _read(factory, repository="example-repo", memory_id=memory_id)
        with factory() as session:
            MemoryService(session).review(memory_id=memory_id, scope=RepositoryScope("example-repo"),
                candidate_sha256=shown["candidate_sha256"], conclusion="accepted", reviewer="person", reason="reviewed")
        with factory() as session:
            use = MemoryService(session).record_use(idempotency_key=uuid4(), scope=RepositoryScope("example-repo"),
                task_id=target_id, run_id=run_id, memory_id=memory_id, candidate_sha256=shown["candidate_sha256"],
                operator="person", purpose="diagnosis", target_environment="fixture-python-3.11")
        records.append((item, use, shown))
    with factory() as session:
        MemoryService(session).record_use(idempotency_key=uuid4(), scope=RepositoryScope("example-repo"),
            task_id=other_id, run_id=other_run, memory_id=UUID(records[0][0]["id"]),
            candidate_sha256=records[0][2]["candidate_sha256"], operator="person", purpose="other",
            target_environment="fixture-python-3.11")
    with factory() as session:
        MemoryService(session).retire(UUID(records[0][0]["id"]), scope=RepositoryScope("example-repo"),
                                      operator="person", reason="withdrawn")
    with factory() as session:
        listed = MemoryService(session).list_task_uses(scope=RepositoryScope("example-repo"), task_id=target_id)
    by_identity = {item["candidate_identity"]: item for item in listed}
    assert set(by_identity) == {"first", "second"}
    assert by_identity["first"]["candidate_status"] == "retired"
    assert by_identity["first"]["source_task_id"] == str(source["task_id"])
    assert by_identity["first"]["source_verification_id"] == str(source["verification_id"])
    assert all(item["kind"] == "operator_declared" and "content" not in item for item in listed)
    with factory() as session:
        assert MemoryService(session).list_task_uses(scope=RepositoryScope("other"), task_id=target_id) == []
    assert memory_main(["list-uses", "--scope-repository", "example-repo", "--task-id", str(target_id)],
                       session_factory=factory) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "memory_uses_listed" and len(payload["uses"]) == 2


def test_use_cli_and_same_task_exclusion(memory_session_factory, capsys):
    factory = memory_session_factory
    source = _passed_run(factory)
    future = datetime.now(timezone.utc) + timedelta(days=1)
    item = _ingest(factory, _candidate(source, topic="tests", valid_until=future))
    mid = UUID(item["id"])
    digest = _read(factory, repository="example-repo", memory_id=mid)["candidate_sha256"]
    with factory() as session:
        MemoryService(session).review(memory_id=mid, scope=RepositoryScope("example-repo"),
            candidate_sha256=digest, conclusion="accepted", reviewer="reviewer", reason="reviewed")
    with factory.begin() as session:
        session.get(Run, source["run_id"]).status = RunStatus.RUNNING
    with factory() as session:
        with pytest.raises(MemoryScopeError, match="target Task"):
            MemoryService(session).record_use(idempotency_key=uuid4(), scope=RepositoryScope("example-repo"),
                task_id=source["task_id"], run_id=source["run_id"], memory_id=mid,
                candidate_sha256=digest, operator="operator", purpose="diagnosis")
    with factory.begin() as session:
        session.get(Run, source["run_id"]).status = RunStatus.PASSED
    task_id, run_id = _running_target(factory)
    args = ["record-use", "--scope-repository", "example-repo", "--idempotency-key", str(uuid4()),
            "--task-id", str(task_id), "--run-id", str(run_id), "--memory-id", str(mid),
            "--candidate-sha256", digest, "--operator", "operator", "--purpose", "diagnosis",
            "--target-environment", "fixture-python-3.11"]
    assert memory_main(args, session_factory=factory) == 0
    use = json.loads(capsys.readouterr().out)
    assert use["kind"] == "operator_declared"
    assert memory_main(["read-use", "--scope-repository", "example-repo", "--use-id", use["id"]],
                       session_factory=factory) == 0
    assert json.loads(capsys.readouterr().out)["candidate_sha256"] == digest


def test_known_benchmark_target_refuses_memory_and_context_does_not_include_candidate(memory_session_factory, tmp_path):
    from devflow_ai.worker import RunDirectories
    factory = memory_session_factory
    source = _passed_run(factory)
    marker = "synthetic-candidate-never-injected-" + uuid4().hex
    item = _ingest(factory, _candidate(source, topic="tests", content=marker,
        valid_until=datetime.now(timezone.utc) + timedelta(days=1)))
    manifest = load_manifest(Path(__file__).resolve().parents[1] / "benchmarks" / "click_3019_public")
    target_id = create_benchmark_task(factory, manifest, repo="example-repo",
                                      policy=build_default_policy(), operator="test operator")
    target_run = Worker(factory, f"benchmark-{uuid4().hex[:8]}").claim_once().run_id
    with factory() as session:
        context = ContextBuilder(session).build_issue_context(target_run)
        assert marker not in json.dumps(context)
        assert session.get(Task, target_id).admission_record["evidence"][0] == "pack:" + manifest.pack_id
    with factory() as session:
        with pytest.raises(MemoryScopeError, match="disabled for known benchmark"):
            MemoryService(session).inspect_candidates(scope=RepositoryScope("example-repo"),
                target_task_id=target_id, topic="tests")
    with factory() as session:
        with pytest.raises(MemoryScopeError, match="disabled for known benchmark"):
            MemoryService(session).record_use(idempotency_key=uuid4(), scope=RepositoryScope("example-repo"),
                task_id=target_id, run_id=target_run, memory_id=UUID(item["id"]),
                candidate_sha256="a" * 64, operator="operator", purpose="diagnosis")
    with factory() as session:
        worker_id = session.get(Run, target_run).worker_id
    called = []
    with pytest.raises(MemoryScopeError, match="disabled for known benchmark"):
        Worker(factory, worker_id).execute_explorer(target_run,
            directories=RunDirectories(tmp_path, tmp_path), agent_input={}, max_bytes=100, stop_event=Event(),
            explore=lambda *_: called.append(True), memory_recall=MemoryRecallRequest(
                idempotency_key=uuid4(), memory_id=UUID(item["id"]), candidate_sha256="a" * 64,
                operator="person", purpose="diagnosis", target_environment="fixture-python-3.11"))
    assert called == []
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == target_run)) == 0


def _uuid_of(stored):
    return UUID(stored["id"])


def _rows(factory):
    with factory() as session:
        return session.scalars(select(MemoryItem).order_by(MemoryItem.created_at)).all()


def _counts(factory):
    with factory() as session:
        return {model.__tablename__: session.scalar(select(func.count()).select_from(model))
                for model in (Task, Run, Verification, TaskReview, MemoryItem)}


def _accepted_review(factory, fixture, *, conclusion="accepted"):
    with factory.begin() as session:
        review = TaskReview(
            task_id=fixture["task_id"], run_id=fixture["run_id"],
            verification_id=fixture["verification_id"], record_sha256="b" * 64,
            conclusion=conclusion, reviewer="reviewer-a",
            reviewed_at="2026-11-17T09:00:00+00:00")
        session.add(review)
        session.flush()
        return review.id


def _artifact(factory, fixture, *, content_hash="c" * 64):
    """An artifact owned by an execution of this Run, with a recorded (not read) hash."""
    with factory.begin() as session:
        execution = AgentExecution(
            run_id=fixture["run_id"], agent_role=AgentRole.VERIFIER.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, input={}, output={})
        session.add(execution)
        session.flush()
        artifact = Artifact(
            agent_execution_id=execution.id, kind="workspace_diff",
            content_ref=str(Path(f"/tmp/memory-fixture/{uuid4()}.bin")),
            content_hash=content_hash)
        session.add(artifact)
        session.flush()
        return artifact.id


# --- the closed loop -----------------------------------------------------------------

def test_a_verified_memory_is_ingested_and_read_by_identity(memory_session_factory):
    factory = memory_session_factory
    fixture = _passed_run(factory)

    result = _ingest(factory, _candidate(fixture))

    # What the cited records establish, and separately what is known about the statement.
    assert result["source_state"] == "trusted_passed"
    assert result["content_status"] == "unverified_candidate"
    assert result["status"] == "active" and result["idempotent"] is False
    assert result["scope_kind"] == "repository" and result["scope_value"] == "example-repo"
    assert result["task_id"] == str(fixture["task_id"])
    assert result["run_id"] == str(fixture["run_id"])
    assert result["verification_id"] == str(fixture["verification_id"])
    assert result["task_review_id"] is None and result["artifact_id"] is None
    assert result["created_at"] and result["created_by"] == "operator-a"
    # The memory points at the formal record; it does not restate it as a new truth.
    row, = _rows(factory)
    assert str(row.id) == result["id"]

    by_identity = _read(factory, repository="example-repo", identity="memory-one")
    assert by_identity is not None and by_identity["id"] == result["id"]
    by_id = _read(factory, repository="example-repo", memory_id=row.id)
    assert by_id is not None and by_id["identity_key"] == "memory-one"
    # Another repository is a different scope: the memory is not visible there.
    assert _read(factory, repository="other-repo", identity="memory-one") is None
    assert _read(factory, repository="example-repo", identity="memory-two") is None


def test_a_reviewed_memory_needs_the_accepted_review_it_cites(memory_session_factory):
    factory = memory_session_factory
    fixture = _passed_run(factory)
    review_id = _accepted_review(factory, fixture)

    result = _ingest(factory, _candidate(
        fixture, identity_key="reviewed-one", source_kind="task_review",
        task_review_id=review_id))

    assert result["source_state"] == "review_accepted"
    assert result["task_review_id"] == str(review_id)
    # The person accepted the Run's patch, not this sentence.
    assert result["content_status"] == "unverified_candidate"

    rejected_fixture = _passed_run(factory)
    rejected_id = _accepted_review(factory, rejected_fixture, conclusion="rejected")
    with pytest.raises(MemoryEvidenceError, match="not accepted"):
        _ingest(factory, _candidate(
            rejected_fixture, identity_key="reviewed-two", source_kind="task_review",
            task_review_id=rejected_id))


def test_a_review_from_another_run_cannot_support_a_memory(memory_session_factory):
    factory = memory_session_factory
    fixture = _passed_run(factory)
    other = _passed_run(factory)
    foreign_review = _accepted_review(factory, other)

    with pytest.raises(MemoryEvidenceError, match="does not belong"):
        _ingest(factory, _candidate(
            fixture, source_kind="task_review", task_review_id=foreign_review))


def test_the_caller_cannot_declare_the_source_state(memory_session_factory):
    """There is no state parameter at all; it comes from the cited rows."""
    factory = memory_session_factory
    fixture = _passed_run(factory)
    review_id = _accepted_review(factory, fixture)

    with pytest.raises(MemoryEvidenceError, match="must not cite a review"):
        _ingest(factory, _candidate(fixture, task_review_id=review_id))
    with pytest.raises(MemoryEvidenceError, match="must cite the review"):
        _ingest(factory, _candidate(fixture, identity_key="no-review", source_kind="task_review"))


def test_only_a_passed_run_supports_a_memory(memory_session_factory):
    factory = memory_session_factory
    for status in (RunStatus.RUNNING, RunStatus.FAILED):
        fixture = _passed_run(factory, run_status=status)
        with pytest.raises(MemoryEvidenceError, match="only a passed Run"):
            _ingest(factory, _candidate(fixture, identity_key=f"memory-{status.value}"))


@pytest.mark.parametrize("overrides", [
    {"scope": "public"},          # not trusted-scope evidence
    {"result": VerificationResult.FAILED},
    {"suite_version": None},      # cannot say which suite it ran
])
def test_a_verification_that_is_not_trusted_evidence_is_refused(
    memory_session_factory, overrides,
):
    factory = memory_session_factory
    fixture = _passed_run(factory, **overrides)

    with pytest.raises(MemoryEvidenceError):
        _ingest(factory, _candidate(fixture))


def test_a_run_from_another_task_is_refused(memory_session_factory):
    factory = memory_session_factory
    first = _passed_run(factory)
    second = _passed_run(factory)

    with pytest.raises(MemoryEvidenceError, match="does not belong to this Task"):
        _ingest(factory, _candidate(first, run_id=second["run_id"]))
    with pytest.raises(MemoryEvidenceError, match="does not belong to this Run"):
        _ingest(factory, _candidate(first, verification_id=second["verification_id"]))


def test_a_scope_that_contradicts_the_task_repository_is_refused(
    memory_session_factory,
):
    factory = memory_session_factory
    fixture = _passed_run(factory, repo="example-repo")

    with pytest.raises(MemoryScopeError, match="does not match the Task"):
        _ingest(factory, _candidate(fixture, scope=RepositoryScope(repository="other-repo")))


def test_an_unsupported_scope_kind_is_refused(memory_session_factory):
    factory = memory_session_factory
    fixture = _passed_run(factory)

    with pytest.raises(MemoryScopeError, match="not supported"):
        RepositoryScope(repository="example-repo", kind="project")
    with pytest.raises(MemoryScopeError, match="non-blank repository"):
        RepositoryScope(repository="   ")
    with pytest.raises(MemoryScopeError, match="surrounding space"):
        RepositoryScope(repository=" example-repo ")
    assert _rows(factory) == []


def test_a_source_that_does_not_exist_is_refused(memory_session_factory):
    factory = memory_session_factory
    fixture = _passed_run(factory)

    with pytest.raises(MemoryEvidenceError, match="Task does not exist"):
        _ingest(factory, _candidate(fixture, task_id=uuid4()))
    with pytest.raises(MemoryEvidenceError, match="does not belong to this Task"):
        _ingest(factory, _candidate(fixture, run_id=uuid4()))
    with pytest.raises(MemoryEvidenceError, match="does not belong to this Run"):
        _ingest(factory, _candidate(fixture, verification_id=uuid4()))
    with pytest.raises(MemoryEvidenceError, match="Task review does not exist"):
        _ingest(factory, _candidate(fixture, source_kind="task_review",
                                    task_review_id=uuid4()))
    assert _rows(factory) == []


# --- identity, idempotency and conflicts ---------------------------------------------

def test_the_same_submission_twice_is_idempotent(memory_session_factory):
    factory = memory_session_factory
    fixture = _passed_run(factory)

    first = _ingest(factory, _candidate(fixture))
    second = _ingest(factory, _candidate(fixture))

    assert second["id"] == first["id"]
    assert second["idempotent"] is True
    assert len(_rows(factory)) == 1


def test_the_same_identity_with_different_content_is_refused(memory_session_factory):
    factory = memory_session_factory
    fixture = _passed_run(factory)
    first = _ingest(factory, _candidate(fixture))

    with pytest.raises(MemoryConflictError, match="content"):
        _ingest(factory, _candidate(fixture, content="A different statement."))

    row, = _rows(factory)
    assert str(row.id) == first["id"]
    assert row.content == "An empty prompt suffix must still be honoured exactly."


def test_the_same_identity_for_another_scope_is_refused(memory_session_factory):
    factory = memory_session_factory
    first = _passed_run(factory, repo="example-repo")
    second = _passed_run(factory, repo="other-repo")
    _ingest(factory, _candidate(first))

    with pytest.raises(MemoryConflictError) as raised:
        _ingest(factory, _candidate(second, scope=RepositoryScope(repository="other-repo")))

    message = str(raised.value)
    for field in ("scope_value", "task", "run", "verification"):
        assert field in message
    assert len(_rows(factory)) == 1


def test_identical_concurrent_ingestions_create_one_row(memory_session_factory):
    factory = memory_session_factory
    fixture = _passed_run(factory)
    barrier = Barrier(2, timeout=15)
    results = []

    def worker():
        barrier.wait()
        try:
            results.append(_ingest(factory, _candidate(fixture)))
        except MemoryError as error:  # pragma: no cover - surfaced through the assertion
            results.append({"error": str(error)})

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: worker(), range(2)))

    assert len(results) == 2
    assert all("error" not in result for result in results), results
    assert results[0]["id"] == results[1]["id"]
    assert sorted(result["idempotent"] for result in results) == [False, True]
    assert len(_rows(factory)) == 1


def test_conflicting_concurrent_ingestions_keep_the_first_row(memory_session_factory):
    factory = memory_session_factory
    fixture = _passed_run(factory)
    barrier = Barrier(2, timeout=15)
    outcomes = []

    def worker(content):
        barrier.wait()
        try:
            outcomes.append(("stored", _ingest(
                factory, _candidate(fixture, content=content))["content"]))
        except MemoryConflictError:
            outcomes.append(("conflict", None))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(worker, ("First statement.", "Second statement.")))

    assert len(_rows(factory)) == 1
    assert sorted(kind for kind, _ in outcomes) == ["conflict", "stored"]
    stored = next(content for kind, content in outcomes if kind == "stored")
    assert _rows(factory)[0].content == stored


def test_the_database_constraints_hold_independently_of_the_service(memory_session_factory):
    factory = memory_session_factory
    fixture = _passed_run(factory)
    _ingest(factory, _candidate(fixture))

    def row(**overrides):
        values = {
            "identity_key": "memory-one", "scope_kind": "repository",
            "scope_value": "example-repo", "source_kind": "verification",
            "source_state": "trusted_passed", "content_status": "unverified_candidate",
            "task_id": fixture["task_id"], "run_id": fixture["run_id"],
            "verification_id": fixture["verification_id"],
            "content": "Another statement.", "created_by": "operator-a",
        }
        values.update(overrides)
        return MemoryItem(**values)

    for broken in (
        row(),                                            # duplicate identity
        row(identity_key="retired-without-time", status="retired"),
        row(identity_key="bad-scope", scope_kind="project"),
        row(identity_key="bad-state", source_state="verified"),
        row(identity_key="review-state", source_state="review_accepted"),
        row(identity_key="verified-state", source_kind="task_review",
            task_review_id=None),
        row(identity_key="claimed-pending", content_status="pending_review"),
        row(identity_key="bad-artifact", artifact_sha256="d" * 64),
        row(identity_key="empty-content", content="   "),
        row(identity_key="unpaired-topic", topic="tests"),
        row(identity_key="blank-topic", topic=" ",
            valid_until=datetime.now(timezone.utc)),
        row(identity_key="applicability-unpaired", applicable_base_commit="a" * 40),
        row(identity_key="applicability-invalid-commit", applicable_base_commit="abc",
            applicable_environment="python-3.11"),
        row(identity_key="applicability-blank-environment", applicable_base_commit="a" * 40,
            applicable_environment="   "),
        row(identity_key="   "),
        row(identity_key="blank-creator", created_by="  "),
        row(identity_key="retired-no-operator", status="retired", retired_at=func.now()),
        row(identity_key="retired-no-reason", status="retired", retired_at=func.now(),
            retired_by="operator-b"),
    ):
        with factory() as session:
            session.add(broken)
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()


# --- retirement and non-interference -------------------------------------------------

def test_a_retired_memory_is_not_readable(memory_session_factory):
    factory = memory_session_factory
    fixture = _passed_run(factory)
    stored = _ingest(factory, _candidate(fixture))

    with factory() as session:
        retired = MemoryService(session).retire(
            _uuid_of(stored), scope=RepositoryScope(repository="example-repo"),
            operator="operator-b", reason="Superseded by a new fact")

    assert retired["status"] == "retired" and retired["idempotent"] is False
    assert _read(factory, repository="example-repo", identity="memory-one") is None
    assert _read(factory, repository="example-repo", memory_id=_uuid_of(stored)) is None
    row, = _rows(factory)
    # Retirement keeps the row and the evidence references.
    assert row.retired_by == "operator-b" and row.retire_reason
    assert row.retired_at is not None and row.verification_id == fixture["verification_id"]
    with factory() as session:
        with pytest.raises(MemoryStateError, match="already retired"):
            MemoryService(session).retire(
                row.id, scope=RepositoryScope(repository="example-repo"),
                operator="operator-b", reason="again")


def test_ingestion_never_touches_the_business_records(memory_session_factory):
    factory = memory_session_factory
    fixture = _passed_run(factory)
    review_id = _accepted_review(factory, fixture)
    before = _counts(factory)

    _ingest(factory, _candidate(fixture))
    _ingest(factory, _candidate(fixture, identity_key="reviewed", source_kind="task_review",
                                task_review_id=review_id))
    # A refused submission changes nothing at all.
    with pytest.raises(MemoryEvidenceError):
        _ingest(factory, _candidate(fixture, identity_key="refused", run_id=uuid4()))

    after = _counts(factory)
    assert after["memory_items"] == before["memory_items"] + 2
    for table in ("tasks", "runs", "verifications", "task_reviews"):
        assert after[table] == before[table]
    with factory() as session:
        assert session.get(Run, fixture["run_id"]).status is RunStatus.PASSED
        assert session.get(Task, fixture["task_id"]).status is TaskStatus.AWAITING_REVIEW


def test_a_retired_identity_cannot_be_resurrected_by_replay(memory_session_factory):
    factory = memory_session_factory
    fixture = _passed_run(factory)
    stored = _ingest(factory, _candidate(fixture))
    with factory() as session:
        MemoryService(session).retire(
            _uuid_of(stored), scope=RepositoryScope(repository="example-repo"),
            operator="operator-b", reason="No longer accurate")

    with pytest.raises(MemoryStateError, match="retired"):
        _ingest(factory, _candidate(fixture))

    row, = _rows(factory)
    assert row.status == "retired"
    assert row.retire_reason == "No longer accurate"


def test_ingest_replay_waits_for_concurrent_retirement(memory_session_factory):
    factory = memory_session_factory
    fixture = _passed_run(factory)
    original = _ingest(factory, _candidate(fixture))
    locked, release = Event(), Event()
    writer_pid = []
    def retire():
        with factory.begin() as session:
            writer_pid.append(session.scalar(select(func.pg_backend_pid())))
            item = session.get(MemoryItem, UUID(original["id"]))
            item.status = "retired"
            item.retired_at = datetime.now(timezone.utc)
            item.retired_by = "operator"
            item.retire_reason = "withdrawn"
            session.flush()
            locked.set()
            assert release.wait(15)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(retire)
        try:
            assert locked.wait(10)
            replay = pool.submit(_ingest, factory, _candidate(fixture))
            deadline = monotonic() + 10
            blocked = False
            while monotonic() < deadline and not replay.done():
                with factory() as session:
                    blocked = session.scalar(text(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE :writer = ANY(pg_blocking_pids(pid)))"), {"writer": writer_pid[0]})
                if blocked:
                    break
                sleep(0.02)
            assert blocked, "replay did not wait for the in-flight retirement"
        finally:
            release.set()
        writing.result(timeout=15)
        with pytest.raises(MemoryStateError, match="retired"):
            replay.result(timeout=15)
    assert len(_rows(factory)) == 1 and _rows(factory)[0].status == "retired"


def test_retirement_requires_the_matching_scope(memory_session_factory):
    factory = memory_session_factory
    fixture = _passed_run(factory)
    stored = _ingest(factory, _candidate(fixture))

    with factory() as session:
        with pytest.raises(MemoryScopeError, match="does not belong to this scope"):
            MemoryService(session).retire(
                _uuid_of(stored), scope=RepositoryScope(repository="other-repo"),
                operator="operator-b", reason="Wrong scope")

    row, = _rows(factory)
    assert row.status == "active" and row.retired_at is None


def _assert_ingestion_waits_for_changed_source(factory, fixture, *, change, exception, match):
    """Wait for PostgreSQL to report the actual lock dependency, without timing guesses."""
    locked, release = Event(), Event()
    writer_pid = []
    def writer():
        with factory.begin() as session:
            writer_pid.append(session.scalar(select(func.pg_backend_pid())))
            change(session)
            session.flush()
            locked.set()
            assert release.wait(15), "test did not release the source lock"
    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(writer)
        try:
            assert locked.wait(10), "writer did not acquire the source lock"
            reading = pool.submit(_ingest, factory, _candidate(fixture))
            deadline = monotonic() + 10
            blocked = False
            while monotonic() < deadline and not reading.done():
                with factory() as session:
                    blocked = session.scalar(text(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE :writer = ANY(pg_blocking_pids(pid)))"), {"writer": writer_pid[0]})
                if blocked:
                    break
                sleep(0.02)
            assert blocked, "PostgreSQL did not report the source writer blocking ingestion"
        finally:
            release.set()
        writing.result(timeout=15)
        with pytest.raises(exception, match=match):
            reading.result(timeout=15)
    assert _rows(factory) == []


def test_an_inflight_task_scope_change_is_observed_before_ingestion(memory_session_factory):
    factory = memory_session_factory
    fixture = _passed_run(factory)
    def change(session):
        session.get(Task, fixture["task_id"]).repo = "changed-repo"
    _assert_ingestion_waits_for_changed_source(factory, fixture, change=change,
                                               exception=MemoryScopeError, match="does not match")


def test_an_inflight_run_change_is_observed_before_the_memory_is_written(
    memory_session_factory,
):
    """A concurrent change commits before the blocked admission read proceeds."""
    factory = memory_session_factory
    fixture = _passed_run(factory)
    def change(session):
        session.get(Run, fixture["run_id"]).status = RunStatus.FAILED
    _assert_ingestion_waits_for_changed_source(factory, fixture, change=change,
                                               exception=MemoryEvidenceError, match="only a passed Run")
    with factory() as session:
        assert session.get(Run, fixture["run_id"]).status is RunStatus.FAILED


# --- cited artifacts ------------------------------------------------------------------

def test_a_cited_artifact_keeps_its_ownership_and_recorded_hash(memory_session_factory):
    factory = memory_session_factory
    fixture = _passed_run(factory)
    artifact_id = _artifact(factory, fixture)

    stored = _ingest(factory, _candidate(
        fixture, artifact_id=artifact_id, artifact_sha256="c" * 64))

    assert stored["artifact_id"] == str(artifact_id)
    assert stored["artifact_sha256"] == "c" * 64
    # The hash is copied from the artifact row, not from the submission.
    with factory() as session:
        row = session.get(MemoryItem, _uuid_of(stored))
        assert row.artifact_sha256 == session.get(Artifact, artifact_id).content_hash
    with pytest.raises(MemoryEvidenceError, match="does not match the submission"):
        _ingest(factory, _candidate(fixture, identity_key="wrong-hash",
                                    artifact_id=artifact_id, artifact_sha256="e" * 64))


def test_an_artifact_from_another_run_is_refused(memory_session_factory):
    factory = memory_session_factory
    fixture = _passed_run(factory)
    other = _passed_run(factory)
    foreign_artifact = _artifact(factory, other)

    with pytest.raises(MemoryEvidenceError, match="does not belong to this Run"):
        _ingest(factory, _candidate(fixture, artifact_id=foreign_artifact))
    with pytest.raises(MemoryEvidenceError, match="does not exist"):
        _ingest(factory, _candidate(fixture, identity_key="missing-artifact",
                                    artifact_id=uuid4()))


# --- the CLI --------------------------------------------------------------------------

def test_the_cli_ingests_and_reports(memory_session_factory, capsys):
    factory = memory_session_factory
    fixture = _passed_run(factory)

    exit_code = memory_main([
        "ingest", "--identity", "cli-memory", "--scope-repository", "example-repo",
        "--source-kind", "verification", "--task-id", str(fixture["task_id"]),
        "--run-id", str(fixture["run_id"]),
        "--verification-id", str(fixture["verification_id"]),
        "--content", "A statement recorded through the CLI.",
        "--created-by", "operator-a"], session_factory=factory)

    assert exit_code == 0
    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert event["event"] == "memory_ingested"
    assert event["source_state"] == "trusted_passed"
    assert event["content_status"] == "unverified_candidate"
    assert _read(factory, repository="example-repo", identity="cli-memory") is not None


def test_the_cli_refuses_an_unusable_submission(memory_session_factory, capsys):
    factory = memory_session_factory
    fixture = _passed_run(factory)

    exit_code = memory_main([
        "ingest", "--identity", "cli-refused", "--scope-repository", "elsewhere",
        "--source-kind", "verification", "--task-id", str(fixture["task_id"]),
        "--run-id", str(fixture["run_id"]),
        "--verification-id", str(fixture["verification_id"]),
        "--content", "A statement outside the Task repository.",
        "--created-by", "operator-a"], session_factory=factory)

    assert exit_code == 2
    assert "does not match the Task" in capsys.readouterr().err
    assert _rows(factory) == []


def test_the_cli_reports_a_missing_memory_as_no_result(memory_session_factory, capsys):
    factory = memory_session_factory

    exit_code = memory_main([
        "read", "--scope-repository", "example-repo", "--identity", "absent"],
        session_factory=factory)

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out.strip())["event"] == "memory_not_found"


def test_the_memory_migration_reverts_without_touching_other_tables(
    memory_session_factory,
):
    """The migration stands alone: reverting it leaves the evidence tables in place."""
    factory = memory_session_factory
    engine = factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations"))

    def apply(operation):
        with engine.begin() as connection:
            connection.execution_options(schema_translate_map=None)
            connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            with Operations.context(MigrationContext.configure(connection)):
                operation(scripts.get_revision("c4a7e2f19b30").module)

    def tables():
        with engine.begin() as connection:
            return {
                name: connection.scalar(text(
                    f"SELECT to_regclass('{schema}.{name}') IS NOT NULL"))
                for name in ("memory_items", "task_reviews", "verifications", "runs")
            }

    assert tables()["memory_items"] is True
    with engine.begin() as connection:
        connection.execution_options(schema_translate_map=None)
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        with Operations.context(MigrationContext.configure(connection)):
            scripts.get_revision("b41e9d2c8370").module.downgrade()
            scripts.get_revision("a5810e46f2c9").module.downgrade()
            scripts.get_revision("f6a92c3184d2").module.downgrade()
            scripts.get_revision("ec624a31d740").module.downgrade()
            scripts.get_revision("d18e42f70a91").module.downgrade()
    apply(lambda module: module.downgrade())
    reverted = tables()
    assert reverted["memory_items"] is False
    assert reverted["task_reviews"] and reverted["verifications"] and reverted["runs"]
    apply(lambda module: module.upgrade())
    assert tables()["memory_items"] is True
    with engine.begin() as connection:
        connection.execution_options(schema_translate_map=None)
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        with Operations.context(MigrationContext.configure(connection)):
            scripts.get_revision("d18e42f70a91").module.upgrade()
            scripts.get_revision("ec624a31d740").module.upgrade()
            scripts.get_revision("f6a92c3184d2").module.upgrade()
            scripts.get_revision("a5810e46f2c9").module.upgrade()
            scripts.get_revision("b41e9d2c8370").module.upgrade()


def test_the_cli_retires_only_inside_the_declared_scope(memory_session_factory, capsys):
    factory = memory_session_factory
    fixture = _passed_run(factory)
    stored = _ingest(factory, _candidate(fixture))

    wrong_scope = memory_main([
        "retire", "--scope-repository", "other-repo", "--memory-id", stored["id"],
        "--operator", "operator-b", "--reason", "Wrong scope"], session_factory=factory)
    assert wrong_scope == 2
    assert "does not belong to this scope" in capsys.readouterr().err
    assert _read(factory, repository="example-repo", identity="memory-one") is not None

    exit_code = memory_main([
        "retire", "--scope-repository", "example-repo", "--memory-id", stored["id"],
        "--operator", "operator-b", "--reason", "Superseded"], session_factory=factory)
    assert exit_code == 0
    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert event["event"] == "memory_retired" and event["status"] == "retired"
    assert _read(factory, repository="example-repo", identity="memory-one") is None
