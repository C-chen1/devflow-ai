from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from devflow_ai.task_pack import load_manifest, to_verification_config
from devflow_ai.verification_config import VerificationConfig
from devflow_ai.worker import RunDirectories, Worker

PACK = Path(__file__).resolve().parents[1] / "benchmarks" / "click_3019_public"


def _directories(tmp_path):
    workspaces = tmp_path / "workspaces"
    artifacts = tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    return RunDirectories(workspaces / "run", artifacts / "run")


def _config(public_tests_root):
    return VerificationConfig(
        task_id=uuid4(), base_commit="a" * 40, command_name="pytest",
        command=("python", "-B", "-m", "pytest", "-q", "scenarios.py"),
        target_tests=("scenarios.py::test_scenario",), external_connections="unknown",
        pythonpath_relative="src", public_tests_root=str(public_tests_root))


def _pack_with_one_test(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "scenarios.py").write_text(
        "def test_scenario():\n    assert True\n", encoding="utf-8")
    return pack


def test_the_public_test_command_points_at_a_run_scoped_copy(tmp_path):
    directories = _directories(tmp_path)
    pack = _pack_with_one_test(tmp_path)
    config = _config(pack)

    command = Worker(None, "checks")._public_check_command(config, directories)

    copied_root = directories.artifact_root / "public_tests"
    copy_path = copied_root / "scenarios.py"
    assert copy_path.read_text(encoding="utf-8").startswith("def test_scenario")
    # The checkout stays untouched, so the baseline workspace integrity check still passes.
    assert not any(directories.workspace_root.glob("*.py"))
    assert str(copy_path) in command
    assert "scenarios.py" not in command
    assert f"--rootdir={copied_root}" in command
    assert f"--confcutdir={copied_root}" in command
    # Pytest still runs the same way; only the test location and rootdir change.
    assert command[:4] == ("python", "-B", "-m", "pytest")

    again = Worker(None, "checks")._public_check_command(config, directories)
    assert again == command


def test_without_a_public_tests_root_the_command_is_unchanged(tmp_path):
    directories = _directories(tmp_path)
    config = VerificationConfig(
        task_id=uuid4(), base_commit="a" * 40, command_name="pytest",
        command=("python", "-m", "pytest", "tests/test_x.py"),
        target_tests=("tests/test_x.py::test_x",), external_connections="unknown")

    assert Worker(None, "checks")._public_check_command(config, directories) == config.command


def test_an_unusable_public_tests_root_is_rejected(tmp_path):
    directories = _directories(tmp_path)
    missing = _config(tmp_path / "absent")
    empty_source = tmp_path / "empty"
    empty_source.mkdir()
    no_tests = _config(empty_source)

    with pytest.raises(ValueError, match="unavailable"):
        Worker(None, "checks")._public_check_command(missing, directories)
    with pytest.raises(ValueError, match="no Python test files"):
        Worker(None, "checks")._public_check_command(no_tests, directories)


def test_the_click_pack_derives_a_config_that_carries_its_public_tests():
    manifest = load_manifest(PACK)

    config = to_verification_config(manifest, task_id=uuid4(), public_tests_root=PACK)

    assert config.public_tests_root == str(PACK)
    assert config.pythonpath_relative == "src"
    assert "scenarios.py" in config.command
    # The declared targets use the JUnit naming the comparison matches on, not the
    # pytest node id (`scenarios.py::test_x`) the report never records.
    assert manifest.public_checks.target_tests[0].startswith("scenarios::")
    assert ".py::" not in manifest.public_checks.target_tests[0]


def test_the_trusted_interpreter_replaces_the_pack_placeholder():
    import sys

    manifest = load_manifest(PACK)

    config = to_verification_config(
        manifest, task_id=uuid4(), public_tests_root=PACK, python_executable=sys.executable)

    assert config.python_executable == sys.executable
    assert config.command[0] == sys.executable
    # Only the interpreter changes; the rest of the registered command is untouched.
    assert config.command[1:] == manifest.public_checks.command[1:]


def test_a_relative_interpreter_is_refused():
    import sys

    manifest = load_manifest(PACK)

    with pytest.raises(ValidationError, match="absolute path"):
        to_verification_config(
            manifest, task_id=uuid4(), public_tests_root=PACK, python_executable="python")

    # The absolute one is accepted, so the check is about the value, not the field.
    assert to_verification_config(
        manifest, task_id=uuid4(), python_executable=sys.executable).python_executable


def test_the_assembled_command_differs_only_in_the_test_location(tmp_path):
    """Everything the command evidence covers — interpreter and options — stays identical."""
    directories = _directories(tmp_path)
    pack = _pack_with_one_test(tmp_path)
    config = _config(pack)

    command = Worker(None, "checks")._public_check_command(config, directories)

    copied = str(directories.artifact_root / "public_tests" / "scenarios.py")
    original = list(config.command)
    assembled = list(command)
    assert assembled[0] == original[0]                       # same interpreter
    assert original[-1] == "scenarios.py" and assembled[-1].startswith("--confcutdir=")
    test_path_index = assembled.index(copied)
    assert assembled[:test_path_index] == original[:-1]      # same options, same order
    assert assembled[test_path_index + 1:] == [
        f"--rootdir={directories.artifact_root / 'public_tests'}",
        f"--confcutdir={directories.artifact_root / 'public_tests'}",
    ]


def test_only_the_python_test_modules_are_copied_from_a_pack(tmp_path):
    directories = _directories(tmp_path)
    manifest = load_manifest(PACK)
    config = to_verification_config(manifest, task_id=uuid4(), public_tests_root=PACK)

    command = Worker(None, "checks")._public_check_command(config, directories)

    copied = sorted(path.name for path
                    in (directories.artifact_root / "public_tests").iterdir())
    assert "scenarios.py" in copied
    assert all(name.endswith(".py") for name in copied)
    assert "task.json" not in copied
    assert str(directories.artifact_root / "public_tests" / "scenarios.py") in command
