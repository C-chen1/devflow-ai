from contextlib import contextmanager
from threading import Event, Thread
from time import monotonic, sleep
from uuid import UUID
from sqlalchemy import exists, select, text
from sqlalchemy.engine import Engine
from devflow_ai.enums import ToolCallStatus
from devflow_ai.models import ToolCall
import sys


class WorkspaceLockUnavailableError(RuntimeError):
    """A workspace operation could not acquire its Run lock."""


class WorkspaceQuarantinedError(WorkspaceLockUnavailableError):
    """An earlier mutating tool may still be operating on this Run's workspace."""


class WorkspaceLockMonitorError(WorkspaceLockUnavailableError):
    """The lock monitor failed or did not stop within its cleanup deadline."""


class WorkspaceLockReceipt:
    def __init__(self, backend_pid: int, run_id: UUID, *, namespace: int = 0):
        self.backend_pid = backend_pid
        self.key = int.from_bytes(run_id.bytes[:8], byteorder="big") ^ namespace

    def assert_held(self, session) -> None:
        held = session.scalar(text(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
            "AND pid = :pid AND classid = :high AND objid = :low AND objsubid = 1 AND granted)"
        ), {"pid": self.backend_pid, "high": self.key >> 32, "low": self.key & 0xffffffff})
        if not held:
            raise WorkspaceLockUnavailableError("Workspace advisory lock is no longer held")


@contextmanager
def workspace_operation_lock(
    engine: Engine, run_id: UUID, *, stop_event: Event | None = None, timeout_seconds: float = 30,
    check_unsettled_mutations: bool = False, namespace: int = 0,
):
    """Hold a session-level PostgreSQL advisory lock for one Run operation.

    ``namespace`` separates independent lock domains for the same Run.
    """
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    lock_key = int.from_bytes(run_id.bytes[:8], byteorder="big", signed=True) ^ namespace
    deadline = monotonic() + timeout_seconds
    with engine.connect() as connection:
        acquired = False
        monitor = None
        monitor_stop = Event()
        monitor_errors: list[Exception] = []
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    raise WorkspaceLockUnavailableError("Workspace operation was stopped")
                acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key})
                if acquired:
                    break
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise WorkspaceLockUnavailableError("Workspace operation lock timed out")
                if stop_event is None:
                    sleep(min(0.1, remaining))
                else:
                    stop_event.wait(min(0.1, remaining))
            if stop_event is not None and stop_event.is_set():
                raise WorkspaceLockUnavailableError("Workspace operation was stopped")
            if check_unsettled_mutations:
                unsettled = connection.scalar(select(exists().where(
                    ToolCall.run_id == run_id,
                    ToolCall.tool_name.in_(("git_clone", "git_checkout", "apply_patch", "run_command")),
                    ToolCall.status.in_((
                        ToolCallStatus.REQUESTED, ToolCallStatus.RUNNING, ToolCallStatus.UNKNOWN_RESULT,
                    )),
                )))
                if unsettled:
                    raise WorkspaceQuarantinedError("Workspace has an unsettled mutating ToolCall")
            receipt = WorkspaceLockReceipt(connection.scalar(text("SELECT pg_backend_pid()")), run_id,
                                           namespace=namespace)
            if stop_event is not None:
                def monitor_connection():
                    try:
                        while not monitor_stop.wait(0.2):
                            connection.execute(text("SELECT 1"))
                    except Exception as error:
                        monitor_errors.append(error)
                        stop_event.set()

                monitor = Thread(target=monitor_connection, name="devflow-workspace-lock", daemon=True)
                monitor.start()
            yield receipt
        finally:
            if acquired:
                active_error = sys.exception()
                if monitor is not None:
                    monitor_stop.set()
                    monitor.join(timeout=2)
                    if monitor.is_alive():
                        stop_event.set()
                        # Keep the connection out of the pool while the monitor may still use it.
                        connection.invalidate()
                        raise WorkspaceLockMonitorError("Workspace lock monitor did not stop")
                try:
                    released = connection.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
                    if not released:
                        connection.invalidate()
                        raise WorkspaceLockUnavailableError("Workspace operation lock was lost")
                except Exception as cleanup_error:
                    connection.invalidate()
                    if active_error is None:
                        if monitor_errors:
                            raise WorkspaceLockMonitorError("Workspace lock connection failed") from monitor_errors[0]
                        raise
                    active_error.add_note(
                        f"Workspace lock cleanup could not be confirmed: {type(cleanup_error).__name__}"
                    )
                if monitor_errors:
                    if active_error is not None:
                        active_error.add_note("Workspace lock monitor detected a connection failure")
                    else:
                        raise WorkspaceLockMonitorError("Workspace lock connection failed") from monitor_errors[0]
