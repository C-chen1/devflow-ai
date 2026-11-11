"""Permission checks, repository setup, and bounded file access for Harness tools."""
from __future__ import annotations
from codecs import getincrementaldecoder
from collections.abc import Collection, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from os import environ, name as platform_name
from pathlib import Path, PureWindowsPath
from re import fullmatch
from tempfile import TemporaryDirectory, TemporaryFile
from threading import Event
from time import monotonic
from typing import BinaryIO
from uuid import UUID, uuid4
from devflow_ai.artifacts import ToolArtifactResult, write_artifact_file
from devflow_ai.enums import AgentRole
from devflow_ai.policy import PolicyPermissions
import subprocess

if platform_name == "nt":
    from devflow_ai.windows_process import WindowsJobProcess, start_windows_job_process


_GIT_POLL_INTERVAL_SECONDS = 0.1
_GIT_CLEANUP_TIMEOUT_SECONDS = 5.0
_GIT_LOG_PREVIEW_BYTES = 16 * 1024
_GIT_ARTIFACT_CHUNK_BYTES = 64 * 1024


def check_tool_permission(
    tool_name: str,
    agent_role: AgentRole,
    registered_tools: Collection[str],
    permissions: PolicyPermissions,
) -> str | None:
    """Return the denial reason, or None when the tool is allowed for the role."""
    if tool_name not in registered_tools:
        return "TOOL_NOT_REGISTERED"
    if tool_name not in permissions.allowed_tools_by_role.get(agent_role, []):
        return "TOOL_NOT_ALLOWED_FOR_ROLE"
    return None


class WorkspacePathError(ValueError):
    """The requested path is unsuitable for the workspace operation."""


class PatchFormatError(WorkspacePathError):
    """Git could not parse the patch before any workspace modification."""

    def __init__(self, stderr: bytes):
        self.stderr = stderr
        super().__init__("Patch format could not be parsed")


class WorkspaceFileNotFoundError(FileNotFoundError):
    """The requested target could not be found inside the Run workspace."""


class WorkspaceUntrackedFilesError(ValueError):
    """A Git diff alone would omit untracked files in the workspace."""

    def __init__(self, preview: GitOutputPreview):
        self.preview = preview
        super().__init__("Workspace contains untracked files")


class GitExecutionCancelledError(RuntimeError):
    """A stop request prevented or interrupted a Git command."""


class GitProcessCleanupError(RuntimeError):
    """Git process termination could not be confirmed."""


class GitHeadMismatchError(ValueError):
    def __init__(self, expected_commit: str, actual_commit: str, exit_code: int):
        self.expected_commit = expected_commit
        self.actual_commit = actual_commit
        self.exit_code = exit_code
        super().__init__(f"Run workspace HEAD mismatch: expected {expected_commit}, got {actual_commit}")


@dataclass(frozen=True)
class GitOutputPreview:
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    artifacts: tuple[ToolArtifactResult, ...] = ()


class GitHeadOutputError(ValueError):
    """HEAD verification failed before a reliable actual commit could be extracted."""

    def __init__(self, message: str, *, reason: str, exit_code: int, preview: GitOutputPreview):
        self.reason = reason
        self.exit_code = exit_code
        self.preview = preview
        super().__init__(message)


class GitCommandFailed(subprocess.CalledProcessError):
    def __init__(self, result: GitCommandResult):
        super().__init__(result.returncode, result.args, output=result.stdout, stderr=result.stderr)
        self.preview = result.preview


class GitCommandTimedOut(subprocess.TimeoutExpired):
    def __init__(self, timeout: subprocess.TimeoutExpired, preview: GitOutputPreview):
        super().__init__(timeout.cmd, timeout.timeout, output=preview.stdout, stderr=preview.stderr)
        self.preview = preview


class GitCommandResult(subprocess.CompletedProcess[bytes]):
    def __init__(self, command: list[str], returncode: int, preview: GitOutputPreview):
        super().__init__(command, returncode, stdout=preview.stdout, stderr=preview.stderr)
        self.preview = preview

    def check_returncode(self) -> None:
        if self.returncode:
            raise GitCommandFailed(self)


@dataclass(frozen=True)
class WorkspaceDiffResult:
    diff: GitCommandResult
    numstat: GitCommandResult


def _read_output_preview(stream: BinaryIO, max_bytes: int) -> tuple[bytes, bool]:
    stream.seek(0)
    data = stream.read(max_bytes + 1)
    return data[:max_bytes], len(data) > max_bytes


def _git_output_preview(
    stdout_file: BinaryIO, stderr_file: BinaryIO, *, max_stdout_bytes: int, max_stderr_bytes: int,
    artifact_root: Path | None = None,
) -> GitOutputPreview:
    stdout, stdout_truncated = _read_output_preview(stdout_file, max_stdout_bytes)
    stderr, stderr_truncated = _read_output_preview(stderr_file, max_stderr_bytes)
    artifacts: list[ToolArtifactResult] = []
    if artifact_root is not None:
        written_paths: list[Path] = []
        try:
            for stream, truncated, kind in (
                (stdout_file, stdout_truncated, "tool_stdout"),
                (stderr_file, stderr_truncated, "tool_stderr"),
            ):
                if not truncated:
                    continue
                stream.seek(0)
                artifact_id = uuid4()
                artifact_path, content_hash = write_artifact_file(
                    artifact_root, artifact_id, stream, chunk_size=_GIT_ARTIFACT_CHUNK_BYTES,
                )
                written_paths.append(artifact_path)
                artifacts.append(ToolArtifactResult(
                    id=artifact_id, kind=kind, content_ref=str(artifact_path.absolute()),
                    content_hash=content_hash,
                ))
        except BaseException as error:
            for path in written_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    error.add_note(f"Git output Artifact cleanup failed: {cleanup_error}")
            raise
    return GitOutputPreview(stdout, stderr, stdout_truncated, stderr_truncated, tuple(artifacts))


def _wait_for_git(
    process: subprocess.Popen[bytes] | WindowsJobProcess, *, deadline: float, timeout_seconds: float,
    stop_event: Event | None,
) -> int:
    while True:
        if stop_event is not None and stop_event.is_set():
            raise GitExecutionCancelledError("Git execution cancelled while waiting for the process")
        remaining_seconds = deadline - monotonic()
        if remaining_seconds <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout_seconds)
        try:
            return process.wait(timeout=min(_GIT_POLL_INTERVAL_SECONDS, remaining_seconds))
        except subprocess.TimeoutExpired:
            # A polling interval elapsed. The original command deadline still applies.
            continue


def _start_git_process(
    command: list[str], *, cwd: Path | None, stdout: BinaryIO, stderr: BinaryIO,
    pythonpath: Path | None = None,
):
    env = {**environ, "GIT_TERMINAL_PROMPT": "0"}
    if pythonpath is not None:
        env["PYTHONPATH"] = str(pythonpath)
    if platform_name == "nt":
        return start_windows_job_process(command, cwd=cwd, env=env, stdout=stdout, stderr=stderr)
    process = subprocess.Popen(
        command, shell=False, cwd=cwd, env=env,
        stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
    )
    return nullcontext(process)


def _stop_git_process(process: subprocess.Popen[bytes] | WindowsJobProcess) -> None:
    if platform_name == "nt" and isinstance(process, WindowsJobProcess):
        process.terminate_tree(cleanup_timeout_seconds=_GIT_CLEANUP_TIMEOUT_SECONDS)
    elif process.poll() is None:
        process.kill()
        process.wait(timeout=_GIT_CLEANUP_TIMEOUT_SECONDS)


def _run_git(
    args: list[str], *, timeout_seconds: int, cwd: Path | None = None,
    stop_event: Event | None = None,
    max_stdout_bytes: int = _GIT_LOG_PREVIEW_BYTES, max_stderr_bytes: int = _GIT_LOG_PREVIEW_BYTES,
    artifact_root: Path | None = None,
) -> GitCommandResult:
    return _run_managed_process(
        ["git", *args], timeout_seconds=timeout_seconds, cwd=cwd, stop_event=stop_event,
        max_stdout_bytes=max_stdout_bytes, max_stderr_bytes=max_stderr_bytes, artifact_root=artifact_root,
    )


def _run_managed_process(
    command: list[str], *, timeout_seconds: float, cwd: Path | None = None,
    stop_event: Event | None = None,
    max_stdout_bytes: int = _GIT_LOG_PREVIEW_BYTES, max_stderr_bytes: int = _GIT_LOG_PREVIEW_BYTES,
    artifact_root: Path | None = None, pythonpath: Path | None = None,
) -> GitCommandResult:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if max_stdout_bytes <= 0 or max_stderr_bytes <= 0:
        raise ValueError("Git output preview limits must be positive")
    with TemporaryFile() as stdout_file, TemporaryFile() as stderr_file:
        if stop_event is not None and stop_event.is_set():
            raise GitExecutionCancelledError("Git execution cancelled before process start")
        deadline = monotonic() + timeout_seconds
        process_kwargs = {"cwd": cwd, "stdout": stdout_file, "stderr": stderr_file}
        if pythonpath is not None:
            process_kwargs["pythonpath"] = pythonpath
        with _start_git_process(command, **process_kwargs) as process:
            try:
                returncode = _wait_for_git(
                    process, deadline=deadline, timeout_seconds=timeout_seconds, stop_event=stop_event,
                )
            except BaseException as error:
                try:
                    _stop_git_process(process)
                except (OSError, subprocess.TimeoutExpired) as cleanup_error:
                    raise GitProcessCleanupError(
                        f"Git process cleanup could not be confirmed: {cleanup_error}"
                    ) from error
                if isinstance(error, subprocess.TimeoutExpired):
                    preview = _git_output_preview(
                        stdout_file, stderr_file, max_stdout_bytes=max_stdout_bytes,
                        max_stderr_bytes=max_stderr_bytes, artifact_root=artifact_root,
                    )
                    raise GitCommandTimedOut(error, preview) from error
                raise
        preview = _git_output_preview(
            stdout_file, stderr_file, max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes, artifact_root=artifact_root,
        )
        result = GitCommandResult(command, returncode, preview)
    result.check_returncode()
    return result


def run_registered_command(
    workspace_root: Path, command_name: str, *,
    registry: Mapping[str, tuple[str, ...]], timeout_seconds: float, stop_event: Event,
    artifact_root: Path | None = None, max_stdout_bytes: int = _GIT_LOG_PREVIEW_BYTES,
    pythonpath_relative: str | None = None,
) -> GitCommandResult:
    """Execute a trusted registry entry without shell parsing or agent-supplied arguments."""
    command = registry.get(command_name)
    if command is None:
        raise ValueError("Command is not registered")
    if not isinstance(command, tuple) or not command or any(not isinstance(part, str) or not part for part in command):
        raise ValueError("Registered command must be a non-empty tuple of arguments")
    executable = Path(command[0])
    if not executable.is_absolute() or (platform_name == "nt" and executable.suffix.lower() != ".exe"):
        raise ValueError("Registered command must use an absolute executable path")
    if stop_event is None:
        raise ValueError("A stop event is required")
    root = workspace_root.resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError("Run workspace root must be a directory")
    pythonpath = None
    if pythonpath_relative is not None:
        path = Path(pythonpath_relative)
        if (not pythonpath_relative or path.anchor or PureWindowsPath(pythonpath_relative).anchor
                or any(part in (".", "..") or part.casefold() == ".git" for part in path.parts)):
            raise WorkspacePathError("Python source path must stay within the workspace")
        try:
            pythonpath = (root / path).resolve(strict=True)
        except OSError as error:
            raise WorkspacePathError("Python source directory is unavailable") from error
        if (not pythonpath.is_dir() or not pythonpath.is_relative_to(root)
                or any(part.casefold() == ".git" for part in pythonpath.relative_to(root).parts)):
            raise WorkspacePathError("Python source path must be a workspace directory")
    return _run_managed_process(
        list(command), cwd=root, timeout_seconds=timeout_seconds, stop_event=stop_event,
        artifact_root=artifact_root, max_stdout_bytes=max_stdout_bytes, pythonpath=pythonpath,
    )


@dataclass(frozen=True)
class RepositoryHeadObservation:
    status: str
    base_commit: str | None = None


def observe_repository_head(repo: str, *, timeout_seconds: int) -> RepositoryHeadObservation:
    """Read HEAD of a caller-trusted repository without treating failures as rejection."""
    if not isinstance(repo, str) or not repo.strip():
        raise ValueError("repo must be a non-blank string")
    try:
        result = _run_git(["ls-remote", "--", repo, "HEAD"], timeout_seconds=timeout_seconds)
    except GitCommandTimedOut:
        return RepositoryHeadObservation(status="timeout")
    except GitCommandFailed:
        return RepositoryHeadObservation(status="unavailable")
    if result.preview.stdout_truncated:
        return RepositoryHeadObservation(status="invalid_output")
    match = fullmatch(rb"([0-9a-f]{40}(?:[0-9a-f]{24})?)\tHEAD\r?\n?", result.stdout)
    if match is None:
        return RepositoryHeadObservation(status="invalid_output")
    return RepositoryHeadObservation(status="observed", base_commit=match.group(1).decode("ascii"))


def clone_repository(
    repo: str, workspace_root: Path, *, timeout_seconds: int,
    stop_event: Event | None = None,
    artifact_root: Path | None = None,
) -> GitCommandResult:
    """Clone an admitted repository into the Worker-prepared, empty Run directory."""
    if not repo.strip():
        raise ValueError("Repository must not be blank")
    target = workspace_root.resolve(strict=True)
    if not target.is_dir() or any(target.iterdir()):
        raise WorkspacePathError("Clone requires an empty Run workspace directory")

    return _run_git(
        ["clone", "--quiet", "--no-checkout", "--", repo, str(target)],
        timeout_seconds=timeout_seconds, stop_event=stop_event, artifact_root=artifact_root,
    )


def checkout_base_commit(
    workspace_root: Path, base_commit: str, *, timeout_seconds: int,
    stop_event: Event | None = None,
) -> str:
    """Check out the Task's pinned commit and return the independently verified HEAD."""
    if not fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", base_commit):
        raise ValueError("Expected a full lowercase hexadecimal base_commit")
    target = workspace_root.resolve(strict=True)
    git_dir = target / ".git"
    if not git_dir.is_dir() or git_dir.resolve().parts != git_dir.parts:
        raise WorkspacePathError("Checkout requires a local .git directory in the Run workspace")

    git_args = [f"--git-dir={git_dir}", f"--work-tree={target}"]
    _run_git(
        [*git_args, "checkout", "--quiet", "--detach", base_commit, "--"],
        cwd=target, timeout_seconds=timeout_seconds, stop_event=stop_event,
    )
    result = _run_git(
        [*git_args, "rev-parse", "HEAD"], cwd=target,
        timeout_seconds=timeout_seconds, stop_event=stop_event, max_stdout_bytes=len(base_commit) + 2,
    )
    if result.preview.stdout_truncated:
        raise GitHeadOutputError(
            "Run workspace HEAD output was truncated", reason="HEAD_OUTPUT_TRUNCATED",
            exit_code=result.returncode, preview=result.preview,
        )
    head_match = fullmatch(rb"([0-9a-f]{40}(?:[0-9a-f]{24})?)\r?\n", result.stdout)
    if head_match is None:
        raise GitHeadOutputError(
            "Run workspace HEAD output must contain exactly one full commit ID", reason="HEAD_INVALID_OUTPUT",
            exit_code=result.returncode, preview=result.preview,
        )
    actual_commit = head_match[1].decode("ascii")
    if actual_commit != base_commit:
        raise GitHeadMismatchError(base_commit, actual_commit, result.returncode)
    return actual_commit


def inspect_workspace_diff(
    workspace_root: Path, base_commit: str, *, timeout_seconds: float, stop_event: Event,
    artifact_root: Path,
) -> WorkspaceDiffResult:
    """Read a tracked diff against the pinned base, rejecting omitted untracked files."""
    if not fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", base_commit):
        raise ValueError("Expected a full lowercase hexadecimal base_commit")
    root = workspace_root.resolve(strict=True)
    git_dir = root / ".git"
    if not root.is_dir() or not git_dir.is_dir() or git_dir.resolve().parts != git_dir.parts:
        raise WorkspacePathError("Diff requires a local .git directory in the Run workspace")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    deadline = monotonic() + timeout_seconds
    git_args = [f"--git-dir={git_dir}", f"--work-tree={root}"]
    untracked = _run_git(
        [*git_args, "ls-files", "--others", "-z"],
        cwd=root, timeout_seconds=timeout_seconds, stop_event=stop_event, artifact_root=artifact_root,
    )
    if untracked.stdout or untracked.preview.stdout_truncated:
        raise WorkspaceUntrackedFilesError(untracked.preview)
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise GitCommandTimedOut(subprocess.TimeoutExpired("git diff", timeout_seconds), untracked.preview)
    numstat = _run_git(
        [*git_args, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--numstat", "-z", base_commit, "--"],
        cwd=root, timeout_seconds=remaining, stop_event=stop_event, artifact_root=artifact_root,
    )
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise GitCommandTimedOut(subprocess.TimeoutExpired("git diff", timeout_seconds), numstat.preview)
    diff = _run_git(
        [*git_args, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "--binary", "--full-index",
         base_commit, "--"], cwd=root, timeout_seconds=remaining, stop_event=stop_event,
        artifact_root=artifact_root,
    )
    return WorkspaceDiffResult(diff=diff, numstat=numstat)


def verify_baseline_workspace(workspace_root: Path, base_commit: str, *, timeout_seconds: float,
                              stop_event: Event, artifact_root: Path) -> None:
    """Check HEAD and workspace against the pinned commit before baseline tests."""
    deadline = monotonic() + timeout_seconds
    root = workspace_root.resolve(strict=True)
    git_dir = root / ".git"
    if not fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", base_commit):
        raise ValueError("Expected a full lowercase hexadecimal base_commit")
    if not git_dir.is_dir() or git_dir.resolve().parts != git_dir.parts:
        raise WorkspacePathError("Baseline requires a local .git directory")
    head = _run_git([f"--git-dir={git_dir}", f"--work-tree={root}", "rev-parse", "HEAD"],
                    cwd=root, timeout_seconds=timeout_seconds, stop_event=stop_event,
                    max_stdout_bytes=len(base_commit) + 2)
    if head.preview.stdout_truncated or head.stdout.strip() != base_commit.encode("ascii"):
        raise ValueError("Baseline HEAD does not match the pinned commit")
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise GitCommandTimedOut(subprocess.TimeoutExpired("baseline workspace", timeout_seconds), head.preview)
    diff = inspect_workspace_diff(root, base_commit, timeout_seconds=remaining,
                                  stop_event=stop_event, artifact_root=artifact_root)
    if diff.diff.stdout or diff.diff.preview.stdout_truncated or diff.numstat.stdout or diff.numstat.preview.stdout_truncated:
        raise ValueError("Baseline workspace differs from the pinned commit")


def resolve_workspace_file(workspace_root: Path, relative_path: str) -> Path:
    """Resolve an existing file under the trusted Run workspace root."""
    path = Path(relative_path)
    if not relative_path or path.anchor or PureWindowsPath(relative_path).anchor:
        raise WorkspacePathError("Provide a relative workspace path")

    root = workspace_root.resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError("Run workspace root must be a directory")
    requested = root / path
    missing_error = None
    try:
        target = requested.resolve(strict=True)
    except FileNotFoundError as error:
        missing_error = error
        # Check the directory boundary even when the last path component is missing.
        target = requested.resolve(strict=False)
    # Compare the resolved spelling too, including case-sensitive Windows directories.
    if not target.is_relative_to(root) or target.parts[:len(root.parts)] != root.parts:
        raise WorkspacePathError("File is outside the Run workspace")
    if missing_error is not None:
        raise WorkspaceFileNotFoundError("Workspace file was not found") from missing_error
    if not target.is_file():
        raise WorkspacePathError("Workspace path must point to a regular file")
    return target


def validate_patch_paths(
    workspace_root: Path, patch: bytes, *, approved_paths: set[str], stop_event: Event | None = None,
) -> frozenset[str]:
    """Inspect Git's parsed paths before a caller attempts to apply this patch."""
    if not isinstance(patch, bytes) or not patch:
        raise ValueError("Patch must contain bytes")
    if any(
        line.startswith((b"rename from ", b"rename to ", b"copy from ", b"copy to "))
        for line in patch.splitlines()
    ):
        raise WorkspacePathError("Patch renames and copies are unsupported")
    if any(
        line.startswith((b"old mode ", b"new mode ", b"new file mode ", b"deleted file mode "))
        for line in patch.splitlines()
    ):
        raise WorkspacePathError("Patch file mode changes are unsupported")
    root = workspace_root.resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError("Run workspace root must be a directory")
    with TemporaryDirectory(prefix="devflow-patch-check-") as directory:
        patch_file = Path(directory) / "input.patch"
        patch_file.write_bytes(patch)
        try:
            result = _run_git(
                ["apply", "--recount", "--numstat", "-z", "--", str(patch_file)],
                cwd=root, timeout_seconds=10, stop_event=stop_event, max_stdout_bytes=1024 * 1024,
            )
        except GitCommandFailed as error:
            raise PatchFormatError(error.preview.stderr) from None
    output = result.preview.stdout
    if result.preview.stdout_truncated or not output or not output.endswith(b"\0"):
        raise WorkspacePathError("Patch paths could not be parsed")

    # --recount may treat a later bare unified-diff header as hunk text. Never
    # authorize a patch if Git did not report every declared file section.
    lines = patch.splitlines()
    declared_sections = sum(1 for first, second in zip(lines, lines[1:])
                            if first.startswith(b"--- ") and second.startswith(b"+++ "))
    if declared_sections != len(output[:-1].split(b"\0")):
        raise WorkspacePathError("Patch contains an unparsed file section")

    paths = set()
    for entry in output[:-1].split(b"\0"):
        parts = entry.split(b"\t", 2)
        if len(parts) != 3 or not parts[2] or not parts[0].isdigit() or not parts[1].isdigit():
            raise WorkspacePathError("Unsupported patch path or operation")
        try:
            name = parts[2].decode("utf-8")
        except UnicodeDecodeError:
            raise WorkspacePathError("Patch path must be UTF-8") from None
        windows_path = PureWindowsPath(name)
        if (not name or "\\" in name or windows_path.anchor or
                any(part in ("", ".", "..") for part in name.split("/")) or
                any(part.casefold() == ".git" for part in name.split("/"))):
            raise WorkspacePathError("Invalid patch path")
        if name not in approved_paths:
            raise WorkspacePathError("Patch path is outside the approved plan")
        target = (root / name).resolve(strict=False)
        if not target.is_relative_to(root) or target.parts[:len(root.parts)] != root.parts:
            raise WorkspacePathError("Patch path is outside the Run workspace")
        paths.add(name)
    return frozenset(paths)


def apply_validated_patch(
    workspace_root: Path, patch: bytes, *, timeout_seconds: int, stop_event: Event,
) -> None:
    """Apply previously checked patch bytes through the managed Git process."""
    with TemporaryDirectory(prefix="devflow-patch-apply-") as directory:
        patch_file = Path(directory) / "input.patch"
        patch_file.write_bytes(patch)
        _run_git(
            ["apply", "--recount", "--", str(patch_file)], cwd=workspace_root,
            timeout_seconds=timeout_seconds, stop_event=stop_event,
        )


def _open_workspace_file(
    workspace_root: Path, relative_path: str, *, max_bytes: int,
) -> BinaryIO:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    target = resolve_workspace_file(workspace_root, relative_path)
    return target.open("rb")


def read_file(
    workspace_root: Path, relative_path: str, *, max_bytes: int,
) -> tuple[bytes, bool]:
    """Return the bounded file prefix and whether it was truncated."""
    with _open_workspace_file(workspace_root, relative_path, max_bytes=max_bytes) as file:
        # One extra byte distinguishes an exact fit from a truncated result.
        data = file.read(max_bytes + 1)

    truncated = len(data) > max_bytes
    return data[:max_bytes], truncated


def read_text_file(
    workspace_root: Path, relative_path: str, *, max_bytes: int,
) -> tuple[str, bool]:
    """Decode a bounded UTF-8 prefix, raising UnicodeDecodeError for invalid input."""
    data, truncated = read_file(workspace_root, relative_path, max_bytes=max_bytes)
    return _decode_text(data, truncated=truncated), truncated


def _decode_text(data: bytes, *, truncated: bool) -> str:
    decoder = getincrementaldecoder("utf-8")(errors="strict")
    return decoder.decode(data, final=not truncated)


def read_text_file_with_artifact(
    workspace_root: Path, relative_path: str, *, max_bytes: int,
    artifact_root: Path, artifact_id: UUID, chunk_size: int,
) -> tuple[str, tuple[Path, str] | None]:
    """Return the UTF-8 preview and, if truncated, the Artifact path and digest."""
    with _open_workspace_file(workspace_root, relative_path, max_bytes=max_bytes) as source:
        data = source.read(max_bytes + 1)
        truncated = len(data) > max_bytes
        preview = _decode_text(data[:max_bytes], truncated=truncated)
        if not truncated:
            return preview, None

        artifact = write_artifact_file(
            artifact_root, artifact_id, source, chunk_size=chunk_size, initial_data=data,
        )
        return preview, artifact
