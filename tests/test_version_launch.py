"""构造验证：控制端显式启动指定副本里的 Worker 入口。

These tests drive ``version_launch.launch_worker_run``: the control plane verifies a named local
copy against an independently supplied pin and then starts **that copy's** ``worker_run`` with an
argument array. What is real: the verification, the OS process, the isolated test schema, the
Task and Run rows, the workspace checkout and the stage implementations. What is constructed:
the Task, the package copies (the live package plus a marker function), and — in the staged test
only — the model gateways.

Two levels of evidence live here, and they are not the same thing:

* most tests have **no** model configuration, so the child stops at "model gateways are not
  configured" before claiming. That still proves it passed its own version check, pack, Task and
  Policy checks against the pin, because a child on any other copy is refused earlier;
* the launcher now starts the child as ``<python> -P -B -c <bootstrap> -- <deploy dir> …`` so the
  deploy directory is inserted into ``sys.path`` *after* interpreter startup. A ``sitecustomize``
  sitting next to the package therefore cannot run before the Worker's version check, which is
  asserted with a real child process and a marker file;
* the stage evidence is **separate and weaker on purpose**: stubbing the model gateways needs a
  startup hook, and the hardened launcher removes exactly that hook. The staged test therefore
  uses the *previous* launch shape (``PYTHONPATH`` naming the deploy directory) directly, not
  ``launch_worker_run``. It shows the artifact's entry can claim a Run and drive real stages with
  stub models; it is not evidence about the hardened launcher's stage path.
"""
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from devflow_ai.coordinator import RunCoordinator
from devflow_ai.enums import AgentRole, TaskStatus
from devflow_ai.execution_binding import package_modules, running_package_root
from devflow_ai.execution_manifest import build_manifest, manifest_sha256
from devflow_ai.models import AgentExecution, Run, Task, ToolCall
from devflow_ai.policy import build_default_policy
from devflow_ai.services import AdmissionRecord, TaskService
from devflow_ai import version_launch

CLAIM_KEY = "worker:{task_id}:1"


@dataclass(frozen=True)
class Artifact:
    root: Path
    package: Path
    manifest: Path
    digest: str


def _artifact_copy(tmp_path: Path, name: str, marker: str) -> Artifact:
    root = tmp_path / name
    package = root / "devflow_ai"
    shutil.copytree(running_package_root(), package,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    init = package / "__init__.py"
    init.write_text(
        init.read_text(encoding="utf-8")
        + f'\n\nARTIFACT_VERSION = "{marker}"\n\n\ndef artifact_probe():\n'
          '    """The observable difference between two local versions."""\n'
          "    return ARTIFACT_VERSION\n",
        encoding="utf-8")
    manifest = build_manifest(package, package_modules(package))
    manifest_path = root / "manifest.json"
    manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")
    return Artifact(root=root, package=package, manifest=manifest_path,
                    digest=manifest_sha256(manifest))


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8", timeout=60).stdout.strip()


def _repo(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    repo = tmp_path / "source-repo"
    repo.mkdir()
    (repo / "README.md").write_text("Repository facts\n", encoding="utf-8")
    _git(repo, "init", "--quiet")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=Launch Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "initial")
    return repo, _git(repo, "rev-parse", "HEAD")


def _pack(tmp_path: Path, base_commit: str) -> Path:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir(exist_ok=True)
    (pack_dir / "scenarios.py").write_text(
        "def test_placeholder():\n    assert True\n", encoding="utf-8")
    manifest = {
        "pack_id": "version-launch-pack", "schema_version": 1, "title": "Version launch pack",
        "source": {"issue_url": "https://example.invalid/issues/1",
                   "repository_url": "https://example.invalid/repo",
                   "license": "MIT", "issue_reference": "demo#1"},
        "base_commit": base_commit,
        "environment": {"python_requires": ">=3.11", "dependencies": ["pytest"],
                        "platform_notes": "CI"},
        "issue": {"sanitized_problem": "A sanitized problem.",
                  "observable_error": "An error.",
                  "expected_behavior": "The expected behaviour."},
        "scope": {"allowed_paths": ["src/"]},
        "public_checks": {"command": ["python", "-m", "pytest"], "command_name": "pytest",
                          "target_tests": ["scenarios::test_placeholder"]},
        "baseline": {"status": "verified_here"},
        "external_connections": "none",
        "independent_suite": None,
        "unverified": [
            {"field": "policy", "reason": "Not pinned for this fixture."},
            {"field": "independent_suite", "reason": "Held by the trusted side."},
        ],
    }
    (pack_dir / "task.json").write_text(json.dumps(manifest), encoding="utf-8")
    return pack_dir


def _task(factory, repo: Path, base_commit: str):
    policy = build_default_policy()
    policy.policy_version = "version-launch-test-v1"
    policy.permissions.allowed_tools_by_role[AgentRole.EXPLORER] = ["read_file"]
    with factory() as session:
        task, _ = TaskService(session, policy_factory=lambda: policy).create_task(
            title="Launch one version", description="Constructed fixture", repo=str(repo),
            idempotency_key=uuid4())
        task_id = task.id
    with factory() as session:
        TaskService(session).apply_admission_decision(
            task_id, TaskStatus.ELIGIBLE, base_commit=base_commit,
            admission_record=AdmissionRecord(reason="fixture", evidence=["fixture"]))
    with factory() as session:
        snapshot = dict(session.scalar(select(Task.policy_snapshot).where(Task.id == task_id)))
    policy_path = repo.parent / "policy.json"
    policy_path.write_text(json.dumps(snapshot), encoding="utf-8")
    return task_id, policy_path


def _claim(factory, task_id, digest, worker_id="launch-worker"):
    with factory() as session:
        run, _ = RunCoordinator(session).start_initial_run(
            task_id, worker_id, CLAIM_KEY.format(task_id=task_id), execution_sha256=digest)
        return run.id


def _schema(factory) -> str:
    return factory.kw["bind"].get_execution_options()["schema_translate_map"][None]


def _environment(factory) -> dict:
    """The parent's environment plus a libpq option that points the child at this schema."""
    return {**os.environ,
            "PGOPTIONS": f"-c timezone=UTC -c search_path={_schema(factory)},public"}


def _worker_arguments(*, pack_dir, task_id, policy_path, python_executable, tmp_path):
    workspaces = tmp_path / "workspaces"
    artifacts = tmp_path / "artifacts"
    for directory in (workspaces, artifacts):
        directory.mkdir(exist_ok=True)
    return [
        "--pack", str(pack_dir), "--task-id", str(task_id), "--policy", str(policy_path),
        "--operator", "operator-a", "--python-executable", str(python_executable),
        "--workspaces-root", str(workspaces), "--artifacts-root", str(artifacts),
        "--attempt-kind", "rehearsal", "--confirm-run",
    ]


def _launch(factory, artifact: Artifact, *, arguments, run=subprocess.run,
            digest: str | None = None, root: Path | None = None,
            manifest: Path | None = None, python_executable=None):
    return version_launch.launch_worker_run(
        artifact_root=root if root is not None else artifact.package,
        manifest_path=manifest or artifact.manifest,
        execution_sha256=digest or artifact.digest,
        python_executable=python_executable or sys.executable,
        worker_arguments=arguments, passthrough=("PGOPTIONS",),
        environment=_environment(factory), timeout_seconds=120, run=run)


def _runs(factory, task_id):
    with factory() as session:
        return session.scalars(select(Run).where(Run.task_id == task_id)).all()


def test_a_verified_copy_is_started_and_its_own_entry_accepts_the_pin(
        worker_session_factory, tmp_path, monkeypatch):
    factory = worker_session_factory
    v0 = _artifact_copy(tmp_path, "v0", "v0")
    repo, base_commit = _repo(tmp_path, monkeypatch)
    pack_dir = _pack(tmp_path, base_commit)
    task_id, policy_path = _task(factory, repo, base_commit)
    run_id = _claim(factory, task_id, v0.digest)
    arguments = _worker_arguments(pack_dir=pack_dir, task_id=task_id, policy_path=policy_path,
                                  python_executable=sys.executable, tmp_path=tmp_path)

    result = _launch(factory, v0, arguments=arguments)

    assert result.execution_sha256 == v0.digest
    assert result.verified_files == len(package_modules(v0.package))
    # The child got past its own version check and its own pack/Task/policy checks, recognised
    # the existing Run as this call's own, and stopped where any model call would begin.
    details = [str(event.get("detail", "")) for event in result.events]
    assert result.returncode == 2
    assert any("Model gateways are not configured" in detail for detail in details)
    assert not any("Execution binding refused" in detail for detail in details)
    # No Run was created, none was rewritten, and nothing was reported as a success.
    runs = _runs(factory, task_id)
    assert [run.id for run in runs] == [run_id]
    assert runs[0].execution_sha256 == v0.digest
    assert runs[0].status.value == "RUNNING"
    assert "worker_run_finished" not in [event.get("event") for event in result.events]


def test_a_copy_that_does_not_match_the_pin_or_was_changed_is_never_started(
        worker_session_factory, tmp_path, monkeypatch):
    factory = worker_session_factory
    v0 = _artifact_copy(tmp_path, "v0", "v0")
    repo, base_commit = _repo(tmp_path, monkeypatch)
    pack_dir = _pack(tmp_path, base_commit)
    task_id, policy_path = _task(factory, repo, base_commit)
    _claim(factory, task_id, v0.digest)
    arguments = _worker_arguments(pack_dir=pack_dir, task_id=task_id, policy_path=policy_path,
                                  python_executable=sys.executable, tmp_path=tmp_path)
    started = []

    def refuse_to_run(*args, **kwargs):
        started.append(args)
        raise AssertionError("no process may be started for an unverifiable copy")

    with pytest.raises(version_launch.VersionLaunchError, match="pinned digest"):
        _launch(factory, v0, arguments=arguments, run=refuse_to_run, digest="f" * 64)

    module = v0.package / "worker_run.py"
    original = module.read_bytes()
    module.write_bytes(original + b"\n# changed\n")
    try:
        with pytest.raises(version_launch.VersionLaunchError, match="digest does not match"):
            _launch(factory, v0, arguments=arguments, run=refuse_to_run)
    finally:
        module.write_bytes(original)

    with pytest.raises(version_launch.VersionLaunchError, match="cannot be read|missing"):
        _launch(factory, v0, arguments=arguments, run=refuse_to_run,
                root=tmp_path / "absent" / "devflow_ai")

    assert started == []


def test_an_unusable_interpreter_is_refused_before_launch(
        worker_session_factory, tmp_path, monkeypatch):
    factory = worker_session_factory
    v0 = _artifact_copy(tmp_path, "v0", "v0")
    repo, base_commit = _repo(tmp_path, monkeypatch)
    pack_dir = _pack(tmp_path, base_commit)
    task_id, policy_path = _task(factory, repo, base_commit)
    _claim(factory, task_id, v0.digest)
    arguments = _worker_arguments(pack_dir=pack_dir, task_id=task_id, policy_path=policy_path,
                                  python_executable=sys.executable, tmp_path=tmp_path)
    started = []

    def refuse_to_run(*args, **kwargs):
        started.append(args)
        raise AssertionError("no process may be started without an interpreter")

    with pytest.raises(version_launch.VersionLaunchError, match="interpreter"):
        _launch(factory, v0, arguments=arguments, run=refuse_to_run,
                python_executable=tmp_path / "not-a-python")
    assert started == []


def test_a_run_pinned_to_another_version_is_refused_by_the_entry(
        worker_session_factory, tmp_path, monkeypatch):
    factory = worker_session_factory
    v0 = _artifact_copy(tmp_path, "v0", "v0")
    v1 = _artifact_copy(tmp_path, "v1", "v1")
    repo, base_commit = _repo(tmp_path, monkeypatch)
    pack_dir = _pack(tmp_path, base_commit)
    task_id, policy_path = _task(factory, repo, base_commit)
    run_id = _claim(factory, task_id, v0.digest)
    arguments = _worker_arguments(pack_dir=pack_dir, task_id=task_id, policy_path=policy_path,
                                  python_executable=sys.executable, tmp_path=tmp_path)

    # V1 verifies against its own manifest, but the Run this Task already owns was pinned to V0.
    result = _launch(factory, v1, arguments=arguments)

    assert result.returncode == 2
    details = " ".join(str(event.get("detail", "")) for event in result.events)
    assert "different execution version" in details
    runs = _runs(factory, task_id)
    assert [run.id for run in runs] == [run_id]
    assert runs[0].execution_sha256 == v0.digest


def test_the_environment_given_to_the_child_is_explicit(monkeypatch):
    base = {
        "PATH": "/usr/bin", "SystemRoot": "C:\\Windows", "TEMP": "C:\\Temp",
        "DEVFLOW_DB_NAME": "devflow_test", "DEVFLOW_DB_PASSWORD": "not-printed",
        "PGOPTIONS": "-c search_path=somewhere", "DEEPSEEK_API_KEY": "not-inherited",
        "SOME_OTHER_SETTING": "dropped", "PYTHONPATH": "/somewhere/else",
    }
    env = version_launch.child_environment(Path("/tmp/artifact"), base=base)

    # The artifact directory must not reach the child through the environment at all.
    assert "PYTHONPATH" not in env
    assert env["PYTHONNOUSERSITE"] == "1" and env["PYTHONDONTWRITEBYTECODE"] == "1"
    # Database settings are needed; model credentials are not inherited unless named.
    assert env["DEVFLOW_DB_NAME"] == "devflow_test"
    assert "DEEPSEEK_API_KEY" not in env
    assert "SOME_OTHER_SETTING" not in env and "PGOPTIONS" not in env
    named = version_launch.child_environment(Path("/tmp/artifact"), base=base,
                                             passthrough=("PGOPTIONS",))
    assert named["PGOPTIONS"] == "-c search_path=somewhere"
    assert "DEEPSEEK_API_KEY" not in named


def test_the_cli_reports_a_refusal_and_exits_two(tmp_path, capsys):
    v0 = _artifact_copy(tmp_path, "v0", "v0")

    code = version_launch.main([
        "--artifact-root", str(v0.package), "--manifest", str(v0.manifest),
        "--execution-sha256", "f" * 64, "--python", sys.executable,
        "--", "--pack", str(tmp_path / "pack")])

    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 2 and event["event"] == "version_launch_refused"
    assert "pinned digest" in event["detail"]


def test_the_command_keeps_the_artifact_off_the_startup_import_path(tmp_path):
    v0 = _artifact_copy(tmp_path, "v0", "v0")
    command = version_launch.build_command(
        python_executable=sys.executable, artifact_root=v0.package,
        worker_arguments=["--pack", "pack-dir"], manifest_path=v0.manifest,
        execution_sha256=v0.digest)

    assert command[0] == str(sys.executable)
    assert command[1:4] == ["-P", "-B", "-c"] and command[4] == version_launch.BOOTSTRAP
    # The deploy directory travels as an argument the bootstrap inserts after startup, not
    # through the environment the interpreter reads while starting up.
    assert command[5:] == ["--", str(v0.package.parent.resolve()), "--pack", "pack-dir",
                           "--execution-manifest", str(v0.manifest.resolve()),
                           "--execution-sha256", v0.digest]
    # The entry supplies the version flags itself: a caller cannot smuggle another pair in.
    with pytest.raises(version_launch.VersionLaunchError, match="execution-manifest"):
        version_launch.build_command(
            python_executable=sys.executable, artifact_root=v0.package,
            worker_arguments=["--execution-sha256", "f" * 64],
            manifest_path=v0.manifest, execution_sha256=v0.digest)
    with pytest.raises(version_launch.VersionLaunchError, match="absolute"):
        version_launch.build_command(python_executable="python3", artifact_root=v0.package,
                                     worker_arguments=["--pack"], manifest_path=v0.manifest,
                                     execution_sha256=v0.digest)
    with pytest.raises(version_launch.VersionLaunchError, match="execution-manifest"):
        version_launch.build_command(
            python_executable=sys.executable, artifact_root=v0.package,
            worker_arguments=["--execution-sha256=" + "f" * 64],
            manifest_path=v0.manifest, execution_sha256=v0.digest)


def test_relative_manifest_is_resolved_before_child_cwd_and_timeout_is_opt_in(tmp_path, monkeypatch):
    v0 = _artifact_copy(tmp_path, "v0", "v0")
    monkeypatch.chdir(tmp_path)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 2, "", "")

    version_launch.launch_worker_run(
        artifact_root=v0.package, manifest_path=v0.manifest.relative_to(tmp_path),
        execution_sha256=v0.digest, python_executable=sys.executable,
        worker_arguments=["--help"], run=fake_run)
    command, kwargs = calls[0]
    assert command[command.index("--execution-manifest") + 1] == str(v0.manifest.resolve())
    assert kwargs["cwd"] == str(v0.root.resolve())
    assert kwargs["timeout"] is None


def test_a_directory_that_is_not_the_package_cannot_be_launched(tmp_path):
    v0 = _artifact_copy(tmp_path, "v0", "v0")
    with pytest.raises(version_launch.VersionLaunchError, match="package directory"):
        version_launch.launch_worker_run(
            artifact_root=v0.root, manifest_path=v0.manifest, execution_sha256=v0.digest,
            python_executable=sys.executable, worker_arguments=["--help"])


def test_an_os_launch_error_is_reported_without_exposing_details(tmp_path):
    v0 = _artifact_copy(tmp_path, "v0", "v0")

    def failed_run(*args, **kwargs):
        raise OSError("sensitive detail")

    with pytest.raises(version_launch.VersionLaunchError, match="could not be started") as error:
        version_launch.launch_worker_run(
            artifact_root=v0.package, manifest_path=v0.manifest, execution_sha256=v0.digest,
            python_executable=sys.executable, worker_arguments=["--help"], run=failed_run)
    assert "sensitive detail" not in str(error.value)


def test_launch_collects_events_from_both_streams_without_assuming_time_order(tmp_path):
    v0 = _artifact_copy(tmp_path, "v0", "v0")

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 0, '{"event":"worker_run_finished"}\n',
            '{"event":"worker_run_warning"}\n')

    result = version_launch.launch_worker_run(
        artifact_root=v0.package, manifest_path=v0.manifest, execution_sha256=v0.digest,
        python_executable=sys.executable, worker_arguments=["--help"], run=fake_run)
    assert {event["event"] for event in result.events} == {
        "worker_run_finished", "worker_run_warning"}


# Test-only injection. It lives in the *deploy directory* next to the package (never inside the
# verified package, so the manifest and the copy stay untouched) and is imported by the
# interpreter at startup through ``sitecustomize``. It replaces the module-level
# ``model_gateways_from_env`` that ``worker_run`` binds when it is imported, so the launched
# entry still is ``python -P -B -m devflow_ai.worker_run`` with the launcher's own arguments.
INJECTION = r"""
import atexit
import contextlib
import json
import os
import sys

import devflow_ai
import devflow_ai.model_gateways as model_gateways
from devflow_ai.coordinator import ModelTokenUsage
from devflow_ai.model_gateway import ModelGateway, ModelReply, PlannerModelGateway

STAGE_LOG = os.environ["DEVFLOW_TEST_STAGE_LOG"]
SOURCE_LOG = os.environ["DEVFLOW_TEST_SOURCE_LOG"]
CALLS = []
EXPLORER_CALLS = []
DECISIONS = iter([
    {"outcome": "EXPLORE", "reason": "Need evidence", "exploration_focus": "Inspect the repo"},
    {"outcome": "PLAN", "reason": "Evidence is enough", "next_goal": "Plan the change"},
    {"outcome": "BLOCKED", "reason": "Constructed stage reached", "decision_required": "Stop"},
])


def generate(prompt, limit):
    if "You are DevFlow AI's Supervisor" in prompt:
        role, output = "SUPERVISOR", next(DECISIONS)
    elif "You are DevFlow AI's Explorer" in prompt:
        role = "EXPLORER"
        EXPLORER_CALLS.append(prompt)
        if len(EXPLORER_CALLS) == 1:
            output = {"path": "README.md"}
        else:
            data = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
            output = {"summary": "The README describes the repository.",
                      "read_evidence": {"path": "README.md",
                                        "tool_call_id": data["read"]["tool_call_id"]}}
    elif "You are DevFlow AI's Planner" in prompt:
        role = "PLANNER"
        output = {"outcome": "PLAN_READY", "plan": {
            "approach": "Change nothing yet", "target_paths": ["README.md"],
            "steps": ["Review the repository facts"], "scope_limits": "Only README.md",
            "verification": ["Run the public check"]}}
    else:
        role, output = "OTHER", {"outcome": "BLOCKED", "reason": "stub",
                                 "decision_required": "Stop"}
    CALLS.append(role)
    with open(STAGE_LOG, "a", encoding="utf-8") as handle:
        handle.write(role + "\n")
    return ModelReply(output, ModelTokenUsage(input_tokens=10, output_tokens=5))


class _Gateways:
    def __init__(self, session_factory, worker_id):
        def gateway():
            return ModelGateway(session_factory=session_factory, worker_id=worker_id,
                                estimate_input_tokens=lambda _: 100, generate=generate,
                                max_output_tokens=100)

        self.supervisor = gateway()
        self.explorer = gateway()
        self.coder = gateway()
        self.verifier = gateway()
        self.planner = PlannerModelGateway(session_factory=session_factory,
                                           worker_id=worker_id,
                                           estimate_input_tokens=lambda _: 100,
                                           generate=generate, max_output_tokens=100)


def stub_factory(session_factory, worker_id):
    return contextlib.nullcontext(_Gateways(session_factory, worker_id))


model_gateways.model_gateways_from_env = stub_factory


@atexit.register
def record_sources():
    payload = {
        "package": os.path.realpath(devflow_ai.__file__),
        "worker": getattr(sys.modules.get("devflow_ai.worker"), "__file__", None),
        "main": getattr(sys.modules.get("__main__"), "__file__", None),
        "calls": CALLS,
    }
    with open(SOURCE_LOG, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
"""


def _install_stub_gateways(artifact: Artifact) -> None:
    """Deploy-directory injection: outside the verified package, imported at child startup."""
    (artifact.root / "sitecustomize.py").write_text(INJECTION, encoding="utf-8")


def test_the_entry_from_a_copy_claims_a_run_and_reaches_real_stages_with_stub_gateways(
        worker_session_factory, tmp_path, monkeypatch):
    """Stage evidence through the *previous* launch shape; not the hardened launcher's path.

    Stubbing the gateways needs something that runs before the Worker's own check, and the
    hardened launcher exists precisely to stop that. So this test starts the artifact's entry the
    old way — ``PYTHONPATH`` naming the deploy directory, where the test's ``sitecustomize.py``
    lives — and asserts what the entry itself does: claim a Run, record the pin, check out the
    workspace and drive real stages with stub model replies.
    """
    factory = worker_session_factory
    v0 = _artifact_copy(tmp_path, "v0", "v0")
    _install_stub_gateways(v0)
    repo, base_commit = _repo(tmp_path, monkeypatch)
    pack_dir = _pack(tmp_path, base_commit)
    task_id, policy_path = _task(factory, repo, base_commit)
    stage_log = tmp_path / "stage.log"
    source_log = tmp_path / "source.json"
    arguments = _worker_arguments(pack_dir=pack_dir, task_id=task_id, policy_path=policy_path,
                                  python_executable=sys.executable, tmp_path=tmp_path)

    completed = _run_entry_with_deploy_hook(
        v0, arguments=arguments,
        environment={**_environment(factory), "DEVFLOW_TEST_STAGE_LOG": str(stage_log),
                     "DEVFLOW_TEST_SOURCE_LOG": str(source_log)})
    events = _events(completed.stdout + "\n" + completed.stderr)

    assert completed.returncode == 0, (completed.returncode,
                                       [event.get("event") for event in events])
    finished = [event for event in events if event.get("event") == "worker_run_finished"]
    assert finished and finished[0]["claimed"] is True
    assert finished[0]["reason"] == "supervisor_did_not_route"
    assert finished[0]["stages"] >= 3
    assert finished[0]["execution_sha256"] == v0.digest
    assert finished[0]["run_status"] == "RUNNING"
    # The claimed Run records the digest of the copy that was launched and verified.
    runs = _runs(factory, task_id)
    assert len(runs) == 1 and runs[0].execution_sha256 == v0.digest
    run_id = runs[0].id
    with factory() as session:
        roles = {execution.agent_role for execution in session.scalars(
            select(AgentExecution).where(AgentExecution.run_id == run_id))}
        tool_names = {call.tool_name for call in session.scalars(
            select(ToolCall).where(ToolCall.run_id == run_id))}
    # Real Worker stages ran (their model calls were stubs), including the workspace checkout.
    assert {"supervisor", "explorer", "planner"} <= roles
    assert {"git_clone", "git_checkout"} <= tool_names and "read_file" in tool_names
    # The child recorded, in its own process, which roles it called and what it imported.
    stages = stage_log.read_text(encoding="utf-8").split()
    assert stages[:2] == ["SUPERVISOR", "EXPLORER"] and "PLANNER" in stages
    sources = json.loads(source_log.read_text(encoding="utf-8"))
    for key in ("package", "worker", "main"):
        assert Path(sources[key]).resolve().is_relative_to(v0.package.resolve()), key
    assert set(sources["calls"]) >= {"SUPERVISOR", "EXPLORER", "PLANNER"}
    # No database credential reached the child's output (reported without echoing the value).
    password = os.environ.get("DEVFLOW_DB_PASSWORD")
    leaked = bool(password) and password in (completed.stdout + completed.stderr)
    assert not leaked, "a database credential appeared in the child's output"


def _events(output: str) -> list[dict]:
    events = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and "event" in payload:
            events.append(payload)
    return events


def _run_entry_with_deploy_hook(artifact: Artifact, *, arguments, environment,
                                timeout: float = 180) -> subprocess.CompletedProcess:
    """Start the entry the pre-hardening way: ``PYTHONPATH`` names the deploy directory.

    Only used to reach the stages with stubbed gateways. The launcher no longer produces this
    command line, and nothing here should be read as evidence about the launcher's stage path.
    """
    env = {**environment, "PYTHONPATH": str(artifact.package.parent),
           "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, "-P", "-B", "-m", "devflow_ai.worker_run", *arguments,
         "--execution-manifest", str(artifact.manifest),
         "--execution-sha256", artifact.digest],
        env=env, cwd=str(artifact.package.parent), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout)


def _write_deploy_hook(artifact: Artifact, marker: Path) -> None:
    (artifact.root / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8")


def test_a_sitecustomize_beside_the_deployed_package_never_runs(
        worker_session_factory, tmp_path, monkeypatch):
    factory = worker_session_factory
    v0 = _artifact_copy(tmp_path, "v0", "v0")
    marker = tmp_path / "deploy-hook-ran.txt"
    _write_deploy_hook(v0, marker)
    repo, base_commit = _repo(tmp_path, monkeypatch)
    pack_dir = _pack(tmp_path, base_commit)
    task_id, policy_path = _task(factory, repo, base_commit)
    _claim(factory, task_id, v0.digest)
    arguments = _worker_arguments(pack_dir=pack_dir, task_id=task_id, policy_path=policy_path,
                                  python_executable=sys.executable, tmp_path=tmp_path)

    result = _launch(factory, v0, arguments=arguments)

    assert not marker.exists(), "a deploy-directory startup hook ran in the child"
    # The child really ran the entry and got past its own version check; only the model
    # configuration is missing, which is where a launch without credentials stops.
    details = " ".join(str(event.get("detail", "")) for event in result.events)
    assert result.returncode == 2
    assert "Model gateways are not configured" in details
    assert "Execution binding refused" not in details


def test_the_previous_startup_shape_did_import_the_deploy_hook(tmp_path):
    """Witness for the gap this change closes: with the deploy dir on PYTHONPATH it runs."""
    v0 = _artifact_copy(tmp_path, "v0", "v0")
    marker = tmp_path / "old-shape-hook.txt"
    _write_deploy_hook(v0, marker)
    environment = {name: os.environ[name] for name in ("PATH", "SystemRoot", "SYSTEMROOT")
                   if name in os.environ}

    completed = _run_entry_with_deploy_hook(v0, arguments=["--help"],
                                            environment=environment)

    assert completed.returncode == 0
    assert marker.exists(), "the pre-hardening startup shape imported the deploy-directory hook"
