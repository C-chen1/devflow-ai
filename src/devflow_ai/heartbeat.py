"""A single Run's heartbeat lifecycle; no task polling or tool-process control."""
from __future__ import annotations
from threading import Event, Lock, Thread, current_thread
from uuid import UUID
from sqlalchemy import event, text
from sqlalchemy.orm import Session, sessionmaker
from devflow_ai.config import HeartbeatConfig, LeaseConfig
from devflow_ai.coordinator import LeaseRenewal, RunCoordinator
import json
import logging


logger = logging.getLogger(__name__)


class HeartbeatShutdownError(RuntimeError):
    """The heartbeat is still alive; cleanup and further work are not permitted."""


class RunHeartbeat:
    def __init__(
        self, session_factory: sessionmaker[Session], run_id: UUID, worker_id: str,
        *, heartbeat_config: HeartbeatConfig, lease_config: LeaseConfig | None = None,
    ):
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be a non-blank string")
        self.session_factory = session_factory
        self.run_id = run_id
        self.worker_id = worker_id
        # Validate detached copies so later caller mutations cannot alter a running loop.
        self._heartbeat_config = HeartbeatConfig.model_validate(heartbeat_config.model_dump())
        self._lease_config = LeaseConfig.model_validate(
            (lease_config if lease_config is not None else LeaseConfig()).model_dump(),
        )
        self.heartbeat_stop_event = Event()
        self.execution_stop_event = Event()
        self._lifecycle_lock = Lock()
        self._result_lock = Lock()
        self._thread: Thread | None = None
        self._closed = False
        self._failure: BaseException | None = None
        self._shutdown_failure: HeartbeatShutdownError | None = None
        self._last_renewal: LeaseRenewal | None = None

    @property
    def failure(self) -> BaseException | None:
        with self._result_lock:
            return self._failure

    @property
    def last_renewal(self) -> LeaseRenewal | None:
        with self._result_lock:
            return self._last_renewal

    def is_alive(self) -> bool:
        with self._lifecycle_lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> RunHeartbeat:
        with self._lifecycle_lock:
            if self._closed or self._thread is not None:
                raise RuntimeError("RunHeartbeat is single-use and cannot be started again")
            # Daemon status permits process exit; it is never proof of successful cleanup.
            thread = Thread(target=self._run, name=f"devflow-heartbeat-{self.run_id}", daemon=True)
            self._thread = thread
            try:
                thread.start()
            except BaseException as error:
                self._thread = None
                self._closed = True
                self._record_failure(error)
                raise
        return self

    def stop(self) -> None:
        with self._lifecycle_lock:
            self.heartbeat_stop_event.set()
            self._closed = True
            thread = self._thread
        if thread is None:
            return
        if thread is current_thread():
            raise RuntimeError("The owning execution flow must stop and join the heartbeat")
        thread.join(timeout=self._heartbeat_config.shutdown_timeout_seconds)
        if thread.is_alive():
            with self._result_lock:
                if self._shutdown_failure is None:
                    self._shutdown_failure = HeartbeatShutdownError(
                        "Heartbeat thread is still running after the shutdown timeout",
                    )
                failure = self._shutdown_failure
            self.execution_stop_event.set()
            self._log_failure("run_heartbeat_shutdown_timeout", failure)
            raise failure

    def raise_if_failed(self) -> None:
        """Propagate the saved cause; this does not check current database authority."""
        with self._result_lock:
            failure = self._shutdown_failure or self._failure
        if failure is not None:
            raise failure

    def __enter__(self) -> RunHeartbeat:
        return self.start()

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.stop()
        if exc_type is None:
            self.raise_if_failed()
        return False

    def _run(self) -> None:
        try:
            while not self.heartbeat_stop_event.wait(self._lease_config.heartbeat_interval_seconds):
                if self.execution_stop_event.is_set():
                    return
                receipt = self._renew_once()
                with self._result_lock:
                    self._last_renewal = receipt
                logger.info(json.dumps({
                    "event": "run_heartbeat_renewed", "run_id": str(self.run_id),
                    "worker_id": self.worker_id,
                    "lease_expires_at": receipt.lease_expires_at.isoformat(),
                }))
        except BaseException as error:
            # Thread boundary: even an unexpected thread exit must notify the owner.
            self._record_failure(error)
            self._log_failure("run_heartbeat_failed", error)

    def _renew_once(self) -> LeaseRenewal:
        with self.session_factory() as session:
            def set_statement_timeout(session, transaction, connection):
                # Transaction-local, including lock waits; no pooled/global setting change.
                connection.execute(
                    text("SELECT set_config('statement_timeout', :timeout, true)"),
                    {"timeout": f"{self._heartbeat_config.statement_timeout_seconds * 1000}ms"},
                )

            event.listen(session, "after_begin", set_statement_timeout, once=True)
            return RunCoordinator(session, self._lease_config).renew_lease(self.run_id, self.worker_id)

    def _record_failure(self, failure: BaseException) -> None:
        with self._result_lock:
            if self._failure is None:
                self._failure = failure
        self.execution_stop_event.set()

    def _log_failure(self, event_name: str, failure: BaseException) -> None:
        # Retain the original error for the caller without printing SQL/DSNs or raw input.
        logger.error(json.dumps({
            "event": event_name, "run_id": str(self.run_id),
            "worker_id": self.worker_id, "error_type": type(failure).__name__,
        }))
