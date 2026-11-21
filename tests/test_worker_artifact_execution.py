"""构造验证：指定副本里的 Worker 真的走过 ``execute_run`` 的执行边界。

The question: when a trusted operator starts a Python process from an old copy of ``devflow_ai``
and that process runs ``Worker.execute_run`` for a Run pinned to that copy's digest, does the
execution boundary accept it, initialise the workspace, and reach the stage callback — and does
it refuse *before* any of that when the bytes or the digest do not match?

What is real here: a separate OS process, an isolated test database schema, a real Git clone and
checkout into a temporary workspace, and the Run row's recorded digest. What is constructed: the
Task, the Run, the package copies (the live package plus one marker function), and the stage
callback — the run stops at a stub instead of driving the five agents. No model, evaluation or
RSI loop is involved, and a subprocess is a process boundary, not a sandbox.

Ordering matters and is asserted, not assumed: the child must import the package before it can
call ``Worker.execute_run``, so "refused before the target stage" means refused before the
workspace is created and before the stage callback — not before import. The negative cases skip
the child's own pre-check on purpose, so the refusal that stops them is ``execute_run``'s version
gate rather than a check the test wrote around it.
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
from devflow_ai.enums import TaskStatus
from devflow_ai.execution_binding import package_modules, running_package_root
from devflow_ai.execution_manifest import build_manifest, manifest_sha256
from devflow_ai.models import Run, Task, ToolCall
from devflow_ai.services import AdmissionRecord, TaskService

# Runs in the child process, from the artifact it was pointed at. It refuses when the package did
# not come from the requested directory, verifies the artifact against the digest it was given,
# and only then constructs the artifact's own Worker and calls ``execute_run``.
BOOTSTRAP = r'''
import json
import os
import sys
from pathlib import Path
from uuid import UUID

OUT = os.environ["DEVFLOW_PROBE_OUT"]
STAGE = os.environ["DEVFLOW_STAGE_OUT"]
ROOT = os.path.realpath(os.environ["DEVFLOW_ARTIFACT_ROOT"])
# Editable installs may add the live source directory through a site-packages .pth file.
LIVE_ROOT = os.path.normcase(os.path.realpath(os.environ["DEVFLOW_LIVE_SOURCE"]))
sys.path[:] = [entry for entry in sys.path
               if not entry or os.path.normcase(os.path.realpath(entry)) != LIVE_ROOT]


def emit(payload, code):
    with open(OUT, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
    print(json.dumps(payload, sort_keys=True))
    raise SystemExit(code)


try:
    import devflow_ai
    from devflow_ai.execution_binding import loaded_package_modules, verify_optional_binding
except Exception as error:
    emit({"outcome": "import_failed", "error_type": type(error).__name__,
          "detail": str(error)[:200],
          "sys_path": [entry for entry in sys.path if entry]}, 3)

package_file = os.path.realpath(devflow_ai.__file__)
if package_file != os.path.join(ROOT, "devflow_ai", "__init__.py"):
    emit({"outcome": "wrong_artifact", "package_file": package_file, "root": ROOT}, 4)

# The positive path verifies the copy it is about to run. The negative cases skip this
# pre-check on purpose, so that ``execute_run``'s own version gate is what refuses.
verified = None
if os.environ.get("DEVFLOW_SKIP_PRECHECK") != "1":
    try:
        verified = verify_optional_binding(
            manifest_path=os.environ["DEVFLOW_ARTIFACT_MANIFEST"],
            expected_sha256=os.environ["DEVFLOW_ARTIFACT_SHA256"],
            require_complete=True)
    except Exception as error:
        emit({"outcome": "refused", "error_type": type(error).__name__,
              "detail": str(error)[:300]}, 2)

# The Worker that runs is the one from this copy, not from anywhere else on the path.
from devflow_ai.config import HeartbeatConfig
from devflow_ai.database import create_db_engine, create_session_factory
from devflow_ai import worker as worker_module

engine = create_db_engine().execution_options(
    schema_translate_map={None: os.environ["DEVFLOW_CHILD_SCHEMA"]})
factory = create_session_factory(engine)


def process_run(directories, actual_commit, stop_event):
    """The controlled stage: only reachable after the boundary accepted this version."""
    payload = {
        "workspace": str(directories.workspace_root),
        "artifacts": str(directories.artifact_root),
        "actual_commit": actual_commit,
        "files": sorted(path.name for path in directories.workspace_root.iterdir()),
        "stop_requested": stop_event.is_set(),
    }
    with open(STAGE, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
    return {"outcome": "stub_stage", **payload}


try:
    result = worker_module.Worker(factory, os.environ["DEVFLOW_WORKER_ID"]).execute_run(
        UUID(os.environ["DEVFLOW_RUN_ID"]),
        workspaces_root=Path(os.environ["DEVFLOW_WORKSPACES"]),
        artifacts_root=Path(os.environ["DEVFLOW_ARTIFACTS"]),
        heartbeat_config=HeartbeatConfig(statement_timeout_seconds=10,
                                         shutdown_timeout_seconds=5),
        process_run=process_run,
        execution_manifest=Path(os.environ["DEVFLOW_ARTIFACT_MANIFEST"]),
        execution_sha256=os.environ["DEVFLOW_ARTIFACT_SHA256"],
    )
except Exception as error:
    # Report the type and, for the version gate, its stated reason; nothing else.
    detail = str(error)[:300] if type(error).__name__ in (
        "RunVersionConflictError", "ExecutionBindingError") else ""
    emit({"outcome": "boundary_refused", "error_type": type(error).__name__,
          "detail": detail, "stage_ran": os.path.exists(STAGE)}, 5)
finally:
    engine.dispose()

inside, outside = loaded_package_modules(ROOT)
emit({"outcome": "executed", "package_file": package_file,
      "worker_file": os.path.realpath(worker_module.__file__),
      "verified": verified, "result": result, "stage_ran": os.path.exists(STAGE),
      "loaded_modules": inside, "outside": outside}, 0)
'''

_INHERITED = ("PATH", "SYSTEMROOT", "SystemRoot", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP",
              "HOME", "USERPROFILE", "LANG", "LC_ALL")
_DB = ("DEVFLOW_DB_NAME", "DEVFLOW_DB_USER", "DEVFLOW_DB_PASSWORD", "DEVFLOW_DB_HOST",
       "DEVFLOW_DB_PORT")


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


def _source_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "README.md").write_text("constructed checkout\n", encoding="utf-8")
    _git(repo, "init", "--quiet")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=Artifact Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "baseline")
    return repo, _git(repo, "rev-parse", "HEAD")


def _task(factory, repo: Path, base_commit: str):
    with factory() as session:
        task, _ = TaskService(session).create_task(
            title="Artifact worker task", description="Constructed fixture", repo=str(repo),
            idempotency_key=uuid4())
        task_id = task.id
    with factory() as session:
        TaskService(session).apply_admission_decision(
            task_id, TaskStatus.ELIGIBLE, base_commit=base_commit,
            admission_record=AdmissionRecord(reason="fixture", evidence=["fixture"]))
    return task_id


def _claim(factory, task_id, digest, worker_id):
    with factory() as session:
        run, _ = RunCoordinator(session).start_initial_run(
            task_id, worker_id, f"key-{uuid4()}", execution_sha256=digest)
        return run.id


def _schema(factory) -> str:
    engine = factory.kw["bind"]
    return engine.get_execution_options()["schema_translate_map"][None]


def _child_env(*, artifact_root, manifest, digest, out, stage, run_id, worker_id,
               workspaces, artifacts, schema) -> dict:
    env = {name: os.environ[name] for name in _INHERITED if name in os.environ}
    env.update({name: os.environ[name] for name in _DB if name in os.environ})
    env.update(
        PYTHONPATH=str(artifact_root),
        PYTHONDONTWRITEBYTECODE="1",
        DEVFLOW_LIVE_SOURCE=str(running_package_root().parent.resolve()),
        DEVFLOW_ARTIFACT_ROOT=str(artifact_root),
        DEVFLOW_ARTIFACT_MANIFEST=str(manifest),
        DEVFLOW_ARTIFACT_SHA256=str(digest),
        DEVFLOW_PROBE_OUT=str(out),
        DEVFLOW_STAGE_OUT=str(stage),
        DEVFLOW_RUN_ID=str(run_id),
        DEVFLOW_WORKER_ID=str(worker_id),
        DEVFLOW_WORKSPACES=str(workspaces),
        DEVFLOW_ARTIFACTS=str(artifacts),
        DEVFLOW_CHILD_SCHEMA=str(schema),
    )
    return env


def _execute(factory, artifact: Artifact, *, run_id, worker_id, tmp_path,
             digest: str | None = None, manifest: Path | None = None,
             root: Path | None = None, skip_precheck: bool = False, timeout: float = 120):
    """Start one child that runs the artifact's Worker.execute_run for this Run."""
    workspaces = tmp_path / "workspaces"
    artifacts = tmp_path / "artifacts"
    for directory in (workspaces, artifacts):
        directory.mkdir(exist_ok=True)
    out = tmp_path / f"probe-{uuid4().hex}.json"
    stage = tmp_path / f"stage-{uuid4().hex}.json"
    env = _child_env(
        artifact_root=root if root is not None else artifact.root,
        manifest=manifest or artifact.manifest,
        digest=digest or artifact.digest,
        out=out, stage=stage, run_id=run_id, worker_id=worker_id,
        workspaces=workspaces, artifacts=artifacts, schema=_schema(factory))
    if skip_precheck:
        env["DEVFLOW_SKIP_PRECHECK"] = "1"
    workdir = tmp_path / f"cwd-{uuid4().hex}"
    workdir.mkdir()
    result = subprocess.run([sys.executable, "-c", BOOTSTRAP], env=env, cwd=str(workdir),
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout)
    payload = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else None
    return result, payload, stage, workspaces / str(run_id)


def _tool_calls(factory, run_id):
    with factory() as session:
        return session.scalars(select(ToolCall).where(ToolCall.run_id == run_id)).all()


def _run(factory, run_id):
    with factory() as session:
        return session.get(Run, run_id)


def test_the_child_executes_the_run_through_the_artifact_worker_boundary(
        worker_session_factory, tmp_path):
    factory = worker_session_factory
    v0 = _artifact_copy(tmp_path, "v0", "v0")
    repo, base_commit = _source_repo(tmp_path)
    task_id = _task(factory, repo, base_commit)
    worker_id = "artifact-worker-v0"
    run_id = _claim(factory, task_id, v0.digest, worker_id)

    result, payload, stage, run_workspace = _execute(
        factory, v0, run_id=run_id, worker_id=worker_id, tmp_path=tmp_path)

    assert result.returncode == 0, (result.returncode, payload and payload.get("outcome"))
    assert payload["outcome"] == "executed"
    # The Worker that ran is the one from the requested copy.
    assert payload["package_file"] == str((v0.package / "__init__.py").resolve())
    assert payload["worker_file"] == str((v0.package / "worker.py").resolve())
    assert payload["outside"] == []
    # The digest the boundary verified is the one the Run recorded and this copy's manifest.
    assert payload["verified"] == v0.digest
    assert _run(factory, run_id).execution_sha256 == v0.digest
    # The workspace was really initialised and the stage callback really ran.
    assert payload["stage_ran"] is True and stage.is_file()
    stage_report = json.loads(stage.read_text(encoding="utf-8"))
    assert stage_report["actual_commit"] == base_commit
    assert "README.md" in stage_report["files"]
    assert Path(stage_report["workspace"]).resolve() == run_workspace.resolve()
    names = {call.tool_name for call in _tool_calls(factory, run_id)}
    assert {"git_clone", "git_checkout"} <= names
    # Nothing rewrote the constructed history.
    run = _run(factory, run_id)
    assert run.attempt_number == 1 and run.task_id == task_id
    with factory() as session:
        assert len(session.scalars(select(Run).where(Run.task_id == task_id)).all()) == 1
        assert session.get(Task, task_id).base_commit == base_commit


def test_a_mismatched_digest_is_refused_before_the_workspace_exists(
        worker_session_factory, tmp_path):
    factory = worker_session_factory
    v0 = _artifact_copy(tmp_path, "v0", "v0")
    repo, base_commit = _source_repo(tmp_path)
    task_id = _task(factory, repo, base_commit)
    worker_id = "artifact-worker-mismatch"
    run_id = _claim(factory, task_id, v0.digest, worker_id)

    # The pre-check is skipped: the refusal has to come from ``execute_run`` itself.
    result, payload, stage, run_workspace = _execute(
        factory, v0, run_id=run_id, worker_id=worker_id, tmp_path=tmp_path,
        digest="f" * 64, skip_precheck=True)

    assert result.returncode == 5 and payload["outcome"] == "boundary_refused"
    assert payload["error_type"] == "ExecutionBindingError"
    assert "pinned digest" in payload["detail"]
    assert payload["stage_ran"] is False
    assert not stage.exists() and not run_workspace.exists()
    assert _tool_calls(factory, run_id) == []
    assert _run(factory, run_id).execution_sha256 == v0.digest


def test_a_changed_or_missing_artifact_is_refused_before_the_workspace_exists(
        worker_session_factory, tmp_path):
    factory = worker_session_factory
    v0 = _artifact_copy(tmp_path, "v0", "v0")
    repo, base_commit = _source_repo(tmp_path)
    task_id = _task(factory, repo, base_commit)
    worker_id = "artifact-worker-changed"
    run_id = _claim(factory, task_id, v0.digest, worker_id)

    # One changed byte under a listed file.
    module = v0.package / "worker.py"
    original = module.read_bytes()
    module.write_bytes(original + b"\n# changed\n")
    try:
        result, payload, stage, run_workspace = _execute(
            factory, v0, run_id=run_id, worker_id=worker_id, tmp_path=tmp_path,
            skip_precheck=True)
    finally:
        module.write_bytes(original)
    assert result.returncode == 5 and payload["outcome"] == "boundary_refused"
    assert payload["error_type"] == "ExecutionBindingError"
    assert "digest does not match" in payload["detail"]
    assert payload["stage_ran"] is False
    assert not stage.exists() and not run_workspace.exists()
    assert _tool_calls(factory, run_id) == []

    # A manifest that is gone: the boundary cannot verify, so it does not run.
    missing_manifest = tmp_path / "absent-manifest.json"
    result, payload, stage, run_workspace = _execute(
        factory, v0, run_id=run_id, worker_id=worker_id, tmp_path=tmp_path,
        manifest=missing_manifest, skip_precheck=True)
    assert result.returncode == 5 and payload["outcome"] == "boundary_refused"
    assert payload["error_type"] == "ExecutionBindingError"
    assert payload["stage_ran"] is False
    assert not stage.exists() and not run_workspace.exists()
    assert _run(factory, run_id).execution_sha256 == v0.digest


def test_a_run_pinned_to_another_version_cannot_be_executed(
        worker_session_factory, tmp_path):
    factory = worker_session_factory
    v0 = _artifact_copy(tmp_path, "v0", "v0")
    v1 = _artifact_copy(tmp_path, "v1", "v1")
    repo, base_commit = _source_repo(tmp_path)
    task_id = _task(factory, repo, base_commit)
    worker_id = "artifact-worker-pinned"
    run_id = _claim(factory, task_id, v0.digest, worker_id)

    # V1 verifies against its own manifest, but the Run is pinned to V0: the boundary refuses.
    result, payload, stage, run_workspace = _execute(
        factory, v1, run_id=run_id, worker_id=worker_id, tmp_path=tmp_path)

    assert result.returncode == 5 and payload["outcome"] == "boundary_refused"
    assert payload["error_type"] == "RunVersionConflictError"
    assert payload["stage_ran"] is False
    assert not stage.exists() and not run_workspace.exists()
    assert _tool_calls(factory, run_id) == []
    assert _run(factory, run_id).execution_sha256 == v0.digest


def test_an_unavailable_artifact_never_falls_back_to_the_live_tree(
        worker_session_factory, tmp_path):
    factory = worker_session_factory
    v0 = _artifact_copy(tmp_path, "v0", "v0")
    repo, base_commit = _source_repo(tmp_path)
    task_id = _task(factory, repo, base_commit)
    worker_id = "artifact-worker-absent"
    run_id = _claim(factory, task_id, v0.digest, worker_id)

    result, payload, stage, run_workspace = _execute(
        factory, v0, run_id=run_id, worker_id=worker_id, tmp_path=tmp_path,
        root=tmp_path / "absent")

    assert payload["outcome"] in {"import_failed", "wrong_artifact"}
    assert result.returncode == {"import_failed": 3, "wrong_artifact": 4}[payload["outcome"]]
    assert not stage.exists() and not run_workspace.exists()
    assert _tool_calls(factory, run_id) == []
    assert _run(factory, run_id).execution_sha256 == v0.digest
    if payload["outcome"] == "wrong_artifact":
        # Something else was importable (an installed copy, for instance) and was refused.
        assert payload["package_file"] != str((v0.package / "__init__.py").resolve())
