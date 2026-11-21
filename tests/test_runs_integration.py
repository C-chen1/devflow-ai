from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import importlib.util
from hashlib import sha256
from pathlib import Path
from queue import Queue
from threading import Barrier
from time import monotonic, sleep
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, inspect, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError

from devflow_ai.coordinator import (
    AgentExecutionPendingToolsError, AgentExecutionStateConflictError, GraphStepBudgetExceededError, RunCoordinator,
    RunExecutionDeniedError, ToolArtifactResult, ToolCallResult,
    ToolCallStateConflictError,
)
from devflow_ai.enums import AgentExecutionStatus, AgentRole, RunStatus, ToolCallStatus
from devflow_ai.models import AgentExecution, Artifact, Run, Task, ToolCall
from devflow_ai.repositories import RunRepository, ToolCallRepository
from devflow_ai.services import TaskService


def load_run_migration(filename="b312290a866d_create_runs_table.py"):
    path = Path(__file__).resolve().parents[1] / "migrations/versions" / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    return revision


def upgrade_handoff_link_schema():
    # These two migrations only depend on agent_executions being present.
    load_run_migration("62eae84cebd1_create_handoffs.py").upgrade()
    load_run_migration("97f3a25cb190_link_receiver_to_handoff.py").upgrade()


def test_tool_call_migration_roundtrip_and_pending_result(run_session_factory, tmp_path):
    run_id = create_repository_run(run_session_factory)
    engine = run_session_factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    agent_revision = load_run_migration("c7e1a29b4d63_create_agent_executions.py")
    tool_revision = load_run_migration("d4f63c192ba8_create_tool_calls.py")
    ownership_revision = load_run_migration("e8c27b14a693_link_tool_calls_to_runs.py")
    with engine.begin() as connection:
        connection.execution_options(schema_translate_map=None)
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        with Operations.context(MigrationContext.configure(connection)):
            agent_revision.upgrade()
            tool_revision.upgrade()
            tool_revision.downgrade()
            assert not inspect(connection).has_table("tool_calls", schema=schema)
            tool_revision.upgrade()
            load_run_migration("f83ab2d16c49_create_artifacts_table.py").upgrade()
            load_run_migration("ab6e1c30f42d_link_artifacts_to_tool_calls.py").upgrade()
            legacy_execution_id = connection.scalar(text("""
                INSERT INTO agent_executions (run_id, agent_role, status, input)
                VALUES (:run_id, 'explorer', 'running', '{}') RETURNING id
            """), {"run_id": run_id})
            legacy_call_id = connection.scalar(text("""
                INSERT INTO tool_calls (agent_execution_id, tool_name, args, status)
                VALUES (:execution_id, 'legacy_read', '{}', 'requested') RETURNING id
            """), {"execution_id": legacy_execution_id})
            ownership_revision.upgrade()
            assert connection.scalar(text("SELECT run_id FROM tool_calls WHERE id = :id"), {
                "id": legacy_call_id,
            }) == run_id
            ownership_revision.downgrade()
            assert connection.scalar(text("SELECT agent_execution_id FROM tool_calls WHERE id = :id"), {
                "id": legacy_call_id,
            }) == legacy_execution_id
            ownership_revision.upgrade()
            load_run_migration("4b79c6e21a50_allow_worker_tool_artifacts.py").upgrade()
            upgrade_handoff_link_schema()

    with run_session_factory.begin() as session:
        # Install a test-only Policy snapshot for the fixture Run.
        task = session.get(Task, session.get(Run, run_id).task_id)
        snapshot = task.policy_snapshot
        task.policy_version = "tool-permission-test-v1"
        task.policy_snapshot = {
            **snapshot,
            "policy_version": task.policy_version,
            "permissions": {"allowed_tools_by_role": {
                **snapshot["permissions"]["allowed_tools_by_role"],
                "EXPLORER": ["read_file", "run_command", "retired_tool"],
                "CODER": ["apply_patch"],
            }},
        }
    with run_session_factory() as session:
        execution_id = RunCoordinator(session).start_agent_execution(
            run_id, "fixture-worker", role=AgentRole.EXPLORER, agent_input={"issue": "inspect files"},
        )
    with run_session_factory.begin() as session:
        execution = session.get(AgentExecution, execution_id)
        assert (execution.agent_role, execution.status, execution.input) == (
            "explorer", AgentExecutionStatus.RUNNING, {"issue": "inspect files"},
        )
        call_id = ToolCallRepository(session).add_requested(
            execution_id, "read_file", {"path": "README.md"}, run_id=run_id,
        )

    with run_session_factory() as session:
        call = session.get(ToolCall, call_id)
        assert call.status == ToolCallStatus.REQUESTED
        assert call.run_id == run_id
        assert call.agent_execution_id == execution_id
        assert call.tool_name == "read_file" and call.args == {"path": "README.md"}
        assert call.exit_code is None and call.stdout_excerpt is None
        session.add(ToolCall(
            run_id=run_id, agent_execution_id=uuid4(), tool_name="read_file",
            args={}, status=ToolCallStatus.REQUESTED,
        ))
        with pytest.raises(IntegrityError) as exc_info:
            session.flush()
        assert exc_info.value.orig.sqlstate == "23503"

    with run_session_factory() as session:
        rolled_back_id = ToolCallRepository(session).add_requested(execution_id, "read_file", {}, run_id=run_id)
        assert session.get(ToolCall, rolled_back_id) is not None
        with run_session_factory() as observer:
            assert observer.get(ToolCall, rolled_back_id) is None
        session.rollback()
    with run_session_factory() as observer:
        assert observer.get(ToolCall, rolled_back_id) is None

    with run_session_factory() as session:
        repository = ToolCallRepository(session)
        with pytest.raises(ValueError, match="Unsupported tool result status"):
            repository.save_result(call_id, status=ToolCallStatus.UNKNOWN_RESULT)
        assert repository.save_result(call_id, status=ToolCallStatus.FAILED, exit_code=1) == 1
        session.rollback()

    with run_session_factory.begin() as session:
        repository = ToolCallRepository(session)
        assert repository.save_result(
            call_id, status=ToolCallStatus.SUCCEEDED, stdout_excerpt="content",
            stderr_excerpt="", exit_code=0, duration_ms=12,
        ) == 1

    with run_session_factory.begin() as session:
        repository = ToolCallRepository(session)
        assert repository.save_result(
            call_id, status=ToolCallStatus.FAILED, stderr_excerpt="duplicate", exit_code=1,
        ) == 0
        call = session.get(ToolCall, call_id)
        assert call.status == ToolCallStatus.SUCCEEDED
        assert (call.stdout_excerpt, call.stderr_excerpt, call.exit_code, call.duration_ms) == (
            "content", "", 0, 12,
        )

    other_run_id = create_repository_run(run_session_factory)
    registered_tools = {"read_file", "run_command", "apply_patch"}

    def execute(registered_id):
        with run_session_factory() as observer:
            registered = observer.get(ToolCall, registered_id)
            assert registered.agent_execution_id == execution_id
            assert registered.status == ToolCallStatus.REQUESTED
        return ToolCallResult(
            status=ToolCallStatus.SUCCEEDED, stdout_excerpt="content from tool",
            stderr_excerpt="", exit_code=0, duration_ms=12,
        )

    with run_session_factory() as session:
        coordinator = RunCoordinator(session)
        for invalid_run, invalid_execution in ((other_run_id, execution_id), (run_id, uuid4())):
            with pytest.raises(RunExecutionDeniedError):
                coordinator.execute_agent_tool_call(
                    invalid_run, "fixture-worker", agent_execution_id=invalid_execution,
                    tool_name="read_file", args={},
                    registered_tools=registered_tools,
                    execute=lambda _: pytest.fail("Unauthorized tool started"),
                )
        registered_id = coordinator.execute_agent_tool_call(
            run_id, "fixture-worker", agent_execution_id=execution_id,
            tool_name="read_file", args={}, registered_tools=registered_tools, execute=execute,
        )
    with run_session_factory() as observer:
        assert observer.scalar(select(func.count()).select_from(ToolCall)) == 3
        registered = observer.get(ToolCall, registered_id)
        assert registered.status == ToolCallStatus.SUCCEEDED
        assert (registered.stdout_excerpt, registered.exit_code, registered.duration_ms) == (
            "content from tool", 0, 12,
        )

    def execute_with_already_saved_result(conflict_id):
        with run_session_factory.begin() as other:
            assert ToolCallRepository(other).save_result(
                conflict_id, status=ToolCallStatus.FAILED, exit_code=1,
            ) == 1
        return execute_output

    execute_output = ToolCallResult(status=ToolCallStatus.SUCCEEDED, exit_code=0)
    with run_session_factory() as session:
        with pytest.raises(ToolCallStateConflictError):
            RunCoordinator(session).execute_agent_tool_call(
                run_id, "fixture-worker", agent_execution_id=execution_id,
                tool_name="run_command", args={}, registered_tools=registered_tools,
                execute=execute_with_already_saved_result,
            )
        assert not session.in_transaction()
        conflict = session.scalar(select(ToolCall).where(ToolCall.tool_name == "run_command"))
        assert (conflict.status, conflict.exit_code) == (ToolCallStatus.FAILED, 1)

    for name, reason in (
        ("apply_patch", "TOOL_NOT_ALLOWED_FOR_ROLE"),
        ("retired_tool", "TOOL_NOT_REGISTERED"),
    ):
        with run_session_factory() as session:
            denied_id = RunCoordinator(session).execute_agent_tool_call(
                run_id, "fixture-worker", agent_execution_id=execution_id,
                tool_name=name, args={"agent_role": "CODER"}, registered_tools=registered_tools,
                execute=lambda _: pytest.fail("Denied tool started"),
            )
        with run_session_factory() as observer:
            denied = observer.get(ToolCall, denied_id)
            assert (denied.status, denied.reason, denied.exit_code) == (
                ToolCallStatus.DENIED, reason, None,
            )
            assert denied.args == {"agent_role": "CODER"}

    workspace_root = tmp_path / "workspace"
    artifact_root = tmp_path / "artifacts"
    workspace_root.mkdir()
    artifact_root.mkdir()
    for name, content, status, excerpt, reason in (
        ("bad.txt", b"a\xffb", ToolCallStatus.FAILED, None, "INVALID_UTF8"),
        ("nul.txt", b"a\x00b", ToolCallStatus.FAILED, None, "UNSUPPORTED_TEXT_CONTENT"),
        ("long.txt", b"abcd", ToolCallStatus.SUCCEEDED, "abc", "OUTPUT_TRUNCATED"),
        ("short.txt", b"ab", ToolCallStatus.SUCCEEDED, "ab", None),
        ("../secret.txt", b"secret", ToolCallStatus.DENIED, None, "WORKSPACE_PATH_DENIED"),
        ("../absent.txt", None, ToolCallStatus.DENIED, None, "WORKSPACE_PATH_DENIED"),
        ("missing.txt", None, ToolCallStatus.FAILED, None, "FILE_NOT_FOUND"),
    ):
        if content is not None:
            (workspace_root / name).write_bytes(content)
        with run_session_factory() as session:
            receipt = RunCoordinator(session).execute_read_file_with_receipt(
                run_id, "fixture-worker", agent_execution_id=execution_id,
                workspace_root=workspace_root, relative_path=name, max_bytes=3,
                artifact_root=artifact_root, artifact_chunk_size=2,
            )
            assert not session.in_transaction()
        read_id = receipt.tool_call_id
        assert (receipt.status, receipt.preview, receipt.reason) == (status, excerpt, reason)
        assert receipt.truncated == (reason == "OUTPUT_TRUNCATED")
        with run_session_factory() as observer:
            read_call = observer.get(ToolCall, read_id)
            assert (read_call.status, read_call.stdout_excerpt, read_call.reason) == (
                status, excerpt, reason,
            )
            assert read_call.tool_name == "read_file" and read_call.args["path"] == name
            if status == ToolCallStatus.SUCCEEDED:
                # What was actually read is recorded next to the request, not instead of it.
                assert read_call.args["observed"]["start_line"] == 1
                assert read_call.args["observed"]["byte_truncated"] == (reason == "OUTPUT_TRUNCATED")
            else:
                assert "observed" not in read_call.args
            assert read_call.exit_code is None
            artifact = observer.scalars(
                select(Artifact).where(Artifact.tool_call_id == read_id)
            ).one_or_none()
            if reason == "OUTPUT_TRUNCATED":
                assert receipt.artifact_ids == (artifact.id,)
                assert artifact.agent_execution_id == execution_id
                assert artifact.kind == "tool_output"
                assert artifact.content_hash == sha256(content).hexdigest()
                assert Path(artifact.content_ref) == artifact_root / f"{artifact.id}.bin"
                assert Path(artifact.content_ref).read_bytes() == content
            else:
                assert artifact is None
                assert receipt.artifact_ids == ()
    assert len(list(artifact_root.iterdir())) == 1

    duplicate = ToolArtifactResult(
        id=uuid4(), kind="tool_output", content_ref="duplicate.bin", content_hash="a" * 64,
    )
    requested_ids = []

    def execute_with_duplicate_artifacts(call_id):
        requested_ids.append(call_id)
        return ToolCallResult(status=ToolCallStatus.SUCCEEDED, artifacts=(duplicate, duplicate))

    with run_session_factory() as session, pytest.raises(IntegrityError) as exc_info:
        RunCoordinator(session).execute_agent_tool_call(
            run_id, "fixture-worker", agent_execution_id=execution_id, tool_name="read_file",
            args={}, registered_tools=registered_tools, execute=execute_with_duplicate_artifacts,
        )
    assert exc_info.value.orig.sqlstate == "23505"
    with run_session_factory() as observer:
        assert observer.get(ToolCall, requested_ids[0]).status == ToolCallStatus.REQUESTED
        assert observer.get(Artifact, duplicate.id) is None

    with pytest.raises(IntegrityError) as exc_info:
        with run_session_factory.begin() as session:
            ToolCallRepository(session).add_requested(execution_id, "read_file", {}, run_id=other_run_id)
    assert exc_info.value.orig.diag.constraint_name == "fk_tool_calls_agent_execution_run"

    with pytest.raises(IntegrityError) as exc_info:
        with run_session_factory.begin() as session:
            ToolCallRepository(session).add_requested(None, "git_clone", {}, run_id=uuid4())
    assert exc_info.value.orig.diag.constraint_name == "fk_tool_calls_run_id"

    with run_session_factory.begin() as session:
        worker_call_id = ToolCallRepository(session).add_requested(None, "git_clone", {}, run_id=run_id)
    with pytest.raises(IntegrityError) as exc_info:
        with engine.begin() as connection:
            connection.execution_options(schema_translate_map=None)
            connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            with Operations.context(MigrationContext.configure(connection)):
                ownership_revision.downgrade()
    assert exc_info.value.orig.sqlstate == "23502"
    with run_session_factory() as observer:
        calls = {
            call.id: call.agent_execution_id
            for call in observer.scalars(select(ToolCall).where(ToolCall.run_id == run_id))
        }
        assert calls[legacy_call_id] == legacy_execution_id
        assert calls[worker_call_id] is None

    def finish(session, execution_id):
        RunCoordinator(session).finish_agent_execution(
            run_id, "fixture-worker", agent_execution_id=execution_id,
            status=AgentExecutionStatus.SUCCEEDED, output={"files": ["README.md"]}, llm_usage=None,
        )

    with run_session_factory() as session:
        coordinator = RunCoordinator(session)
        active_execution_id = coordinator.start_agent_execution(
            run_id, "fixture-worker", role=AgentRole.EXPLORER, agent_input={},
        )

        def attempt_to_finish_while_tool_runs(tool_call_id):
            with run_session_factory() as competing_session:
                with pytest.raises(AgentExecutionPendingToolsError):
                    finish(competing_session, active_execution_id)
                assert competing_session.get(AgentExecution, active_execution_id).status == AgentExecutionStatus.RUNNING
                assert competing_session.get(ToolCall, tool_call_id).status == ToolCallStatus.REQUESTED
            return ToolCallResult(status=ToolCallStatus.SUCCEEDED)

        coordinator.execute_agent_tool_call(
            run_id, "fixture-worker", agent_execution_id=active_execution_id,
            tool_name="read_file", args={}, registered_tools=registered_tools,
            execute=attempt_to_finish_while_tool_runs,
        )
        finish(session, active_execution_id)
        late_call_id = coordinator.execute_agent_tool_call(
            run_id, "fixture-worker", agent_execution_id=active_execution_id,
            tool_name="read_file", args={}, registered_tools=registered_tools,
            execute=lambda _: pytest.fail("Terminal Agent started a late tool"),
        )
    with run_session_factory() as observer:
        assert observer.get(AgentExecution, active_execution_id).status == AgentExecutionStatus.SUCCEEDED
        late_call = observer.get(ToolCall, late_call_id)
        assert (late_call.status, late_call.reason, late_call.exit_code) == (
            ToolCallStatus.DENIED, "AGENT_EXECUTION_NOT_RUNNING", None,
        )


def test_agent_execution_migration_roundtrip_and_run_foreign_key(run_session_factory):
    run_id = create_repository_run(run_session_factory)
    engine = run_session_factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    path = Path(__file__).resolve().parents[1] / "migrations/versions/c7e1a29b4d63_create_agent_executions.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)

    with engine.begin() as connection:
        # Run Alembic with the same unqualified DDL as the normal entry point.
        connection.execution_options(schema_translate_map=None)
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
            revision.downgrade()
            assert not inspect(connection).has_table("agent_executions", schema=schema)
            revision.upgrade()
            upgrade_handoff_link_schema()

    with run_session_factory.begin() as session:
        execution = AgentExecution(
            run_id=run_id, agent_role="explorer", status=AgentExecutionStatus.RUNNING,
            input={"issue": "inspect repository"},
        )
        session.add(execution)
        session.flush()
        execution_id = execution.id
        assert execution.output is None and execution.llm_usage is None
        assert execution.created_at.utcoffset() is not None

    with run_session_factory() as session:
        assert session.get(AgentExecution, execution_id).status == AgentExecutionStatus.RUNNING
        session.add(AgentExecution(
            run_id=uuid4(), agent_role="explorer", status=AgentExecutionStatus.RUNNING, input={},
        ))
        with pytest.raises(IntegrityError) as exc_info:
            session.flush()
        assert exc_info.value.orig.sqlstate == "23503"


def create_parent_task(session_factory):
    with session_factory() as session:
        task, _ = TaskService(session).create_task(
            title="Run constraint test", description="Verify Run state constraints",
            repo="example-repo", idempotency_key=uuid4(),
        )
        return task.id

def make_run(**fields):
    # Test-only owner/lease values; five minutes is not a production lease policy.
    now = datetime.now(timezone.utc)
    defaults = {"worker_id": "fixture-worker", "lease_expires_at": now + timedelta(minutes=5)}
    if fields.get("status") in (RunStatus.FAILED, RunStatus.PASSED):
        defaults.update(
            last_heartbeat_at=now, started_at=now, finished_at=now,
            created_at=now, updated_at=now,
        )
    return Run(**(defaults | fields))


def test_concurrent_agent_starts_share_run_graph_step_budget(run_session_factory):
    run_id = create_repository_run(run_session_factory)
    engine = run_session_factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    with engine.begin() as connection:
        connection.execution_options(schema_translate_map=None)
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        with Operations.context(MigrationContext.configure(connection)):
            load_run_migration("c7e1a29b4d63_create_agent_executions.py").upgrade()
            upgrade_handoff_link_schema()

    with run_session_factory.begin() as session:
        task = session.get(Task, session.get(Run, run_id).task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {
            **snapshot, "budget": {**snapshot["budget"], "max_graph_steps_per_run": 2},
        }
    with run_session_factory() as session:
        coordinator = RunCoordinator(session)
        with pytest.raises(RunExecutionDeniedError):
            coordinator.start_agent_execution(run_id, "different-worker", role=AgentRole.EXPLORER, agent_input={})
        first_id = coordinator.start_agent_execution(
            run_id, "fixture-worker", role=AgentRole.EXPLORER, agent_input={"attempt": 1},
        )

    barrier = Barrier(2, timeout=5)

    def start(attempt):
        with run_session_factory() as session:
            barrier.wait()
            try:
                return RunCoordinator(session).start_agent_execution(
                    run_id, "fixture-worker", role=AgentRole.EXPLORER, agent_input={"attempt": attempt},
                )
            except GraphStepBudgetExceededError:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(start, attempt) for attempt in (2, 3)]
        results = [future.result(timeout=10) for future in futures]
    assert results.count(None) == 1
    winner = next(execution_id for execution_id in results if execution_id is not None)
    assert winner != first_id
    with run_session_factory() as session:
        executions = session.scalars(select(AgentExecution).where(AgentExecution.run_id == run_id)).all()
        assert {execution.id for execution in executions} == {first_id, winner}
        assert session.get(AgentExecution, first_id).input == {"attempt": 1}
        assert session.get(AgentExecution, winner).input["attempt"] in (2, 3)


def test_agent_execution_completion_commits_one_terminal_result(run_session_factory):
    run_id = create_repository_run(run_session_factory)
    other_run_id = create_repository_run(run_session_factory)
    engine = run_session_factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    with engine.begin() as connection:
        connection.execution_options(schema_translate_map=None)
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        with Operations.context(MigrationContext.configure(connection)):
            load_run_migration("c7e1a29b4d63_create_agent_executions.py").upgrade()
            load_run_migration("d4f63c192ba8_create_tool_calls.py").upgrade()
            load_run_migration("e8c27b14a693_link_tool_calls_to_runs.py").upgrade()
            upgrade_handoff_link_schema()

    with run_session_factory() as session:
        coordinator = RunCoordinator(session)
        execution_id = coordinator.start_agent_execution(
            run_id, "fixture-worker", role=AgentRole.EXPLORER, agent_input={"goal": "inspect"},
        )
        with pytest.raises(AgentExecutionStateConflictError):
            coordinator.finish_agent_execution(
                other_run_id, "fixture-worker", agent_execution_id=execution_id,
                status=AgentExecutionStatus.FAILED, output=None, llm_usage=None,
            )
        with pytest.raises(RunExecutionDeniedError):
            coordinator.finish_agent_execution(
                run_id, "different-worker", agent_execution_id=execution_id,
                status=AgentExecutionStatus.FAILED, output=None, llm_usage=None,
            )
        with pytest.raises(ValueError, match="SUCCEEDED or FAILED"):
            coordinator.finish_agent_execution(
                run_id, "fixture-worker", agent_execution_id=execution_id,
                status=AgentExecutionStatus.RUNNING, output=None, llm_usage=None,
            )

    results = {
        AgentExecutionStatus.SUCCEEDED: ({"files": ["auth.py"]}, {"input_tokens": 18}),
        AgentExecutionStatus.FAILED: ({"reason": "contract error"}, None),
    }
    barrier = Barrier(2, timeout=5)

    def finish(status):
        with run_session_factory() as session:
            output, usage = results[status]
            barrier.wait()
            try:
                RunCoordinator(session).finish_agent_execution(
                    run_id, "fixture-worker", agent_execution_id=execution_id,
                    status=status, output=output, llm_usage=usage,
                )
            except AgentExecutionStateConflictError:
                return None
            return status

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(finish, status) for status in results]
        outcomes = [future.result(timeout=10) for future in futures]
    assert outcomes.count(None) == 1
    winning_status = next(status for status in outcomes if status is not None)
    with run_session_factory() as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution.status == winning_status
        assert (execution.output, execution.llm_usage) == results[winning_status]
        assert execution.input == {"goal": "inspect"}


def test_artifact_migration_roundtrip_and_ownership_constraints(run_session_factory):
    run_id = create_repository_run(run_session_factory)
    engine = run_session_factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    agent_revision = load_run_migration("c7e1a29b4d63_create_agent_executions.py")
    tool_revision = load_run_migration("d4f63c192ba8_create_tool_calls.py")
    artifact_revision = load_run_migration("f83ab2d16c49_create_artifacts_table.py")
    link_revision = load_run_migration("ab6e1c30f42d_link_artifacts_to_tool_calls.py")
    worker_artifact_revision = load_run_migration("4b79c6e21a50_allow_worker_tool_artifacts.py")
    with engine.begin() as connection:
        connection.execution_options(schema_translate_map=None)
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        with Operations.context(MigrationContext.configure(connection)):
            agent_revision.upgrade()
            tool_revision.upgrade()
            artifact_revision.upgrade()
            artifact_revision.downgrade()
            assert not inspect(connection).has_table("artifacts", schema=schema)
            assert inspect(connection).has_table("tool_calls", schema=schema)
            artifact_revision.upgrade()
            legacy_execution_id = connection.scalar(text("""
                INSERT INTO agent_executions (run_id, agent_role, status, input)
                VALUES (:run_id, 'explorer', 'running', '{}')
                RETURNING id
            """), {"run_id": run_id})
            legacy_artifact_id = connection.scalar(text("""
                INSERT INTO artifacts (agent_execution_id, kind, content_ref, content_hash)
                VALUES (:execution_id, 'analysis', 'legacy.txt', :digest)
                RETURNING id
            """), {"execution_id": legacy_execution_id, "digest": "a" * 64})
            link_revision.upgrade()
            link_revision.downgrade()
            assert "tool_call_id" not in {
                column["name"] for column in inspect(connection).get_columns("artifacts", schema=schema)
            }
            link_revision.upgrade()
            load_run_migration("e8c27b14a693_link_tool_calls_to_runs.py").upgrade()
            worker_artifact_revision.upgrade()
            worker_artifact_revision.downgrade()
            worker_artifact_revision.upgrade()
            upgrade_handoff_link_schema()

    with run_session_factory.begin() as session:
        execution = AgentExecution(
            run_id=run_id, agent_role="explorer", status=AgentExecutionStatus.RUNNING, input={},
        )
        session.add(execution)
        session.flush()
        artifact = Artifact(
            agent_execution_id=execution.id, kind="analysis",
            content_ref="analysis.txt", content_hash="a" * 64,
        )
        session.add(artifact)
        session.flush()
        artifact_id = artifact.id
        execution_id = execution.id
        tool_call_id = ToolCallRepository(session).add_requested(execution_id, "run_command", {}, run_id=run_id)
        worker_call_id = ToolCallRepository(session).add_requested(None, "git_clone", {}, run_id=run_id)
        worker_artifact = Artifact(
            tool_call_id=worker_call_id, kind="tool_stderr",
            content_ref="clone-stderr.txt", content_hash="d" * 64,
        )
        session.add(worker_artifact)
        session.flush()
        worker_artifact_id = worker_artifact.id
        session.add_all([
            Artifact(
                agent_execution_id=execution_id, tool_call_id=tool_call_id, kind="analysis",
                content_ref=ref, content_hash="b" * 64,
            )
            for ref in ("stdout.txt", "stderr.txt")
        ])

    with run_session_factory() as observer:
        artifact = observer.get(Artifact, artifact_id)
        assert artifact.agent_execution_id == execution_id
        assert artifact.content_hash == "a" * 64
        assert artifact.created_at.utcoffset() is not None
        assert artifact.tool_call_id is None
        legacy_artifact = observer.get(Artifact, legacy_artifact_id)
        assert legacy_artifact.content_ref == "legacy.txt" and legacy_artifact.tool_call_id is None
        refs = observer.scalars(select(Artifact.content_ref).where(Artifact.tool_call_id == tool_call_id))
        assert set(refs) == {"stdout.txt", "stderr.txt"}
        worker_artifact = observer.get(Artifact, worker_artifact_id)
        assert worker_artifact.agent_execution_id is None
        assert observer.get(ToolCall, worker_artifact.tool_call_id).run_id == run_id

    for invalid_call_id, constraint in (
        (None, "ck_artifacts_has_owner"),
        (uuid4(), "fk_artifacts_tool_call_id"),
    ):
        with pytest.raises(IntegrityError) as exc_info:
            with run_session_factory.begin() as session:
                session.add(Artifact(
                    tool_call_id=invalid_call_id, kind="tool_stderr",
                    content_ref="invalid.txt", content_hash="c" * 64,
                ))
        assert exc_info.value.orig.diag.constraint_name == constraint

    with pytest.raises(IntegrityError) as exc_info:
        with engine.begin() as connection:
            connection.execution_options(schema_translate_map=None)
            connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            with Operations.context(MigrationContext.configure(connection)):
                worker_artifact_revision.downgrade()
    assert exc_info.value.orig.sqlstate == "23502"
    with run_session_factory() as observer:
        assert observer.get(Artifact, worker_artifact_id).tool_call_id == worker_call_id

    with pytest.raises(IntegrityError) as exc_info:
        with run_session_factory.begin() as session:
            session.add(Artifact(
                agent_execution_id=legacy_execution_id, tool_call_id=tool_call_id, kind="analysis",
                content_ref="wrong-owner.txt", content_hash="c" * 64,
            ))
    assert exc_info.value.orig.diag.constraint_name == "fk_artifacts_tool_call_agent_execution"

    with pytest.raises(IntegrityError) as exc_info:
        with run_session_factory.begin() as session:
            session.add(Artifact(
                agent_execution_id=uuid4(), kind="analysis",
                content_ref="orphan.txt", content_hash="b" * 64,
            ))
    assert exc_info.value.orig.sqlstate == "23503"


def create_repository_run(session_factory):
    task_id = create_parent_task(session_factory)
    with session_factory() as session:
        run = RunRepository(session).add(make_run(
            task_id=task_id, attempt_number=1, status=RunStatus.RUNNING,
            creation_idempotency_key=uuid4().hex,
        ))
        run_id = run.id
        session.commit()
    return run_id


def test_run_history_and_other_tasks_do_not_block_active_run(run_session_factory):
    first_task_id = create_parent_task(run_session_factory)
    second_task_id = create_parent_task(run_session_factory)
    with run_session_factory() as session:
        session.add_all([
            make_run(creation_idempotency_key=uuid4().hex, task_id=first_task_id, attempt_number=1, status=RunStatus.FAILED),
            make_run(creation_idempotency_key=uuid4().hex, task_id=first_task_id, attempt_number=2, status=RunStatus.FAILED),
            make_run(creation_idempotency_key=uuid4().hex, task_id=first_task_id, attempt_number=3, status=RunStatus.PASSED),
            make_run(creation_idempotency_key=uuid4().hex, task_id=first_task_id, attempt_number=4, status=RunStatus.RUNNING),
            make_run(creation_idempotency_key=uuid4().hex, task_id=second_task_id, attempt_number=1, status=RunStatus.RUNNING),
        ])
        session.commit()
    with run_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 5
        rows = session.scalars(select(Run).where(Run.task_id == first_task_id).order_by(Run.attempt_number)).all()
        assert [row.status for row in rows] == [
            RunStatus.FAILED, RunStatus.FAILED, RunStatus.PASSED, RunStatus.RUNNING,
        ]


def test_second_running_run_for_same_task_is_rejected(run_session_factory):
    task_id = create_parent_task(run_session_factory)
    with run_session_factory() as session:
        session.add(make_run(creation_idempotency_key=uuid4().hex, task_id=task_id, attempt_number=1, status=RunStatus.RUNNING))
        session.commit()
    with run_session_factory() as session:
        with pytest.raises(IntegrityError) as exc_info:
            with session.begin():
                session.add(make_run(creation_idempotency_key=uuid4().hex, task_id=task_id, attempt_number=2, status=RunStatus.RUNNING))
                session.flush()
        assert exc_info.value.orig.sqlstate == "23505"
        assert exc_info.value.orig.diag.constraint_name == "uq_runs_one_running_per_task"
    with run_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 1


def test_updating_history_to_running_cannot_bypass_active_run_index(run_session_factory):
    task_id = create_parent_task(run_session_factory)
    with run_session_factory() as session:
        session.add_all([
            make_run(creation_idempotency_key=uuid4().hex, task_id=task_id, attempt_number=1, status=RunStatus.FAILED),
            make_run(creation_idempotency_key=uuid4().hex, task_id=task_id, attempt_number=2, status=RunStatus.RUNNING),
        ])
        session.commit()
    with run_session_factory() as session:
        with pytest.raises(IntegrityError) as exc_info:
            with session.begin():
                session.execute(update(Run).where(
                    Run.task_id == task_id, Run.attempt_number == 1,
                ).values(status=RunStatus.RUNNING))
        assert exc_info.value.orig.diag.constraint_name == "uq_runs_one_running_per_task"
    with run_session_factory() as session:
        statuses = session.scalars(select(Run.status).where(Run.task_id == task_id).order_by(Run.attempt_number)).all()
        assert statuses == [RunStatus.FAILED, RunStatus.RUNNING]


@pytest.mark.parametrize("finished_status", [RunStatus.FAILED, RunStatus.PASSED])
def test_finished_run_releases_active_slot(run_session_factory, finished_status):
    task_id = create_parent_task(run_session_factory)
    with run_session_factory() as session:
        session.add(make_run(creation_idempotency_key=uuid4().hex, task_id=task_id, attempt_number=1, status=RunStatus.RUNNING))
        session.commit()
    with run_session_factory() as session:
        # Index behavior only: this is not a RunCoordinator or recovery flow.
        session.execute(update(Run).where(Run.task_id == task_id).values(
            status=finished_status, finished_at=func.now(),
        ))
        session.add(make_run(creation_idempotency_key=uuid4().hex, task_id=task_id, attempt_number=2, status=RunStatus.RUNNING))
        session.commit()
    with run_session_factory() as session:
        statuses = session.scalars(select(Run.status).where(Run.task_id == task_id).order_by(Run.attempt_number)).all()
        assert statuses == [finished_status, RunStatus.RUNNING]


def test_run_creation_key_is_unique_within_each_task(run_session_factory):
    first_task_id = create_parent_task(run_session_factory)
    second_task_id = create_parent_task(run_session_factory)
    key = "same-creation-request"
    with run_session_factory() as session:
        # Terminal fixture rows keep the active-Run index out of this test.
        session.add_all([
            make_run(task_id=first_task_id, attempt_number=1, status=RunStatus.FAILED, creation_idempotency_key=key),
            make_run(task_id=second_task_id, attempt_number=1, status=RunStatus.FAILED, creation_idempotency_key=key),
        ])
        session.commit()
    with run_session_factory() as session:
        with pytest.raises(IntegrityError) as exc_info:
            with session.begin():
                session.add(make_run(
                    task_id=first_task_id, attempt_number=2,
                    status=RunStatus.FAILED, creation_idempotency_key=key,
                ))
                session.flush()
        assert exc_info.value.orig.sqlstate == "23505"
        assert exc_info.value.orig.diag.constraint_name == "uq_runs_task_creation_idempotency_key"
    with run_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 2


@pytest.mark.parametrize("explicit_null", [True, False])
def test_run_creation_key_is_required_without_generated_default(run_session_factory, explicit_null):
    task_id = create_parent_task(run_session_factory)
    fields = {"task_id": task_id, "attempt_number": 1, "status": RunStatus.RUNNING}
    if explicit_null:
        fields["creation_idempotency_key"] = None
    with run_session_factory() as session:
        with pytest.raises(IntegrityError) as exc_info:
            with session.begin():
                session.add(make_run(**fields))
                session.flush()
        assert exc_info.value.orig.sqlstate == "23502"
        assert exc_info.value.orig.diag.column_name == "creation_idempotency_key"


def test_run_time_defaults_are_aware_and_unfinished_run_has_no_finish_time(run_session_factory):
    task_id = create_parent_task(run_session_factory)
    with run_session_factory() as session:
        # Omit timestamp defaults so PostgreSQL must provide them.
        run = Run(
            task_id=task_id, attempt_number=1, status=RunStatus.RUNNING,
            creation_idempotency_key=uuid4().hex, worker_id="default-time-test",
            lease_expires_at=func.now() + text("INTERVAL '5 minutes'"),
        )
        session.add(run)
        session.commit()
        run_id = run.id
    with run_session_factory() as session:
        run = session.get(Run, run_id)
        assert run.finished_at is None
        assert run.last_heartbeat_at == run.started_at == run.created_at == run.updated_at
        for value in (run.last_heartbeat_at, run.lease_expires_at, run.started_at, run.created_at, run.updated_at):
            assert value.tzinfo is not None and value.utcoffset() is not None
        assert run.lease_expires_at > run.last_heartbeat_at
        assert run.worker_id == "default-time-test"


@pytest.mark.parametrize("field", ["worker_id", "lease_expires_at"])
@pytest.mark.parametrize("explicit_null", [True, False])
def test_run_requires_owner_and_lease_expiry(run_session_factory, field, explicit_null):
    task_id = create_parent_task(run_session_factory)
    fields = {
        "task_id": task_id, "attempt_number": 1, "status": RunStatus.RUNNING,
        "creation_idempotency_key": uuid4().hex, "worker_id": "required-field-test",
        "lease_expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    if explicit_null:
        fields[field] = None
    else:
        del fields[field]
    with run_session_factory() as session:
        with pytest.raises(IntegrityError) as exc_info:
            with session.begin():
                session.add(Run(**fields))
                session.flush()
        assert exc_info.value.orig.sqlstate == "23502"
        assert exc_info.value.orig.diag.column_name == field


def test_run_finishing_updates_time_without_rewriting_start_or_creation(run_session_factory):
    task_id = create_parent_task(run_session_factory)
    with run_session_factory() as session:
        run = make_run(
            task_id=task_id, attempt_number=1, status=RunStatus.RUNNING,
            creation_idempotency_key=uuid4().hex,
        )
        session.add(run)
        session.commit()
        run_id = run.id
        original = {field: getattr(run, field) for field in (
            "created_at", "started_at", "last_heartbeat_at", "lease_expires_at", "updated_at",
        )}
    with run_session_factory() as session:
        session.execute(update(Run).where(Run.id == run_id).values(
            status=RunStatus.FAILED, finished_at=func.now(),
        ))
        session.commit()
    with run_session_factory() as session:
        run = session.get(Run, run_id)
        assert run.status == RunStatus.FAILED
        assert run.finished_at == run.updated_at
        assert run.updated_at > original["updated_at"]
        for field in ("created_at", "started_at", "last_heartbeat_at", "lease_expires_at"):
            assert getattr(run, field) == original[field]


def test_run_foreign_key_rejects_nonexistent_parent_task(run_session_factory):
    with run_session_factory() as session:
        with pytest.raises(IntegrityError) as exc_info:
            with session.begin():
                session.add(make_run(
                    task_id=uuid4(), attempt_number=1, status=RunStatus.RUNNING,
                    creation_idempotency_key=uuid4().hex,
                ))
                session.flush()
        assert exc_info.value.orig.sqlstate == "23503"
        assert exc_info.value.orig.diag.table_name == "runs"


def test_run_attempt_number_cannot_repeat_with_different_creation_key(run_session_factory):
    task_id = create_parent_task(run_session_factory)
    with run_session_factory() as session:
        session.add(make_run(
            task_id=task_id, attempt_number=1, status=RunStatus.FAILED,
            creation_idempotency_key="first-creation",
        ))
        session.commit()
    with run_session_factory() as session:
        with pytest.raises(IntegrityError) as exc_info:
            with session.begin():
                session.add(make_run(
                    task_id=task_id, attempt_number=1, status=RunStatus.FAILED,
                    creation_idempotency_key="different-creation",
                ))
                session.flush()
        assert exc_info.value.orig.sqlstate == "23505"
        assert exc_info.value.orig.diag.constraint_name == "uq_runs_task_attempt_number"


def test_run_migration_roundtrip_preserves_tasks_and_recreates_enum(run_session_factory):
    task_id = create_parent_task(run_session_factory)
    with run_session_factory() as session:
        run = make_run(
            task_id=task_id, attempt_number=1, status=RunStatus.RUNNING,
            creation_idempotency_key=uuid4().hex,
        )
        session.add(run)
        session.commit()
        old_run_id = run.id

    engine = run_session_factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    revision = load_run_migration()
    recovery_revision = load_run_migration("e7a1c4d9b2f0_add_run_recovery_action.py")
    start_revision = load_run_migration("f3b8d2a6c1e4_add_run_recovery_start.py")
    # The execution-version column lives in its own revision; the roundtrip re-applies every
    # revision that shapes this table, so the column set can still be compared with the model.
    version_revision = load_run_migration("e2f5b8c41d07_add_run_execution_sha256.py")
    with engine.begin() as connection:
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            revision.downgrade()
            inspector = inspect(connection)
            assert not inspector.has_table("runs", schema=schema)
            assert "run_status" not in {item["name"] for item in inspector.get_enums(schema=schema)}
            assert connection.scalar(select(Task.id).where(Task.id == task_id)) == task_id
            revision.upgrade()
            recovery_revision.upgrade()
            start_revision.upgrade()
            version_revision.upgrade()
        inspector = inspect(connection)
        assert {column["name"] for column in inspector.get_columns("runs", schema=schema)} == set(Run.__table__.columns.keys())
        enum = next(item for item in inspector.get_enums(schema=schema) if item["name"] == "run_status")
        assert enum["labels"] == ["RUNNING", "PASSED", "FAILED"]
    with run_session_factory() as session:
        # A schema roundtrip does not restore the deleted Run's historical record.
        assert session.get(Run, old_run_id) is None
        assert session.get(Task, task_id) is not None
        session.add(make_run(
            task_id=task_id, attempt_number=1, status=RunStatus.RUNNING,
            creation_idempotency_key=uuid4().hex,
        ))
        session.commit()

def test_run_repository_add_flushes_but_caller_can_rollback(run_session_factory):
    task_id = create_parent_task(run_session_factory)
    with run_session_factory() as session:
        run = make_run(
            task_id=task_id, attempt_number=1, status=RunStatus.RUNNING,
            creation_idempotency_key=uuid4().hex,
        )
        assert RunRepository(session).add(run) is run
        run_id = run.id
        assert run_id is not None
        assert session.in_transaction()
        with run_session_factory() as read_session:
            assert read_session.get(Run, run_id) is None
        session.rollback()
    with run_session_factory() as session:
        assert session.get(Run, run_id) is None


def test_run_repository_lookup_uses_both_task_and_creation_key(run_session_factory):
    first_task_id = create_parent_task(run_session_factory)
    second_task_id = create_parent_task(run_session_factory)
    with run_session_factory() as session:
        repository = RunRepository(session)
        first_run = repository.add(make_run(
            task_id=first_task_id, attempt_number=1, status=RunStatus.FAILED,
            creation_idempotency_key="shared-key",
        ))
        second_run = repository.add(make_run(
            task_id=second_task_id, attempt_number=1, status=RunStatus.FAILED,
            creation_idempotency_key="shared-key",
        ))
        other_run = repository.add(make_run(
            task_id=first_task_id, attempt_number=2, status=RunStatus.FAILED,
            creation_idempotency_key="other-key",
        ))
        expected_ids = first_run.id, second_run.id, other_run.id
        session.commit()
    with run_session_factory() as session:
        repository = RunRepository(session)
        assert repository.get_by_creation_idempotency_key(first_task_id, "shared-key").id == expected_ids[0]
        assert repository.get_by_creation_idempotency_key(second_task_id, "shared-key").id == expected_ids[1]
        assert repository.get_by_creation_idempotency_key(first_task_id, "other-key").id == expected_ids[2]
        assert repository.get_by_creation_idempotency_key(first_task_id, "missing-key") is None
        assert repository.get_by_creation_idempotency_key(uuid4(), "shared-key") is None
        assert session.in_transaction()


def test_run_repository_flush_failure_rolls_back_earlier_add(run_session_factory):
    task_id = create_parent_task(run_session_factory)
    with run_session_factory() as session:
        repository = RunRepository(session)
        first_run = repository.add(make_run(
            task_id=task_id, attempt_number=1, status=RunStatus.FAILED,
            creation_idempotency_key="first-key",
        ))
        first_run_id = first_run.id
        with pytest.raises(IntegrityError) as exc_info:
            repository.add(make_run(
                task_id=task_id, attempt_number=2, status=RunStatus.FAILED,
                creation_idempotency_key="invalid-owner", worker_id=None,
            ))
        assert exc_info.value.orig.diag.column_name == "worker_id"
        session.rollback()
    with run_session_factory() as session:
        assert session.get(Run, first_run_id) is None


@pytest.mark.parametrize("deleted", [False, True])
def test_run_repository_lookup_reads_current_database_state(run_session_factory, deleted):
    task_id = create_parent_task(run_session_factory)
    key = "refresh-key"
    with run_session_factory() as session:
        run = RunRepository(session).add(make_run(
            task_id=task_id, attempt_number=1, status=RunStatus.RUNNING,
            creation_idempotency_key=key,
        ))
        run_id = run.id
        session.commit()
    with run_session_factory(expire_on_commit=False) as stale_session:
        repository = RunRepository(stale_session)
        cached_run = repository.get_by_creation_idempotency_key(task_id, key)
        stale_session.commit()
        with run_session_factory() as session:
            if deleted:
                session.delete(session.get(Run, run_id))
            else:
                session.execute(update(Run).where(Run.id == run_id).values(
                    status=RunStatus.FAILED, finished_at=func.now(),
                ))
            session.commit()
        assert cached_run.status == RunStatus.RUNNING
        loaded_run = repository.get_by_creation_idempotency_key(task_id, key)
        if deleted:
            assert loaded_run is None
        else:
            assert loaded_run is cached_run
            assert loaded_run.status == RunStatus.FAILED
            assert loaded_run.finished_at is not None


def test_run_repository_locked_lookup_returns_target_without_changing_fields(run_session_factory):
    run_id = create_repository_run(run_session_factory)
    with run_session_factory() as session:
        original_run = session.get(Run, run_id)
        original = {column.name: getattr(original_run, column.name) for column in Run.__table__.columns}
        transaction = session.get_transaction()
        repository = RunRepository(session)
        locked_run = repository.get_by_id_for_update(run_id)
        assert locked_run is original_run
        assert {name: getattr(locked_run, name) for name in original} == original
        assert repository.get_by_id_for_update(uuid4()) is None
        assert session.get_transaction() is transaction
        session.rollback()
    with run_session_factory() as session:
        stored_run = session.get(Run, run_id)
        assert {name: getattr(stored_run, name) for name in original} == original


@pytest.mark.parametrize("deleted", [False, True])
def test_run_repository_locked_lookup_refreshes_cached_or_deleted_run(run_session_factory, deleted):
    run_id = create_repository_run(run_session_factory)
    with run_session_factory(expire_on_commit=False) as stale_session:
        cached_run = stale_session.get(Run, run_id)
        stale_session.commit()
        with run_session_factory() as session:
            run = session.get(Run, run_id)
            if deleted:
                session.delete(run)
            else:
                run.status = RunStatus.FAILED
                run.finished_at = func.now()
            session.commit()
        assert cached_run.status == RunStatus.RUNNING
        loaded_run = RunRepository(stale_session).get_by_id_for_update(run_id)
        if deleted:
            assert loaded_run is None
        else:
            assert loaded_run is cached_run
            assert loaded_run.status == RunStatus.FAILED
            assert loaded_run.finished_at is not None


@pytest.mark.parametrize("end_transaction", ["commit", "rollback"])
def test_run_repository_lock_is_held_until_caller_ends_transaction(run_session_factory, end_transaction):
    run_id = create_repository_run(run_session_factory)
    with run_session_factory() as holder, run_session_factory() as contender:
        assert RunRepository(holder).get_by_id_for_update(run_id).id == run_id
        transaction = holder.get_transaction()
        contender.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with pytest.raises(OperationalError) as exc_info:
            RunRepository(contender).get_by_id_for_update(run_id)
        assert exc_info.value.orig.sqlstate == "55P03"
        contender.rollback()
        assert holder.get_transaction() is transaction
        getattr(holder, end_transaction)()
        contender.execute(text("SET LOCAL lock_timeout = '1s'"))
        assert RunRepository(contender).get_by_id_for_update(run_id).id == run_id


def test_run_repository_waits_for_lock_then_reads_committed_state(run_session_factory):
    run_id = create_repository_run(run_session_factory)
    waiting_pids = Queue()

    def wait_for_run():
        with run_session_factory() as session:
            session.execute(text("SET LOCAL statement_timeout = '5s'"))
            cached_run = session.get(Run, run_id)
            assert cached_run.status == RunStatus.RUNNING
            waiting_pids.put(session.scalar(text("SELECT pg_backend_pid()")))
            transaction = session.get_transaction()
            locked_run = RunRepository(session).get_by_id_for_update(run_id)
            assert locked_run is cached_run
            assert session.get_transaction() is transaction
            return locked_run.id, locked_run.status, locked_run.finished_at

    engine = run_session_factory.kw["bind"]
    with run_session_factory() as holder:
        holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
        run = RunRepository(holder).get_by_id_for_update(run_id)
        # Simulate a lifecycle update while another transaction is waiting for the lock.
        run.status = RunStatus.FAILED
        run.finished_at = func.now()
        holder.flush()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(wait_for_run)
            try:
                waiting_pid = waiting_pids.get(timeout=5)
                with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
                    deadline = monotonic() + 3
                    while True:
                        blockers = observer.scalar(
                            text("SELECT pg_blocking_pids(:pid)"), {"pid": waiting_pid},
                        )
                        if holder_pid in blockers:
                            break
                        if future.done():
                            future.result()
                            pytest.fail("Run lookup returned before the row lock was released")
                        assert monotonic() < deadline, "Run lookup never waited for the expected row lock"
                        sleep(0.01)
                assert not future.done()
                holder.commit()
                result = future.result(timeout=6)
            finally:
                holder.rollback()
        assert result[0] == run_id
        assert result[1] == RunStatus.FAILED
        assert result[2] is not None
