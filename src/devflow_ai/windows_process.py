"""Windows process creation with Job membership established before execution."""
from contextlib import ExitStack, contextmanager
from ctypes import wintypes
from math import ceil
from os import devnull
from pathlib import Path
from time import monotonic, sleep
from typing import BinaryIO
import ctypes
import _winapi
import msvcrt
import subprocess

from devflow_ai.process_evidence import mark_process_not_started


_CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_POLL_INTERVAL_SECONDS = 0.02
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_ERROR_INVALID_PARAMETER = 87
_ERROR_MORE_DATA = 234
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_create_job = _kernel32.CreateJobObjectW
_create_job.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
_create_job.restype = wintypes.HANDLE
_assign_process_to_job = _kernel32.AssignProcessToJobObject
_assign_process_to_job.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
_assign_process_to_job.restype = wintypes.BOOL
_resume_thread = _kernel32.ResumeThread
_resume_thread.argtypes = (wintypes.HANDLE,)
_resume_thread.restype = wintypes.DWORD
_terminate_job = _kernel32.TerminateJobObject
_terminate_job.argtypes = (wintypes.HANDLE, wintypes.UINT)
_terminate_job.restype = wintypes.BOOL
_query_job = _kernel32.QueryInformationJobObject
_query_job.argtypes = (
    wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPDWORD,
)
_query_job.restype = wintypes.BOOL
_set_job = _kernel32.SetInformationJobObject
_set_job.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
_set_job.restype = wintypes.BOOL
_is_process_in_job = _kernel32.IsProcessInJob
_is_process_in_job.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
_is_process_in_job.restype = wintypes.BOOL


class _JobAccounting(ctypes.Structure):
    _fields_ = [
        ("total_user_time", ctypes.c_longlong),
        ("total_kernel_time", ctypes.c_longlong),
        ("period_user_time", ctypes.c_longlong),
        ("period_kernel_time", ctypes.c_longlong),
        ("total_page_fault_count", wintypes.DWORD),
        ("total_processes", wintypes.DWORD),
        ("active_processes", wintypes.DWORD),
        ("total_terminated_processes", wintypes.DWORD),
    ]


class _JobBasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_user_time_limit", ctypes.c_longlong),
        ("job_user_time_limit", ctypes.c_longlong),
        ("limit_flags", wintypes.DWORD),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class _JobExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _JobBasicLimits),
        ("io_counters", ctypes.c_ulonglong * 6),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


class WindowsJobProcess:
    def __init__(self, args: list[str], process_handle: int, job_handle: int):
        self.args = args
        self._process_handle = process_handle
        self.job_handle = job_handle

    def poll(self) -> int | None:
        if _winapi.WaitForSingleObject(self._process_handle, 0) == _winapi.WAIT_TIMEOUT:
            return None
        return _winapi.GetExitCodeProcess(self._process_handle)

    def _wait_for_root(self, timeout: float | None = None) -> int:
        milliseconds = _winapi.INFINITE if timeout is None else ceil(timeout * 1000)
        if _winapi.WaitForSingleObject(self._process_handle, milliseconds) == _winapi.WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return _winapi.GetExitCodeProcess(self._process_handle)

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else monotonic() + timeout
        returncode = self._wait_for_root(timeout)
        if not self._wait_for_empty_job(deadline):
            raise subprocess.TimeoutExpired(self.args, timeout)
        return returncode

    def active_process_count(self) -> int:
        accounting = _JobAccounting()
        if not _query_job(
            self.job_handle, _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(accounting), ctypes.sizeof(accounting), None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return accounting.active_processes

    def _wait_for_empty_job(self, deadline: float | None) -> bool:
        while self.active_process_count():
            wait_seconds = _JOB_POLL_INTERVAL_SECONDS
            if deadline is not None:
                remaining_seconds = deadline - monotonic()
                if remaining_seconds <= 0:
                    return False
                wait_seconds = min(wait_seconds, remaining_seconds)
            sleep(wait_seconds)
        return True

    def _process_ids(self, deadline: float) -> list[int]:
        capacity = 8
        while monotonic() < deadline:
            class ProcessIdList(ctypes.Structure):
                _fields_ = [
                    ("assigned", wintypes.DWORD),
                    ("count", wintypes.DWORD),
                    ("pids", ctypes.c_size_t * capacity),
                ]

            members = ProcessIdList()
            success = _query_job(
                self.job_handle, _JOB_OBJECT_BASIC_PROCESS_ID_LIST,
                ctypes.byref(members), ctypes.sizeof(members), None,
            )
            if success and members.assigned == members.count:
                return list(members.pids[:members.count])
            if not success:
                error = ctypes.get_last_error()
                if error != _ERROR_MORE_DATA:
                    raise ctypes.WinError(error)
            capacity = max(capacity * 2, members.assigned)
        raise TimeoutError("Windows Job process enumeration exceeded the cleanup timeout")

    def terminate_tree(self, *, cleanup_timeout_seconds: float) -> None:
        deadline = monotonic() + cleanup_timeout_seconds
        with ExitStack() as process_handles:
            wait_handles = []
            try:
                for pid in self._process_ids(deadline):
                    try:
                        handle = _winapi.OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                    except OSError as error:
                        if error.winerror == _ERROR_INVALID_PARAMETER:
                            continue  # The process already exited.
                        raise
                    process_handles.callback(_winapi.CloseHandle, handle)
                    belongs_to_job = wintypes.BOOL()
                    if not _is_process_in_job(handle, self.job_handle, ctypes.byref(belongs_to_job)):
                        raise ctypes.WinError(ctypes.get_last_error())
                    if belongs_to_job.value:
                        wait_handles.append(handle)
            finally:
                if not _terminate_job(self.job_handle, 1):
                    raise ctypes.WinError(ctypes.get_last_error())

            # Job accounting can reach zero before a terminating process signals its handle.
            for handle in wait_handles:
                milliseconds = max(0, ceil((deadline - monotonic()) * 1000))
                if _winapi.WaitForSingleObject(handle, milliseconds) == _winapi.WAIT_TIMEOUT:
                    raise TimeoutError("A Windows Job process did not exit before the cleanup timeout")
            if not self._wait_for_empty_job(deadline):
                raise TimeoutError("Windows Job still has active processes after the cleanup timeout")

    def kill(self) -> None:
        try:
            _winapi.TerminateProcess(self._process_handle, 1)
        except PermissionError:
            if self.poll() is None:
                raise


@contextmanager
def start_windows_job_process(
    args: list[str], *, cwd: Path | None, env: dict[str, str], stdout: BinaryIO, stderr: BinaryIO,
):
    """Start a process in a new Job and keep its handles open through the context."""
    with ExitStack() as owned_handles:
        try:
            job_handle = _create_job(None, None)
            if not job_handle:
                raise ctypes.WinError(ctypes.get_last_error())
            owned_handles.callback(_winapi.CloseHandle, job_handle)
            limits = _JobExtendedLimits()
            limits.basic.limit_flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _set_job(
                job_handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits), ctypes.sizeof(limits),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        except OSError as error:
            # No child exists yet, so this boundary can prove non-execution.
            raise mark_process_not_started(error)

        with open(devnull, "rb") as stdin, ExitStack() as inherited_handles:
            current_process = _winapi.GetCurrentProcess()
            std_handles = []
            for stream in (stdin, stdout, stderr):
                handle = _winapi.DuplicateHandle(
                    current_process, msvcrt.get_osfhandle(stream.fileno()), current_process,
                    0, True, _winapi.DUPLICATE_SAME_ACCESS,
                )
                inherited_handles.callback(_winapi.CloseHandle, handle)
                std_handles.append(handle)
            startup = subprocess.STARTUPINFO(
                dwFlags=_winapi.STARTF_USESTDHANDLES,
                hStdInput=std_handles[0], hStdOutput=std_handles[1], hStdError=std_handles[2],
                lpAttributeList={"handle_list": std_handles},
            )
            try:
                process_handle, thread_handle, _, _ = _winapi.CreateProcess(
                    None, subprocess.list2cmdline(args), None, None, True,
                    _CREATE_SUSPENDED | _winapi.CREATE_NO_WINDOW, env,
                    str(cwd) if cwd is not None else None, startup,
                )
            except OSError as error:
                # CreateProcess failing means no child exists yet; anything that fails
                # after this point leaves the outcome unknown.
                raise mark_process_not_started(error)
            owned_handles.callback(_winapi.CloseHandle, process_handle)
            process = WindowsJobProcess(args, process_handle, job_handle)
            try:
                if not _assign_process_to_job(job_handle, process_handle):
                    raise ctypes.WinError(ctypes.get_last_error())
                if _resume_thread(thread_handle) == 0xFFFFFFFF:
                    raise ctypes.WinError(ctypes.get_last_error())
            except BaseException as error:
                try:
                    if process.poll() is None:
                        process.kill()
                        process._wait_for_root()
                except OSError as cleanup_error:
                    error.add_note(f"Suspended process cleanup failed: {cleanup_error}")
                raise
            finally:
                _winapi.CloseHandle(thread_handle)

        yield process
