from datetime import datetime, timezone
import json
from unittest.mock import Mock
from uuid import uuid4
from threading import Event

import pytest

import devflow_ai.worker as worker_module
from devflow_ai.enums import RunStatus
from devflow_ai.worker import RunClaim, main, resolve_run_directories


def test_recovery_loop_stops_after_result_and_forwards_parameters(tmp_path, monkeypatch):
    worker = worker_module.Worker(None, "loop-worker")
    stop = Event()
    scan = Mock(return_value={"outcome": "idle", "run_id": None})
    monkeypatch.setattr(worker, "reclaim_next_expired_run", scan)
    seen = []

    def receive(result):
        seen.append(result)
        stop.set()

    worker.run_recovery_loop(
        workspaces_root=tmp_path, artifacts_root=tmp_path,
        supervisor_gateway=None, explorer_gateway=None, planner_gateway=None,
        recovery_max_read_bytes=1024, stop_event=stop, poll_interval_seconds=30,
        on_result=receive)
    assert len(seen) == 1 and seen[0]["outcome"] == "idle"
    assert scan.call_count == 1 and scan.call_args.kwargs["stop_event"] is stop


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan"), True])
def test_recovery_loop_rejects_invalid_interval(tmp_path, interval):
    worker = worker_module.Worker(None, "loop-worker")
    with pytest.raises((TypeError, ValueError)):
        worker.run_recovery_loop(
            workspaces_root=tmp_path, artifacts_root=tmp_path,
            supervisor_gateway=None, explorer_gateway=None, planner_gateway=None,
            recovery_max_read_bytes=1024, stop_event=Event(), poll_interval_seconds=interval)


def test_recovery_loop_propagates_scan_failure(tmp_path, monkeypatch):
    worker = worker_module.Worker(None, "loop-worker")
    monkeypatch.setattr(worker, "reclaim_next_expired_run", Mock(side_effect=RuntimeError("scan failed")))
    with pytest.raises(RuntimeError, match="scan failed"):
        worker.run_recovery_loop(
            workspaces_root=tmp_path, artifacts_root=tmp_path,
            supervisor_gateway=None, explorer_gateway=None, planner_gateway=None,
            recovery_max_read_bytes=1024, stop_event=Event(), poll_interval_seconds=1)


def test_resolve_run_directories_keeps_distinct_run_paths(tmp_path):
    workspaces = tmp_path / "workspaces"
    artifacts = tmp_path / "artifacts"
    first_id = uuid4()
    first = resolve_run_directories(first_id, workspaces_root=workspaces, artifacts_root=artifacts)
    second = resolve_run_directories(uuid4(), workspaces_root=workspaces, artifacts_root=artifacts)

    assert first.workspace_root == workspaces.resolve() / str(first_id)
    assert first.artifact_root == artifacts.resolve() / str(first_id)
    assert first.workspace_root != second.workspace_root
    assert first.artifact_root != second.artifact_root
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(("workspace", "artifact"), [
    ("workspaces", "other/../workspaces"),
    ("workspaces", "workspaces/artifacts"),
    ("artifacts/workspaces", "artifacts"),
])
def test_resolve_run_directories_rejects_overlapping_roots(tmp_path, workspace, artifact):
    with pytest.raises(ValueError, match="must be separate"):
        resolve_run_directories(
            uuid4(), workspaces_root=tmp_path / workspace, artifacts_root=tmp_path / artifact,
        )


def test_resolve_run_directories_rejects_alias_of_another_run(tmp_path):
    workspaces = tmp_path / "workspaces"
    artifacts = tmp_path / "artifacts"
    existing_run = workspaces / str(uuid4())
    existing_run.mkdir(parents=True)
    run_id = uuid4()
    link = workspaces / str(run_id)
    try:
        link.symlink_to(existing_run, target_is_directory=True)
    except OSError as error:
        if getattr(error, "winerror", None) != 1314:
            raise
        from _winapi import CreateJunction

        CreateJunction(str(existing_run), str(link))

    with pytest.raises(ValueError, match="must not redirect"):
        resolve_run_directories(run_id, workspaces_root=workspaces, artifacts_root=artifacts)


def test_cli_requires_explicit_once_mode_before_opening_database(monkeypatch):
    monkeypatch.setattr(worker_module, "create_db_engine", lambda: pytest.fail("Unexpected database access"))
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 2


def test_cli_help_does_not_open_database(monkeypatch, capsys):
    monkeypatch.setattr(worker_module, "create_db_engine", lambda: pytest.fail("Unexpected database access"))
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    assert "no Agent execution" in capsys.readouterr().out


@pytest.mark.parametrize("arguments", [
    ["--once", "--worker-id", "   "],
    ["--once", "--heartbeat-interval-seconds", "10", "--lease-duration-seconds", "5"],
    ["--once", "--lease-duration-seconds", "0"],
])
def test_cli_invalid_configuration_fails_before_database_access(monkeypatch, capsys, arguments):
    monkeypatch.setattr(worker_module, "create_db_engine", lambda: pytest.fail("Unexpected database access"))
    assert main(arguments) == 2
    captured = capsys.readouterr()
    assert not captured.out
    result = json.loads(captured.err)
    assert result["event"] == "worker_configuration_error"
    if "--worker-id" in arguments:
        assert "worker_id" in result["detail"]
    else:
        assert result["errors"]
        assert all("input" not in error for error in result["errors"])


@pytest.mark.parametrize("claimed", [False, True])
def test_cli_distinguishes_idle_and_claimed_and_disposes_engine(monkeypatch, capsys, claimed):
    engine = Mock()
    session_factory = Mock()
    claim = RunClaim(
        task_id=uuid4(), run_id=uuid4(), attempt_number=1, status=RunStatus.RUNNING,
        worker_id="cli-test", creation_idempotency_key="creation-key",
        lease_expires_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
    ) if claimed else None
    monkeypatch.setattr(worker_module, "create_db_engine", lambda: engine)
    monkeypatch.setattr(worker_module, "create_session_factory", lambda received_engine: session_factory)

    def fake_claim_once(worker):
        assert worker.session_factory is session_factory
        assert worker.worker_id == "cli-test"
        return claim

    monkeypatch.setattr(worker_module.Worker, "claim_once", fake_claim_once)
    assert main(["--once", "--worker-id", "cli-test"]) == 0
    captured = capsys.readouterr()
    assert not captured.err
    result = json.loads(captured.out)
    assert result["event"] == ("worker_run_claimed" if claimed else "worker_idle")
    if claimed:
        assert result["run_id"] == str(claim.run_id)
        assert result["task_id"] == str(claim.task_id)
        assert result["lease_expires_at"].endswith("Z")
    engine.dispose.assert_called_once_with()


def test_cli_error_is_nonzero_sanitized_and_resources_disposed(monkeypatch, capsys):
    engine = Mock()
    monkeypatch.setattr(worker_module, "create_db_engine", lambda: engine)
    monkeypatch.setattr(worker_module, "create_session_factory", lambda received_engine: Mock())

    def fail_claim(worker):
        raise RuntimeError("password=must-not-appear-in-output")

    monkeypatch.setattr(worker_module.Worker, "claim_once", fail_claim)
    assert main(["--once", "--worker-id", "cli-test"]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "must-not-appear" not in captured.err
    assert json.loads(captured.err) == {
        "event": "worker_claim_failed", "worker_id": "cli-test",
        "error_type": "RuntimeError", "detail": "Task claim failed",
    }
    engine.dispose.assert_called_once_with()


def test_cli_missing_database_configuration_is_reported_without_traceback(monkeypatch, capsys):
    for name in ("DEVFLOW_DB_NAME", "DEVFLOW_DB_USER", "DEVFLOW_DB_PASSWORD", "DEVFLOW_DB_HOST", "DEVFLOW_DB_PORT"):
        monkeypatch.delenv(name, raising=False)
    assert main(["--once", "--worker-id", "cli-test"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.err)["error_type"] == "ValueError"
    assert "Traceback" not in captured.err
