from contextlib import nullcontext
from hashlib import sha256
from io import BytesIO
import os
import subprocess
import sys
from threading import Event, Timer
from unittest.mock import Mock
from uuid import uuid4

import pytest

import devflow_ai.harness as harness_module
from devflow_ai.enums import AgentRole
from devflow_ai.harness import (
    GitExecutionCancelledError,
    GitProcessCleanupError,
    WorkspaceFileNotFoundError,
    WorkspacePathError,
    check_tool_permission,
    checkout_base_commit,
    clone_repository,
    read_file,
    read_text_file,
    read_text_file_with_artifact,
    resolve_workspace_file,
)
from devflow_ai.policy import PolicyPermissions
from devflow_ai.process_evidence import proves_process_not_started


def test_baseline_workspace_requires_pinned_clean_git_state(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True, timeout=15).stdout
    git("init", "--quiet")
    source = repo / "app.py"
    source.write_bytes(b"value = 1\n")
    git("add", "app.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
        "commit", "--quiet", "-m", "baseline")
    base = git("rev-parse", "HEAD").decode("ascii").strip()
    def check(commit=base):
        harness_module.verify_baseline_workspace(repo, commit, timeout_seconds=15, stop_event=Event(),
                                                  artifact_root=artifacts)
    check()
    with pytest.raises(ValueError, match="HEAD"):
        check("a" * 40)
    source.write_bytes(b"value = 2\n")
    with pytest.raises(ValueError, match="differs"):
        check()
    source.write_bytes(b"value = 1\n")
    (repo / "extra.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(harness_module.WorkspaceUntrackedFilesError):
        check()


def test_patch_path_inspection_rejects_unapproved_files_before_writing(tmp_path):
    patch = (
        b"diff --git a/auth.py b/auth.py\n--- a/auth.py\n+++ b/auth.py\n"
        b"@@ -1 +1 @@\n-old\n+new\n"
        b"diff --git a/config.py b/config.py\n--- a/config.py\n+++ b/config.py\n"
        b"@@ -1 +1 @@\n-old\n+new\n"
    )
    (tmp_path / "auth.py").write_text("old\n")
    (tmp_path / "config.py").write_text("old\n")
    with pytest.raises(WorkspacePathError, match="approved plan"):
        harness_module.validate_patch_paths(tmp_path, patch, approved_paths={"auth.py"})
    assert (tmp_path / "auth.py").read_text() == "old\n"
    assert (tmp_path / "config.py").read_text() == "old\n"
    assert harness_module.validate_patch_paths(
        tmp_path, patch, approved_paths={"auth.py", "config.py"},
    ) == {"auth.py", "config.py"}


def test_registered_command_runs_exact_arguments_and_captures_result(tmp_path):
    registry = {"test": (sys.executable, "-c", "print('registered')")}
    result = harness_module.run_registered_command(
        tmp_path, "test", registry=registry, timeout_seconds=5, stop_event=Event(),
    )
    assert result.returncode == 0
    assert result.stdout.strip() == b"registered"
    with pytest.raises(ValueError, match="not registered"):
        harness_module.run_registered_command(
            tmp_path, "test; arbitrary", registry=registry, timeout_seconds=5, stop_event=Event(),
        )


def test_registered_command_can_use_a_workspace_source_directory_without_global_env_changes(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "example_module.py").write_text("value = 'workspace'\n", encoding="utf-8")
    command = {"test": (sys.executable, "-B", "-c", "import example_module; print(example_module.value)")}
    result = harness_module.run_registered_command(
        tmp_path, "test", registry=command, timeout_seconds=5, stop_event=Event(), pythonpath_relative="src",
    )
    assert result.stdout.strip() == b"workspace"
    assert "PYTHONPATH" not in os.environ
    with pytest.raises(WorkspacePathError):
        harness_module.run_registered_command(
            tmp_path, "test", registry=command, timeout_seconds=5, stop_event=Event(), pythonpath_relative="../outside",
        )
    metadata = tmp_path / ".git"
    metadata.mkdir()
    alias = tmp_path / "metadata_alias"
    try:
        alias.symlink_to(metadata, target_is_directory=True)
    except OSError as error:
        if getattr(error, "winerror", None) != 1314:
            raise
        from _winapi import CreateJunction
        CreateJunction(str(metadata), str(alias))
    with pytest.raises(WorkspacePathError):
        harness_module.run_registered_command(
            tmp_path, "test", registry=command, timeout_seconds=5, stop_event=Event(), pythonpath_relative="metadata_alias",
        )


def test_registered_command_reports_failure_and_rejects_relative_executable(tmp_path):
    with pytest.raises(harness_module.GitCommandFailed) as exc_info:
        harness_module.run_registered_command(
            tmp_path, "test", registry={"test": (sys.executable, "-c", "import sys; sys.exit(7)")},
            timeout_seconds=5, stop_event=Event(),
        )
    assert exc_info.value.returncode == 7
    with pytest.raises(ValueError, match="absolute executable"):
        harness_module.run_registered_command(
            tmp_path, "test", registry={"test": ("python", "-c", "print(1)")},
            timeout_seconds=5, stop_event=Event(),
        )


def test_registered_command_stops_a_timed_out_process(tmp_path):
    with pytest.raises(harness_module.GitCommandTimedOut):
        harness_module.run_registered_command(
            tmp_path, "test", registry={"test": (sys.executable, "-c", "import time; time.sleep(30)")},
            timeout_seconds=1, stop_event=Event(),
        )


def test_only_the_process_boundary_can_prove_a_command_never_started(tmp_path):
    """The proof comes from the spawn boundary, not from the exception type."""
    with pytest.raises(ValueError, match="absolute executable") as rejected:
        harness_module.run_registered_command(
            tmp_path, "test", registry={"test": ("python", "-c", "print(1)")},
            timeout_seconds=5, stop_event=Event(),
        )
    assert proves_process_not_started(rejected.value) is True

    # A validation error raised in the same call shape, but after a child really ran,
    # carries no proof: nothing may be concluded from its type alone.
    result = harness_module.run_registered_command(
        tmp_path, "test", registry={"test": (sys.executable, "-B", "-c", "print('started')")},
        timeout_seconds=15, stop_event=Event(),
    )
    assert result.stdout.strip() == b"started"
    try:
        raise ValueError("absolute executable")
    except ValueError as error:
        assert proves_process_not_started(error) is False

    # A command that started and then failed is a result, not a failure to start.
    with pytest.raises(harness_module.GitCommandFailed) as failed:
        harness_module.run_registered_command(
            tmp_path, "test", registry={"test": (sys.executable, "-B", "-c", "raise SystemExit(2)")},
            timeout_seconds=15, stop_event=Event(),
        )
    assert proves_process_not_started(failed.value) is False


def test_patch_path_inspection_rejects_rename_and_unparseable_patch(tmp_path):
    rename = (
        b"diff --git a/auth.py b/config.py\nsimilarity index 100%\n"
        b"rename from auth.py\nrename to config.py\n"
    )
    for patch in (rename, b"not a patch"):
        with pytest.raises(WorkspacePathError):
            harness_module.validate_patch_paths(tmp_path, patch, approved_paths={"auth.py", "config.py"})


def test_patch_path_inspection_rejects_parent_path(tmp_path):
    patch = (
        b"diff --git a/../outside.py b/../outside.py\n"
        b"--- a/../outside.py\n+++ b/../outside.py\n@@ -1 +1 @@\n-old\n+new\n"
    )
    with pytest.raises(WorkspacePathError):
        harness_module.validate_patch_paths(tmp_path, patch, approved_paths={"../outside.py"})


def test_patch_path_inspection_respects_stop_before_start(tmp_path, monkeypatch):
    stop_event = Event()
    stop_event.set()
    start = Mock()
    monkeypatch.setattr(harness_module, "_start_git_process", start)
    patch = b"--- a/auth.py\n+++ b/auth.py\n@@ -1 +1 @@\n-old\n+new\n"
    with pytest.raises(GitExecutionCancelledError):
        harness_module.validate_patch_paths(
            tmp_path, patch, approved_paths={"auth.py"}, stop_event=stop_event,
        )
    start.assert_not_called()


def test_patch_path_inspection_rejects_symlink_mode_change(tmp_path):
    patch = (
        b"diff --git a/auth.py b/auth.py\nold mode 100644\nnew mode 120000\n"
        b"--- a/auth.py\n+++ b/auth.py\n@@ -1 +1 @@\n-old\n+outside\n"
    )
    with pytest.raises(WorkspacePathError, match="mode changes"):
        harness_module.validate_patch_paths(tmp_path, patch, approved_paths={"auth.py"})


def test_clone_and_checkout_pin_an_older_commit_in_a_literal_workspace_path(tmp_path):
    repo = tmp_path / "source & repository"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, check=True, timeout=15,
        ).stdout

    git("init", "--quiet")
    commit_args = (
        "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "--quiet", "-m",
    )
    (repo / "README.md").write_bytes(b"source content")
    git("add", "README.md")
    git(*commit_args, "initial")
    base_commit = git("rev-parse", "HEAD").decode("ascii").strip()
    (repo / "README.md").write_bytes(b"newer content")
    git("add", "README.md")
    git(*commit_args, "advanced default branch")

    workspace_root = tmp_path / "Run workspace"
    workspace_root.mkdir()
    result = clone_repository(str(repo), workspace_root, timeout_seconds=15)

    assert result.returncode == 0
    assert (workspace_root / ".git").is_dir()
    assert not (workspace_root / "README.md").exists()
    assert checkout_base_commit(workspace_root, base_commit, timeout_seconds=15) == base_commit
    assert (workspace_root / "README.md").read_bytes() == b"source content"
    assert (workspace_root / ".git" / "HEAD").read_text(encoding="ascii").strip() == base_commit
    assert git("rev-parse", "HEAD").decode("ascii").strip() != base_commit


@pytest.mark.parametrize(("output", "truncated", "message"), [
    (b"b" * 40 + b"\n", False, "expected " + "a" * 40 + ", got " + "b" * 40),
    (b"a" * 40 + b"\n", True, "HEAD output was truncated"),
    (b"a" * 40 + b"\n\n", False, "exactly one full commit ID"),
    (b"a" * 8 + b"\n", False, "exactly one full commit ID"),
])
def test_checkout_base_commit_rejects_unverified_head(tmp_path, monkeypatch, output, truncated, message):
    (tmp_path / ".git").mkdir()
    expected = "a" * 40
    preview = harness_module.GitOutputPreview(output, b"", truncated, False)
    git = Mock(side_effect=[
        subprocess.CompletedProcess([], 0, stdout=b""),
        harness_module.GitCommandResult([], 0, preview),
    ])
    monkeypatch.setattr(harness_module, "_run_git", git)

    with pytest.raises(ValueError, match=message) as exc_info:
        checkout_base_commit(tmp_path, expected, timeout_seconds=15)
    assert git.call_count == 2
    assert git.call_args.kwargs["max_stdout_bytes"] == 42
    if output == b"b" * 40 + b"\n":
        mismatch = exc_info.value
        assert isinstance(mismatch, harness_module.GitHeadMismatchError)
        assert (mismatch.expected_commit, mismatch.actual_commit, mismatch.exit_code) == (expected, "b" * 40, 0)
    else:
        error = exc_info.value
        assert isinstance(error, harness_module.GitHeadOutputError)
        assert error.reason == ("HEAD_OUTPUT_TRUNCATED" if truncated else "HEAD_INVALID_OUTPUT")
        assert error.exit_code == 0 and error.preview is preview


def test_clone_repository_refuses_to_reuse_nonempty_workspace(tmp_path):
    existing_file = tmp_path / "keep.txt"
    existing_file.write_bytes(b"existing data")
    with pytest.raises(WorkspacePathError, match="empty Run workspace"):
        clone_repository("unused", tmp_path, timeout_seconds=15)
    assert existing_file.read_bytes() == b"existing data"


def test_clone_repository_does_not_start_when_stopped(tmp_path, monkeypatch):
    stop_event = Event()
    stop_event.set()
    start = Mock()
    monkeypatch.setattr(harness_module, "_start_git_process", start)

    with pytest.raises(GitExecutionCancelledError):
        clone_repository("unused", tmp_path, timeout_seconds=15, stop_event=stop_event)

    start.assert_not_called()


def test_checkout_skips_head_verification_when_stopped_between_commands(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    stop_event = Event()

    def finish_checkout(*, timeout):
        stop_event.set()
        return 0

    process = Mock()
    process.wait.side_effect = finish_checkout
    start = Mock(return_value=nullcontext(process))
    monkeypatch.setattr(harness_module, "_start_git_process", start)
    with pytest.raises(GitExecutionCancelledError):
        checkout_base_commit(tmp_path, "a" * 40, timeout_seconds=15, stop_event=stop_event)

    start.assert_called_once()
    assert "checkout" in start.call_args.args[0]


def test_git_polling_uses_original_deadline_and_kills_on_timeout(tmp_path, monkeypatch):
    clock = iter([0, 0, 0.91, 0.98, 1.0])
    monkeypatch.setattr(harness_module, "monotonic", lambda: next(clock))
    process = Mock(args=["git", "clone"])
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired(process.args, 0.1) for _ in range(3)] + [1]

    def start_process(args, *, stdout, stderr, **kwargs):
        stdout.write(b"partial stdout")
        stderr.write(b"partial stderr")
        return nullcontext(process)

    monkeypatch.setattr(harness_module, "_start_git_process", start_process)
    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        harness_module._run_git(
            ["clone"], timeout_seconds=1, max_stdout_bytes=7, max_stderr_bytes=14,
            artifact_root=tmp_path,
        )

    waits = process.wait.call_args_list
    assert [call.kwargs["timeout"] for call in waits[:-1]] == pytest.approx([0.1, 0.09, 0.02])
    assert waits[-1].kwargs == {"timeout": 5.0}
    assert exc_info.value.timeout == 1
    assert (exc_info.value.output, exc_info.value.stderr) == (b"partial", b"partial stderr")
    assert exc_info.value.preview.stdout_truncated
    assert not exc_info.value.preview.stderr_truncated
    artifact, = exc_info.value.preview.artifacts
    assert artifact.kind == "tool_stdout"
    assert (tmp_path / f"{artifact.id}.bin").read_bytes() == b"partial stdout"
    process.kill.assert_called_once()


@pytest.mark.parametrize(("returncode", "stdout_limit"), [(0, 4), (1, 3)])
def test_git_bounds_previews_and_saves_full_outputs(tmp_path, monkeypatch, returncode, stdout_limit):
    process = Mock()
    process.wait.return_value = returncode

    def start_process(args, *, stdout, stderr, **kwargs):
        stdout.write(b"abcd")
        stderr.write(b"uvwxyz")
        return nullcontext(process)

    monkeypatch.setattr(harness_module, "_start_git_process", start_process)

    def run():
        return harness_module._run_git(
            ["clone"], timeout_seconds=1, max_stdout_bytes=stdout_limit, max_stderr_bytes=3,
            artifact_root=tmp_path,
        )

    if returncode:
        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            run()
        result = exc_info.value
    else:
        result = run()

    assert (result.stdout, result.stderr) == (b"abcd"[:stdout_limit], b"uvw")
    assert result.preview.stdout_truncated == (stdout_limit < 4)
    assert result.preview.stderr_truncated
    expected = {"tool_stderr": b"uvwxyz"}
    if stdout_limit < 4:
        expected["tool_stdout"] = b"abcd"
    assert {artifact.kind for artifact in result.preview.artifacts} == set(expected)
    for artifact in result.preview.artifacts:
        path = tmp_path / f"{artifact.id}.bin"
        assert str(path.absolute()) == artifact.content_ref
        assert path.read_bytes() == expected[artifact.kind]
        assert artifact.content_hash == sha256(expected[artifact.kind]).hexdigest()
    assert len(list(tmp_path.iterdir())) == len(expected)


def test_git_cleans_first_artifact_when_second_write_fails(tmp_path, monkeypatch):
    write_artifact_file = harness_module.write_artifact_file
    writes = 0

    def fail_second_write(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("second output failed")
        return write_artifact_file(*args, **kwargs)

    monkeypatch.setattr(harness_module, "write_artifact_file", fail_second_write)
    with pytest.raises(OSError, match="second output failed"):
        harness_module._git_output_preview(
            BytesIO(b"stdout"), BytesIO(b"stderr"), max_stdout_bytes=2, max_stderr_bytes=2,
            artifact_root=tmp_path,
        )
    assert list(tmp_path.iterdir()) == []


def test_git_cleanup_failure_keeps_the_original_timeout(monkeypatch):
    process = Mock()
    timeout = subprocess.TimeoutExpired(["git", "clone"], 1)
    monkeypatch.setattr(harness_module, "_start_git_process", Mock(return_value=nullcontext(process)))
    monkeypatch.setattr(harness_module, "_wait_for_git", Mock(side_effect=timeout))
    monkeypatch.setattr(harness_module, "_stop_git_process", Mock(side_effect=TimeoutError("still active")))

    with pytest.raises(GitProcessCleanupError, match="still active") as exc_info:
        harness_module._run_git(["clone"], timeout_seconds=1)

    assert exc_info.value.__cause__ is timeout


def test_git_stop_signal_terminates_a_running_process(monkeypatch):
    stop_event = Event()
    original_popen = subprocess.Popen
    timer = Timer(0.15, stop_event.set)
    processes = []

    def start_process(args, **kwargs):
        process = original_popen([sys.executable, "-c", "import time; time.sleep(10)"], **kwargs)
        processes.append(process)
        timer.start()
        return process

    monkeypatch.setattr(harness_module, "_start_git_process", start_process)
    try:
        with pytest.raises(GitExecutionCancelledError, match="while waiting"):
            harness_module._run_git(["clone"], timeout_seconds=5, stop_event=stop_event)
        assert processes[0].poll() is not None
    finally:
        timer.cancel()
        if timer.ident is not None:
            timer.join()
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.parametrize(
    ("tool_name", "role", "expected"),
    [
        ("read_file", AgentRole.EXPLORER, None),
        ("apply_patch", AgentRole.EXPLORER, "TOOL_NOT_ALLOWED_FOR_ROLE"),
        ("apply_patch", AgentRole.CODER, None),
        ("read_file", AgentRole.SUPERVISOR, "TOOL_NOT_ALLOWED_FOR_ROLE"),
        ("retired_tool", AgentRole.EXPLORER, "TOOL_NOT_REGISTERED"),
    ],
)
def test_tool_permission_requires_registration_and_role_grant(tool_name, role, expected):
    allowed = {role: [] for role in AgentRole}
    allowed[AgentRole.EXPLORER] = ["read_file", "retired_tool"]
    allowed[AgentRole.CODER] = ["apply_patch"]
    permissions = PolicyPermissions(allowed_tools_by_role=allowed)

    assert check_tool_permission(
        tool_name, role, {"read_file", "apply_patch"}, permissions,
    ) == expected


def test_resolve_workspace_file_checks_the_resolved_directory_boundary(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    sibling = tmp_path / "run-other"
    sibling.mkdir()
    inside = root / "README.md"
    outside = sibling / "secret.txt"
    inside.touch()
    outside.touch()

    assert resolve_workspace_file(root, "README.md") == inside.resolve()
    for path in (str(inside), r"C:README.md", r"\README.md"):
        with pytest.raises(WorkspacePathError, match="relative"):
            resolve_workspace_file(root, path)
    with pytest.raises(WorkspacePathError, match="outside"):
        resolve_workspace_file(root, "../run-other/secret.txt")
    with pytest.raises(WorkspacePathError, match="regular file"):
        resolve_workspace_file(root, ".")


def test_resolve_workspace_file_rejects_link_to_an_external_directory(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    outside = tmp_path / "external"
    outside.mkdir()
    (outside / "notes.md").touch()
    link = root / "docs"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        if getattr(error, "winerror", None) != 1314:
            raise
        # Windows junctions exercise the same resolved-path check without symlink privileges.
        from _winapi import CreateJunction

        CreateJunction(str(outside), str(link))

    with pytest.raises(WorkspacePathError, match="outside"):
        resolve_workspace_file(root, "docs/notes.md")
    with pytest.raises(WorkspacePathError, match="outside"):
        resolve_workspace_file(root, "docs/missing.md")


def test_resolve_workspace_file_distinguishes_missing_target_from_missing_root(tmp_path):
    with pytest.raises(WorkspaceFileNotFoundError):
        resolve_workspace_file(tmp_path, "missing.txt")

    with pytest.raises(FileNotFoundError) as exc_info:
        resolve_workspace_file(tmp_path / "missing-workspace", "notes.txt")
    assert not isinstance(exc_info.value, WorkspaceFileNotFoundError)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"", (b"", False)),
        (b"ab", (b"ab", False)),
        (b"abc", (b"abc", False)),
        (b"abcd", (b"abc", True)),
    ],
)
def test_read_file_returns_bounded_prefix(tmp_path, content, expected):
    (tmp_path / "notes.txt").write_bytes(content)

    assert read_file(tmp_path, "notes.txt", max_bytes=3) == expected


def test_read_file_rejects_non_positive_limit(tmp_path):
    with pytest.raises(ValueError, match="max_bytes"):
        read_file(tmp_path, "notes.txt", max_bytes=0)


@pytest.mark.parametrize(
    ("max_bytes", "expected"),
    [
        (2, ("a", True)),
        (4, ("a中", True)),
        (5, ("a中b", False)),
    ],
)
def test_read_text_file_preserves_complete_utf8_characters(tmp_path, max_bytes, expected):
    (tmp_path / "notes.txt").write_bytes("a中b".encode("utf-8"))

    assert read_text_file(tmp_path, "notes.txt", max_bytes=max_bytes) == expected


@pytest.mark.parametrize(
    ("content", "max_bytes"),
    [(b"a\xe4", 2), (b"a\xffbc", 3)],
)
def test_read_text_file_raises_on_invalid_utf8(tmp_path, content, max_bytes):
    (tmp_path / "notes.txt").write_bytes(content)

    with pytest.raises(UnicodeDecodeError):
        read_text_file(tmp_path, "notes.txt", max_bytes=max_bytes)


@pytest.mark.parametrize(("max_bytes", "expected"), [(2, "a"), (5, "a中b")])
def test_read_text_file_saves_full_artifact_only_for_truncated_preview(
    tmp_path, max_bytes, expected,
):
    workspace_root = tmp_path / "workspace"
    artifact_root = tmp_path / "artifacts"
    workspace_root.mkdir()
    artifact_root.mkdir()
    content = "a中b".encode("utf-8")
    (workspace_root / "notes.txt").write_bytes(content)
    artifact_id = uuid4()

    preview, artifact = read_text_file_with_artifact(
        workspace_root, "notes.txt", max_bytes=max_bytes,
        artifact_root=artifact_root, artifact_id=artifact_id, chunk_size=1,
    )

    assert preview == expected
    if max_bytes == len(content):
        assert artifact is None
        assert list(artifact_root.iterdir()) == []
    else:
        path, digest = artifact
        assert path == artifact_root / f"{artifact_id}.bin"
        assert path.read_bytes() == content
        assert digest == sha256(content).hexdigest()


def test_a_line_range_reads_the_requested_window_and_reports_coverage(tmp_path):
    from devflow_ai.harness import read_text_file_range

    body = "".join(f"line {number}\n" for number in range(1, 21))
    (tmp_path / "core.py").write_bytes(body.encode("utf-8"))

    middle = read_text_file_range(tmp_path, "core.py", start_line=10, line_limit=3, max_bytes=4096)
    assert (middle.start_line, middle.end_line, middle.total_lines) == (10, 12, 20)
    assert middle.text == "line 10\nline 11\nline 12\n"
    assert middle.has_more is True and middle.byte_truncated is False

    tail = read_text_file_range(tmp_path, "core.py", start_line=18, line_limit=5, max_bytes=4096)
    assert (tail.start_line, tail.end_line, tail.total_lines) == (18, 20, 20)
    # Reaching the end does not make it the whole file: it started at line 18.
    assert tail.has_more is False

    whole = read_text_file_range(tmp_path, "core.py", start_line=1, line_limit=20, max_bytes=4096)
    assert whole.has_more is False and whole.text == body
    assert (whole.end_line, whole.total_lines, whole.partial_line) == (20, 20, False)

    beyond = read_text_file_range(tmp_path, "core.py", start_line=50, line_limit=3, max_bytes=4096)
    assert (beyond.text, beyond.end_line, beyond.has_more) == ("", 0, False)


def test_a_line_range_respects_the_byte_limit_and_the_workspace_boundary(tmp_path):
    from devflow_ai.harness import read_text_file_range

    (tmp_path / "core.py").write_bytes(b"aaaaaaaaaa\nbbbbbbbbbb\ncccccccccc\n")

    capped = read_text_file_range(tmp_path, "core.py", start_line=1, line_limit=3, max_bytes=12)
    # The second line does not fit: a bounded prefix is returned and marked partial, rather
    # than being dropped and leaving the caller with less than it asked for.
    assert capped.byte_truncated is True and capped.has_more is True
    assert capped.text == "aaaaaaaaaa\nb"
    assert (capped.end_line, capped.partial_line) == (2, True)

    with pytest.raises(WorkspacePathError):
        read_text_file_range(tmp_path, "../outside.py", start_line=1, line_limit=1, max_bytes=64)
    with pytest.raises(ValueError):
        read_text_file_range(tmp_path, "core.py", start_line=0, line_limit=1, max_bytes=64)
    with pytest.raises(ValueError):
        read_text_file_range(tmp_path, "core.py", start_line=1, line_limit=0, max_bytes=64)


def test_a_single_line_longer_than_the_byte_budget_returns_a_bounded_prefix(tmp_path):
    """A huge line must not produce an empty preview, and must not be buffered whole."""
    from devflow_ai.harness import read_text_file_range

    huge = "x" * 200_000
    (tmp_path / "minified.js").write_bytes(f"short\n{huge}\n".encode("utf-8"))

    result = read_text_file_range(tmp_path, "minified.js", start_line=2, line_limit=1, max_bytes=1024)

    assert len(result.text) == 1024
    assert (result.start_line, result.end_line, result.total_lines) == (2, 2, 2)
    assert result.byte_truncated is True and result.partial_line is True
    assert result.has_more is True


def test_partial_only_line_has_unread_content(tmp_path):
    from devflow_ai.harness import read_text_file_range

    (tmp_path / "single.txt").write_bytes(b"abcdefghij")
    result = read_text_file_range(tmp_path, "single.txt", start_line=1, line_limit=1, max_bytes=4)

    assert result.text == "abcd"
    assert result.byte_truncated and result.partial_line and result.has_more


def test_a_range_that_ends_without_a_newline_still_returns_the_last_line(tmp_path):
    from devflow_ai.harness import read_text_file_range

    (tmp_path / "tail.txt").write_bytes(b"one\ntwo\nthree")

    result = read_text_file_range(tmp_path, "tail.txt", start_line=3, line_limit=1, max_bytes=64)

    assert result.text == "three"
    assert (result.end_line, result.total_lines, result.has_more) == (3, 3, False)


def test_a_range_that_fits_exactly_is_not_marked_truncated(tmp_path):
    from devflow_ai.harness import read_text_file_range

    (tmp_path / "exact.txt").write_bytes(b"12345\n")

    result = read_text_file_range(tmp_path, "exact.txt", start_line=1, line_limit=1, max_bytes=6)

    assert result.text == "12345\n"
    assert result.byte_truncated is False and result.partial_line is False
