from pathlib import Path
from time import monotonic
from types import SimpleNamespace

import pytest

from devflow_ai.control import main as control_main
from devflow_ai.control import run_recovery_loop_with_backoff

ROOTS = dict(workspaces_root=Path("/tmp"), artifacts_root=Path("/tmp"))


def _gateways():
    return SimpleNamespace(supervisor=None, explorer=None, planner=None)


def _worker(call):
    return SimpleNamespace(reclaim_next_expired_run=call)


def test_recovery_loop_backs_off_and_gives_up_after_repeated_failures():
    calls = []

    def failing(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("database is down")

    with pytest.raises(RuntimeError, match="database is down"):
        run_recovery_loop_with_backoff(
            _worker(failing), _gateways(), run_for_seconds=5.0,
            poll_interval_seconds=0.01, max_consecutive_failures=2, **ROOTS)

    # The loop retried once, then let the error through instead of spinning.
    assert len(calls) == 2


def test_recovery_loop_survives_one_failure_then_reports_its_scans():
    calls = []

    def flaky(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("transient fault")
        return {"outcome": "idle", "run_id": None}

    report = run_recovery_loop_with_backoff(
        _worker(flaky), _gateways(), run_for_seconds=0.3,
        poll_interval_seconds=0.01, max_consecutive_failures=3, **ROOTS)

    assert report["failures"] == [{"error_type": "RuntimeError"}]
    # Idle scans do not end a long-running loop; it keeps polling until its budget.
    assert report["scans"] >= 1
    assert len(calls) == report["scans"] + 1


def test_recovery_loop_is_capped_by_its_time_budget():
    started = monotonic()

    report = run_recovery_loop_with_backoff(
        _worker(lambda **kwargs: {"outcome": "idle", "run_id": None}), _gateways(),
        run_for_seconds=0.15, poll_interval_seconds=0.05,
        max_consecutive_failures=3, **ROOTS)

    assert monotonic() - started < 2
    assert report["scans"] >= 1


def test_recovery_loop_validates_its_limits():
    worker = _worker(lambda **kwargs: {"outcome": "idle", "run_id": None})
    for overrides, message in (
        ({"run_for_seconds": 0}, "run_for_seconds"),
        ({"poll_interval_seconds": 0}, "poll_interval_seconds"),
        ({"max_consecutive_failures": 0}, "max_consecutive_failures"),
    ):
        arguments = dict(run_for_seconds=1.0, poll_interval_seconds=0.01,
                         max_consecutive_failures=3, **ROOTS)
        arguments.update(overrides)
        with pytest.raises(ValueError, match=message):
            run_recovery_loop_with_backoff(worker, _gateways(), **arguments)


def test_control_cli_requires_a_loop_budget(tmp_path, monkeypatch, capsys):
    workspaces, artifacts = tmp_path / "w", tmp_path / "a"
    workspaces.mkdir()
    artifacts.mkdir()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEVFLOW_DEEPSEEK_MODEL", "deepseek-chat")

    exit_code = control_main([
        "--run-recovery-loop", "--workspaces-root", str(workspaces),
        "--artifacts-root", str(artifacts)])

    assert exit_code == 2
    assert "run-for-seconds" in capsys.readouterr().err
