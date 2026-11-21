"""一份旧工件能不能被"真的启动"：独立子进程从指定目录加载并执行可观察的 DevFlow 代码。

The question this file answers is narrow: if a trusted operator puts an old copy of ``devflow_ai``
in a directory and starts a Python process pointed at it, does that process load **that** copy
(rather than the current working tree), does it verify the copy against the recorded digest
before running the target probe, and does it refuse — instead of silently using whatever
version is installed — when the copy is changed, incomplete or absent?

What it deliberately does not answer: whether the real Worker ever starts such a process for a
Run, whether a candidate is isolated from the control plane, or whether any of this is a security
sandbox. A subprocess is a process boundary, not a trust boundary: the child verifies itself with
the artifact's own code, and the parent verifies the same bytes independently before spawning it.
The child imports artifact code before its self-check. Neither check is independent proof that
the bytes on disk are the bytes in memory.
"""
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from devflow_ai.coordinator import RunCoordinator
from devflow_ai.enums import RunStatus, TaskStatus
from devflow_ai.execution_binding import (
    ExecutionBindingError, package_modules, running_package_root, verify_execution_binding,
    verify_task_version,
)
from devflow_ai.execution_manifest import build_manifest, manifest_sha256
from devflow_ai.models import Run
from devflow_ai.services import AdmissionRecord, TaskService

# Runs in the child process. It imports nothing before the interpreter resolves ``devflow_ai``
# from PYTHONPATH, refuses when that resolution is not the requested directory, verifies the
# artifact against the digest it was given, and only then executes the probe and a real DevFlow
# function. It reports provenance so the test can check where the code actually came from.
BOOTSTRAP = r'''
import json
import os
import sys

OUT = os.environ["DEVFLOW_PROBE_OUT"]
ROOT = os.path.realpath(os.environ["DEVFLOW_ARTIFACT_ROOT"])
# Editable installs may add the live source directory via a site-packages .pth file.
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
except Exception as error:
    emit({"outcome": "import_failed", "error_type": type(error).__name__,
          "detail": str(error), "sys_path": [entry for entry in sys.path if entry]}, 3)

package_file = os.path.realpath(devflow_ai.__file__)
if package_file != os.path.join(ROOT, "devflow_ai", "__init__.py"):
    emit({"outcome": "wrong_artifact", "package_file": package_file, "root": ROOT}, 4)

from devflow_ai.execution_binding import loaded_package_modules, verify_optional_binding

try:
    verified = verify_optional_binding(
        manifest_path=os.environ["DEVFLOW_ARTIFACT_MANIFEST"],
        expected_sha256=os.environ["DEVFLOW_ARTIFACT_SHA256"],
        require_complete=True)
except Exception as error:
    emit({"outcome": "refused", "error_type": type(error).__name__, "detail": str(error)}, 2)

# Target logic: only after the version check above returned the pinned digest.
from devflow_ai.benchmark_record import SCHEMA_VERSION
from devflow_ai.verification_config import command_evidence_sha256

probe = getattr(devflow_ai, "artifact_probe", None)
inside, outside = loaded_package_modules(ROOT)
emit({"outcome": "loaded", "package_file": package_file, "root": ROOT, "verified": verified,
      "probe": probe() if callable(probe) else None,
      "schema_version": SCHEMA_VERSION,
      "command_evidence": command_evidence_sha256(("python", "-V"), "."),
      "loaded_modules": inside, "outside": outside,
      "sys_path": [entry for entry in sys.path if entry]}, 0)
'''

# Variables the interpreter itself may need on Windows; everything else is set explicitly.
_INHERITED = ("PATH", "SYSTEMROOT", "SystemRoot", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP",
              "HOME", "USERPROFILE", "LANG", "LC_ALL")


@dataclass(frozen=True)
class Artifact:
    """One local copy of the package, its manifest and the trusted digest for that manifest."""

    root: Path        # directory that contains the ``devflow_ai`` package copy
    package: Path     # .../devflow_ai itself
    manifest: Path
    digest: str


def _artifact_copy(tmp_path: Path, name: str, marker: str) -> Artifact:
    """Copy the running package, add a distinguishing probe, and inventory every module."""
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


def _child_env(root: Path, manifest: Path, digest: str, out: Path) -> dict:
    """A child environment where the artifact directory is the first source of ``devflow_ai``.

    ``PYTHONPATH`` is replaced, not extended, and the inherited ``DEVFLOW_*`` variables are not
    passed on. Another importable copy may still exist (an installed package, for instance);
    the child checks the origin itself and refuses anything else.
    """
    env = {name: os.environ[name] for name in _INHERITED if name in os.environ}
    env.update(
        PYTHONPATH=str(root),
        PYTHONDONTWRITEBYTECODE="1",
        DEVFLOW_ARTIFACT_ROOT=str(root),
        DEVFLOW_LIVE_SOURCE=str(running_package_root().parent.resolve()),
        DEVFLOW_ARTIFACT_MANIFEST=str(manifest),
        DEVFLOW_ARTIFACT_SHA256=str(digest),
        DEVFLOW_PROBE_OUT=str(out),
    )
    return env


def _launch(artifact: Artifact, *, out: Path, root: Path | None = None,
            manifest: Path | None = None, digest: str | None = None,
            timeout: float = 120) -> tuple[subprocess.CompletedProcess, dict | None]:
    """Start one child from ``root`` and return its report (written only by the child)."""
    target = root if root is not None else artifact.root
    env = _child_env(target, manifest or artifact.manifest, digest or artifact.digest, out)
    # An empty working directory: with ``-c`` the cwd is on ``sys.path``, and it must not be
    # able to supply the package by accident.
    workdir = out.parent / f"cwd-{uuid4().hex}"
    workdir.mkdir()
    result = subprocess.run(
        [sys.executable, "-c", BOOTSTRAP], env=env, cwd=str(workdir),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    payload = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else None
    return result, payload


def _task(factory):
    with factory() as session:
        task, _ = TaskService(session).create_task(
            title="Artifact launch task", description="Constructed fixture", repo="example-repo",
            idempotency_key=uuid4())
        task_id = task.id
    with factory() as session:
        TaskService(session).apply_admission_decision(
            task_id, TaskStatus.ELIGIBLE, base_commit="a" * 40,
            admission_record=AdmissionRecord(reason="fixture", evidence=["fixture"]))
    return task_id


def _claim(factory, task_id, digest):
    with factory() as session:
        run, _ = RunCoordinator(session).start_initial_run(
            task_id, "artifact-launch-worker", f"key-{uuid4()}", execution_sha256=digest)
        return run.id


def test_one_child_process_loads_the_selected_artifact_and_reports_its_provenance(tmp_path):
    v0 = _artifact_copy(tmp_path, "v0", "v0")
    # The control plane verifies the same bytes before anything is started.
    binding = verify_execution_binding(
        manifest_path=v0.manifest, expected_sha256=v0.digest, root=v0.package)
    assert binding.sha256 == v0.digest and binding.file_count == len(package_modules(v0.package))

    result, payload = _launch(v0, out=tmp_path / "probe.json")

    assert result.returncode == 0, result.stderr
    assert payload["outcome"] == "loaded"
    assert payload["verified"] == v0.digest
    assert Path(payload["package_file"]) == (v0.package / "__init__.py").resolve()
    assert payload["probe"] == "v0"
    assert payload["outside"] == []
    assert "devflow_ai/__init__.py" in payload["loaded_modules"]  # relative to the artifact root
    # A real DevFlow function from the artifact ran and produced the same value as the live one.
    from devflow_ai.verification_config import command_evidence_sha256
    assert payload["command_evidence"] == command_evidence_sha256(("python", "-V"), ".")
    # Nothing on the child's import path could have supplied the working tree's package.
    live_parent = str(running_package_root().parent.resolve())
    assert live_parent not in {str(Path(entry).resolve()) for entry in payload["sys_path"]}


def test_two_local_versions_are_distinguishable_and_the_live_tree_is_never_used(tmp_path):
    v0 = _artifact_copy(tmp_path, "v0", "v0")
    v1 = _artifact_copy(tmp_path, "v1", "v1")
    assert v0.digest != v1.digest

    first, first_payload = _launch(v0, out=tmp_path / "first.json")
    second, second_payload = _launch(v1, out=tmp_path / "second.json")

    assert (first.returncode, second.returncode) == (0, 0)
    assert (first_payload["probe"], second_payload["probe"]) == ("v0", "v1")
    assert first_payload["package_file"] != second_payload["package_file"]
    # The live tree is what a silent fallback would load, and it has no probe at all.
    import devflow_ai
    assert not hasattr(devflow_ai, "artifact_probe")
    for payload in (first_payload, second_payload):
        assert payload["package_file"] != str((running_package_root() / "__init__.py").resolve())


def test_a_bad_digest_changed_byte_or_missing_file_is_refused_before_target_logic(tmp_path):
    v0 = _artifact_copy(tmp_path, "v0", "v0")
    out = tmp_path / "probe.json"

    # Wrong digest at the control plane: refused before any process is started.
    with pytest.raises(ExecutionBindingError, match="pinned digest"):
        verify_execution_binding(manifest_path=v0.manifest, expected_sha256="f" * 64,
                                 root=v0.package)
    assert not out.exists()
    # A changed byte under a listed file.
    module = v0.package / "benchmark_record.py"
    original = module.read_bytes()
    module.write_bytes(original + b"\n# changed\n")
    with pytest.raises(ExecutionBindingError, match="digest does not match"):
        verify_execution_binding(manifest_path=v0.manifest, expected_sha256=v0.digest,
                                 root=v0.package)
    module.write_bytes(original)
    # A listed file that is gone.
    module.unlink()
    with pytest.raises(ExecutionBindingError, match="cannot be read|missing"):
        verify_execution_binding(manifest_path=v0.manifest, expected_sha256=v0.digest,
                                 root=v0.package)
    module.write_bytes(original)
    # The same refusal inside the child: a caller that skips the control-plane check and hands
    # the child another digest still gets no target logic executed.
    result, payload = _launch(v0, out=out, digest="f" * 64)
    assert result.returncode == 2
    assert payload["outcome"] == "refused" and "pinned digest" in payload["detail"]
    assert "probe" not in payload and "command_evidence" not in payload


def test_an_unavailable_artifact_is_never_replaced_by_the_running_version(tmp_path):
    v0 = _artifact_copy(tmp_path, "v0", "v0")
    missing = tmp_path / "absent"

    with pytest.raises(ExecutionBindingError, match="cannot be read|missing"):
        verify_execution_binding(manifest_path=v0.manifest, expected_sha256=v0.digest,
                                 root=missing)

    result, payload = _launch(v0, out=tmp_path / "probe.json", root=missing)

    # Two honest outcomes, depending on whether the interpreter also has some other copy
    # importable (an installed package, for instance): it fails to import, or it notices the
    # package came from somewhere else. Neither runs the target logic.
    assert payload["outcome"] in {"import_failed", "wrong_artifact"}
    assert result.returncode == {"import_failed": 3, "wrong_artifact": 4}[payload["outcome"]]
    assert "probe" not in payload and "command_evidence" not in payload
    if payload["outcome"] == "wrong_artifact":
        # Whatever it found instead, it is not the requested directory — and it was refused.
        assert payload["package_file"] != str((missing / "devflow_ai" / "__init__.py").resolve())


def test_the_launched_artifact_is_the_version_the_task_recorded(
        worker_session_factory, tmp_path):
    factory = worker_session_factory
    v0 = _artifact_copy(tmp_path, "v0", "v0")
    v1 = _artifact_copy(tmp_path, "v1", "v1")
    task_id = _task(factory)
    run_id = _claim(factory, task_id, v0.digest)

    with factory() as session:
        binding = verify_task_version(
            session, task_id, manifest_path=v0.manifest, artifact_root=v0.package)
    assert binding.sha256 == v0.digest
    result, payload = _launch(v0, out=tmp_path / "probe.json")
    assert result.returncode == 0 and payload["verified"] == binding.sha256

    # The other local version cannot be selected for this Task, and nothing was rewritten.
    with factory() as session, pytest.raises(ExecutionBindingError, match="pinned digest"):
        verify_task_version(session, task_id, manifest_path=v1.manifest, artifact_root=v1.package)
    with factory() as session:
        assert session.get(Run, run_id).execution_sha256 == v0.digest
        assert [run.attempt_number for run in session.scalars(
            select(Run).where(Run.task_id == task_id))] == [1]

    # Unknown and mixed histories are still refused, not guessed.
    unknown_task = _task(factory)
    _claim(factory, unknown_task, None)
    with factory() as session, pytest.raises(ExecutionBindingError, match="unknown version"):
        verify_task_version(session, unknown_task, manifest_path=v0.manifest,
                            artifact_root=v0.package)
    mixed_task = _task(factory)
    _claim(factory, mixed_task, v0.digest)
    with factory.begin() as session:
        session.add(Run(task_id=mixed_task, attempt_number=2, status=RunStatus.FAILED,
                        creation_idempotency_key=f"fixture-{uuid4()}",
                        worker_id="fixture-worker", execution_sha256=v1.digest,
                        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                        finished_at=datetime.now(timezone.utc)))
    with factory() as session, pytest.raises(ExecutionBindingError, match="ambiguous"):
        verify_task_version(session, mixed_task, manifest_path=v0.manifest,
                            artifact_root=v0.package)
