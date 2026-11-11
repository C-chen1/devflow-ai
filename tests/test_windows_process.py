from contextlib import ExitStack, contextmanager
import ctypes
from ctypes import wintypes
from os import environ
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryFile
from threading import Event
from time import monotonic, sleep
from unittest.mock import Mock

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows Jobs")


def test_windows_job_contains_a_child_started_by_the_process(tmp_path):
    import _winapi

    from devflow_ai.windows_process import start_windows_job_process

    is_process_in_job = ctypes.WinDLL("kernel32", use_last_error=True).IsProcessInJob
    is_process_in_job.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
    is_process_in_job.restype = wintypes.BOOL
    pid_file = tmp_path / "child-pid.txt"
    release_file = tmp_path / "release"
    child = """
import os, sys, time
from pathlib import Path
Path(sys.argv[1]).write_text(str(os.getpid()), encoding="ascii")
deadline = time.monotonic() + 10
while not Path(sys.argv[2]).exists() and time.monotonic() < deadline:
    time.sleep(0.02)
"""
    parent = "import subprocess, sys; subprocess.run([sys.executable, '-c', *sys.argv[1:]], check=True)"
    command = [sys.executable, "-c", parent, child, str(pid_file), str(release_file)]

    with TemporaryFile() as stdout, TemporaryFile() as stderr, start_windows_job_process(
        command, cwd=tmp_path, env=dict(environ), stdout=stdout, stderr=stderr,
    ) as process:
        try:
            deadline = monotonic() + 5
            pid = ""
            while not pid and monotonic() < deadline and process.poll() is None:
                if pid_file.exists():
                    pid = pid_file.read_text(encoding="ascii")
                if not pid:
                    sleep(0.02)
            assert pid, "Child process did not report its PID"
            child_handle = _winapi.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
            try:
                belongs_to_job = wintypes.BOOL()
                assert is_process_in_job(child_handle, process.job_handle, ctypes.byref(belongs_to_job))
                assert belongs_to_job.value
            finally:
                _winapi.CloseHandle(child_handle)
        finally:
            release_file.touch()
            process.wait(timeout=10)


def test_windows_job_assignment_failure_kills_the_suspended_process(tmp_path, monkeypatch):
    import devflow_ai.windows_process as windows_process

    marker = tmp_path / "process-ran"
    command = [sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(marker)]

    def reject_assignment(*args):
        ctypes.set_last_error(5)
        return False

    exit_codes = []
    original_wait = windows_process.WindowsJobProcess._wait_for_root

    def wait_for_termination(process):
        result = original_wait(process, timeout=5)
        exit_codes.append(result)
        return result

    resume = Mock()
    monkeypatch.setattr(windows_process, "_assign_process_to_job", reject_assignment)
    monkeypatch.setattr(windows_process, "_resume_thread", resume)
    monkeypatch.setattr(windows_process.WindowsJobProcess, "_wait_for_root", wait_for_termination)
    with TemporaryFile() as stdout, TemporaryFile() as stderr:
        with pytest.raises(OSError) as exc_info:
            with windows_process.start_windows_job_process(
                command, cwd=tmp_path, env=dict(environ), stdout=stdout, stderr=stderr,
            ):
                pytest.fail("A process must be assigned before being returned")
    assert exc_info.value.winerror == 5
    resume.assert_not_called()
    assert exit_codes == [1]
    assert not marker.exists()


@pytest.mark.parametrize("trigger", ["cancel", "timeout"])
def test_harness_terminates_job_when_root_exited_but_child_is_alive(tmp_path, monkeypatch, trigger):
    import _winapi

    import devflow_ai.harness as harness
    from devflow_ai.windows_process import start_windows_job_process

    pid_file = tmp_path / "child-pid.txt"
    child = (
        "from pathlib import Path; import os, sys, time; "
        "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='ascii'); time.sleep(15)"
    )
    parent = "import subprocess, sys; subprocess.Popen([sys.executable, '-c', *sys.argv[1:]])"
    command = [sys.executable, "-c", parent, child, str(pid_file)]
    stop_event = Event()
    child_handles = []
    active_counts_at_exit = []

    @contextmanager
    def start(_args, *, cwd, stdout, stderr):
        with start_windows_job_process(
            command, cwd=cwd, env=dict(environ), stdout=stdout, stderr=stderr,
        ) as process:
            try:
                deadline = monotonic() + 5
                pid = ""
                while monotonic() < deadline:
                    if pid_file.exists():
                        pid = pid_file.read_text(encoding="ascii")
                    if pid and process.poll() is not None:
                        break
                    sleep(0.02)
                assert pid and process.poll() == 0
                child_handle = _winapi.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
                child_handles.append(child_handle)
                assert _winapi.WaitForSingleObject(child_handle, 0) == _winapi.WAIT_TIMEOUT
                assert process.active_process_count() > 0
                if trigger == "cancel":
                    stop_event.set()
                yield process
            finally:
                active_count = process.active_process_count()
                active_counts_at_exit.append(active_count)
                if active_count:
                    process.terminate_tree(cleanup_timeout_seconds=5)

    monkeypatch.setattr(harness, "_start_git_process", start)
    expected_error = harness.GitExecutionCancelledError if trigger == "cancel" else subprocess.TimeoutExpired
    try:
        with pytest.raises(expected_error):
            harness._run_git(["clone"], timeout_seconds=2, cwd=tmp_path, stop_event=stop_event)
        assert active_counts_at_exit == [0]
        assert _winapi.WaitForSingleObject(child_handles[0], 0) == _winapi.WAIT_OBJECT_0
    finally:
        for handle in child_handles:
            _winapi.CloseHandle(handle)


def test_windows_closes_job_and_kills_tree_after_forced_worker_exit(tmp_path):
    import _winapi

    script = tmp_path / "process_tree.py"
    script.write_text("""
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryFile
import time

from devflow_ai.windows_process import start_windows_job_process

role = sys.argv[1]
directory = Path(sys.argv[2])
if role == "worker":
    with TemporaryFile() as stdout, TemporaryFile() as stderr, start_windows_job_process(
        [sys.executable, __file__, "root", str(directory)],
        cwd=directory, env=dict(os.environ), stdout=stdout, stderr=stderr,
    ):
        time.sleep(20)
elif role == "root":
    child = subprocess.Popen([sys.executable, __file__, "child", str(directory)])
    (directory / "root.pid").write_text(str(os.getpid()), encoding="ascii")
    child.wait(timeout=20)
else:
    (directory / "child.pid").write_text(str(os.getpid()), encoding="ascii")
    time.sleep(20)
""", encoding="utf-8")
    source_root = Path(__file__).resolve().parents[1] / "src"
    # Start the real interpreter so the handle belongs to the Worker itself.
    worker = subprocess.Popen(
        [sys._base_executable, "-B", str(script), "worker", str(tmp_path)],
        env={**environ, "PYTHONPATH": str(source_root)}, stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        with ExitStack() as handles:
            deadline = monotonic() + 8
            processes = []
            for name in ("root.pid", "child.pid"):
                pid_file = tmp_path / name
                pid = ""
                while monotonic() < deadline and worker.poll() is None:
                    if pid_file.exists():
                        pid = pid_file.read_text(encoding="ascii")
                        if pid:
                            break
                    sleep(0.02)
                assert pid, f"Test process did not report its PID: {name}"
                handle = _winapi.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
                handles.callback(_winapi.CloseHandle, handle)
                assert _winapi.WaitForSingleObject(handle, 0) == _winapi.WAIT_TIMEOUT
                processes.append(handle)

            worker.kill()
            worker.wait(timeout=5)
            for handle in processes:
                assert _winapi.WaitForSingleObject(handle, 5000) == _winapi.WAIT_OBJECT_0
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
