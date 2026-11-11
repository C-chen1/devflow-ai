import subprocess

import pytest

import devflow_ai.harness as harness


def test_repository_head_observation_reads_real_local_commit(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-q", "--allow-empty", "-m", "test"], check=True)
    expected = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                              capture_output=True, check=True, text=True).stdout.strip()
    result = harness.observe_repository_head(str(tmp_path), timeout_seconds=10)
    assert result.status == "observed"
    assert result.base_commit == expected


def test_unavailable_repository_does_not_become_a_rejection(tmp_path):
    result = harness.observe_repository_head(str(tmp_path / "missing"), timeout_seconds=10)
    assert (result.status, result.base_commit) == ("unavailable", None)


@pytest.mark.parametrize("content", [b"", b"a" * 40 + b"\tOTHER\n", b"a" * 39 + b"\tHEAD\n",
                                      b"a" * 40 + b"\tHEAD\nextra\n"])
def test_invalid_head_output_never_selects_a_commit(monkeypatch, content):
    preview = harness.GitOutputPreview(content, b"", False, False)
    monkeypatch.setattr(harness, "_run_git", lambda *a, **k: harness.GitCommandResult([], 0, preview))
    result = harness.observe_repository_head("trusted-repo", timeout_seconds=5)
    assert (result.status, result.base_commit) == ("invalid_output", None)


def test_timeout_is_not_a_rejection(monkeypatch):
    def timeout(*args, **kwargs):
        raise harness.GitCommandTimedOut(subprocess.TimeoutExpired(["git"], 5),
                                         harness.GitOutputPreview(b"", b"", False, False))
    monkeypatch.setattr(harness, "_run_git", timeout)
    result = harness.observe_repository_head("trusted-repo", timeout_seconds=5)
    assert (result.status, result.base_commit) == ("timeout", None)
