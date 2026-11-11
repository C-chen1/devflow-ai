from datetime import datetime, timedelta, timezone
import json
from threading import Event
from unittest.mock import Mock
from uuid import uuid4

from pydantic import ValidationError
import pytest
from sqlalchemy.exc import SQLAlchemyError

from devflow_ai.config import HeartbeatConfig, LeaseConfig
from devflow_ai.coordinator import LeaseRenewal, RunExecutionDeniedError
from devflow_ai.heartbeat import HeartbeatShutdownError, RunHeartbeat


def make_heartbeat(**kwargs):
    return RunHeartbeat(
        Mock(), uuid4(), "heartbeat-test",
        heartbeat_config=HeartbeatConfig(statement_timeout_seconds=1, shutdown_timeout_seconds=1),
        **kwargs,
    )


def renewal(heartbeat, offset=0):
    now = datetime.now(timezone.utc) + timedelta(seconds=offset)
    return LeaseRenewal(
        run_id=heartbeat.run_id, worker_id=heartbeat.worker_id,
        last_heartbeat_at=now, lease_expires_at=now + timedelta(seconds=30),
    )


@pytest.mark.parametrize("values", [
    {}, {"statement_timeout_seconds": 1}, {"shutdown_timeout_seconds": 1},
    {"statement_timeout_seconds": 0, "shutdown_timeout_seconds": 1},
    {"statement_timeout_seconds": 1, "shutdown_timeout_seconds": -1},
    {"statement_timeout_seconds": True, "shutdown_timeout_seconds": 1},
    {"statement_timeout_seconds": 1, "shutdown_timeout_seconds": "2"},
    {"statement_timeout_seconds": 1.5, "shutdown_timeout_seconds": 1},
    {"statement_timeout_seconds": 1, "shutdown_timeout_seconds": None},
    {"statement_timeout_seconds": 1, "shutdown_timeout_seconds": 1, "unknown": 1},
])
def test_heartbeat_requires_explicit_positive_runtime_limits(values):
    with pytest.raises(ValidationError):
        HeartbeatConfig.model_validate(values)


@pytest.mark.parametrize("worker_id", ["", "   ", None])
def test_heartbeat_rejects_invalid_worker_without_starting_thread(worker_id):
    with pytest.raises(ValueError, match="worker_id"):
        RunHeartbeat(
            Mock(), uuid4(), worker_id,
            heartbeat_config=HeartbeatConfig(statement_timeout_seconds=1, shutdown_timeout_seconds=1),
        )


def test_normal_context_exit_interrupts_interval_and_does_not_signal_execution_failure():
    heartbeat = make_heartbeat()
    with heartbeat:
        assert heartbeat.is_alive()
        assert not heartbeat.heartbeat_stop_event.is_set()
        assert not heartbeat.execution_stop_event.is_set()
    assert not heartbeat.is_alive()
    assert heartbeat.heartbeat_stop_event.is_set()
    assert not heartbeat.execution_stop_event.is_set()
    assert heartbeat.failure is None and heartbeat.last_renewal is None
    heartbeat.session_factory.assert_not_called()
    heartbeat.raise_if_failed()


def test_background_loop_can_repeat_and_expose_the_latest_detached_receipt(monkeypatch):
    heartbeat = make_heartbeat()
    receipts = [renewal(heartbeat), renewal(heartbeat, offset=1)]
    completed = Event()
    calls = []

    def renew_once():
        calls.append(len(calls))
        if len(calls) == 2:
            heartbeat.heartbeat_stop_event.set()
            completed.set()
        return receipts[len(calls) - 1]

    # A controlled scheduler keeps this lifecycle test independent of wall-clock speed.
    monkeypatch.setattr(heartbeat.heartbeat_stop_event, "wait", lambda timeout: heartbeat.heartbeat_stop_event.is_set())
    monkeypatch.setattr(heartbeat, "_renew_once", renew_once)
    with heartbeat:
        assert completed.wait(2)
    assert len(calls) == 2
    assert heartbeat.last_renewal is receipts[-1]
    assert heartbeat.last_renewal.model_dump(mode="json")["run_id"] == str(heartbeat.run_id)
    assert not heartbeat.execution_stop_event.is_set()


@pytest.mark.parametrize("error_type", [RunExecutionDeniedError, SQLAlchemyError, RuntimeError, SystemExit])
def test_background_failure_notifies_owner_preserves_cause_and_sanitizes_logs(monkeypatch, caplog, error_type):
    heartbeat = make_heartbeat()
    failure = error_type("password=not-for-logs")

    def fail():
        raise failure

    monkeypatch.setattr(heartbeat.heartbeat_stop_event, "wait", lambda timeout: heartbeat.heartbeat_stop_event.is_set())
    monkeypatch.setattr(heartbeat, "_renew_once", fail)
    with pytest.raises(error_type) as exc_info:
        with heartbeat:
            assert heartbeat.execution_stop_event.wait(2)
            # Failure notification is separate from the owner's normal shutdown signal.
            assert not heartbeat.heartbeat_stop_event.is_set()
    assert exc_info.value is heartbeat.failure is failure
    assert not heartbeat.is_alive()
    records = [record for record in caplog.records if record.name == "devflow_ai.heartbeat"]
    assert len(records) == 1
    log_data = json.loads(records[0].getMessage())
    assert log_data["event"] == "run_heartbeat_failed"
    assert log_data["error_type"] == error_type.__name__
    assert log_data["run_id"] == str(heartbeat.run_id)
    assert "not-for-logs" not in caplog.text


def test_owner_exception_still_stops_heartbeat_and_is_not_masked():
    heartbeat = make_heartbeat()
    failure = ValueError("simulated main-flow failure")
    with pytest.raises(ValueError) as exc_info:
        with heartbeat:
            raise failure
    assert exc_info.value is failure
    assert not heartbeat.is_alive()
    assert not heartbeat.execution_stop_event.is_set()


def test_owner_exception_remains_primary_when_heartbeat_also_failed(monkeypatch):
    heartbeat = make_heartbeat()
    background_failure = SQLAlchemyError("simulated database failure")
    owner_failure = ValueError("simulated main-flow failure")

    def fail():
        raise background_failure

    monkeypatch.setattr(heartbeat.heartbeat_stop_event, "wait", lambda timeout: heartbeat.heartbeat_stop_event.is_set())
    monkeypatch.setattr(heartbeat, "_renew_once", fail)
    with pytest.raises(ValueError) as exc_info:
        with heartbeat:
            assert heartbeat.execution_stop_event.wait(2)
            raise owner_failure
    assert exc_info.value is owner_failure
    assert heartbeat.failure is background_failure
    with pytest.raises(SQLAlchemyError) as background_info:
        heartbeat.raise_if_failed()
    assert background_info.value is background_failure
    assert not heartbeat.is_alive()


@pytest.mark.parametrize("when", ["running", "stopped", "never_started"])
def test_heartbeat_is_single_use_and_stop_is_idempotent(when):
    heartbeat = make_heartbeat()
    try:
        if when != "never_started":
            heartbeat.start()
        if when != "running":
            heartbeat.stop()
        with pytest.raises(RuntimeError, match="single-use"):
            heartbeat.start()
    finally:
        heartbeat.stop()
        heartbeat.stop()
    assert not heartbeat.is_alive()


def test_shutdown_timeout_does_not_claim_a_live_thread_was_cleaned_up(monkeypatch):
    heartbeat = make_heartbeat()
    entered = Event()
    release = Event()

    def blocked_renewal():
        entered.set()
        assert release.wait(6), "Test did not release the simulated blocked database operation"
        return renewal(heartbeat)

    monkeypatch.setattr(heartbeat.heartbeat_stop_event, "wait", lambda timeout: heartbeat.heartbeat_stop_event.is_set())
    monkeypatch.setattr(heartbeat, "_renew_once", blocked_renewal)
    heartbeat.start()
    try:
        assert entered.wait(2)
        with pytest.raises(HeartbeatShutdownError):
            heartbeat.stop()
        assert heartbeat.is_alive()
        assert heartbeat.heartbeat_stop_event.is_set()
        assert heartbeat.execution_stop_event.is_set()
        with pytest.raises(HeartbeatShutdownError):
            heartbeat.raise_if_failed()
    finally:
        release.set()
        heartbeat.stop()
    assert not heartbeat.is_alive()
    # Later cleanup does not silently turn the original failed shutdown into success.
    with pytest.raises(HeartbeatShutdownError):
        heartbeat.raise_if_failed()
