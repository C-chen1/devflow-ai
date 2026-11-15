"""First-run preflight for one benchmark pack.

Two of the first-run steps are preparation, not execution: check the trusted inputs, then
observe the pack's public baseline without any model. Both are here, and both stay away
from evidence: this module never creates a Task, Run, Verification or ToolCall, never
migrates a database, never calls a model and never reads credentials. Secrets are
reported as present or absent by name only.

The baseline observation matters because it exercises the same derivation a real Run
uses — manifest to ``VerificationConfig`` to assembled command to a run-scoped copy, with
the pinned interpreter and import path — and then parses the JUnit report with the same
parser formal verification uses. That is enough to separate pack/environment problems
from agent problems before any budget is spent. It is *not* a recorded baseline: a real
Run must still fail this test in its own evidence before a patch is considered.

External connections are never inferred here. A static scan can raise a flag, but only a
trusted verifier may change the declaration, so an unverified pack stays ``unknown``.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from threading import Event
from uuid import uuid4

from devflow_ai.harness import GitCommandFailed, GitCommandTimedOut, run_registered_command
from devflow_ai.task_pack import load_manifest, to_verification_config
from devflow_ai.verification_config import (
    copied_public_tests_sha256, expected_command_evidence, public_check_command,
    public_test_modules, public_tests_copy_root,
)
from devflow_ai.verifier import parse_pytest_junit_report

__all__ = [
    "PreflightError", "check_checkout", "check_database", "check_docker", "check_environment",
    "check_interpreter", "check_policy_snapshot", "main", "observe_public_test_connections",
    "run_public_baseline",
]

DEFAULT_MAX_DURATION_SECONDS = 1200
DEFAULT_MAX_MODEL_TOKENS = 80_000
DEFAULT_BASELINE_TIMEOUT_SECONDS = 300
MODEL_ENVIRONMENT_NAMES = ("DEEPSEEK_API_KEY", "DEVFLOW_DEEPSEEK_MODEL")

# Static hints that a public test may reach the network. A hit is a question to answer,
# never a declaration: only the trusted side may set `external_connections`.
NETWORK_HINTS = (
    b"import socket", b"import urllib", b"import http", b"import requests", b"import httpx",
    b"import aiohttp", b"from urllib", b"127.0.0.1", b"localhost", b"https://", b"http://",
)

_INTERPRETER_PROBE = (
    "import json, platform, sys\n"
    "info = {'version': platform.python_version(), 'executable': sys.executable}\n"
    "try:\n"
    "    import pytest\n"
    "except Exception:\n"
    "    info['pytest'] = None\n"
    "else:\n"
    "    info['pytest'] = pytest.__version__\n"
    "    info['pytest_path'] = pytest.__file__\n"
    "print(json.dumps(info))\n"
)


# Variables whose names look like credentials are removed from the environment the public
# checks run in, so a benchmark test cannot read (or print) a model key. Only names are
# handled; no value is ever read, compared or logged.
CREDENTIAL_NAME_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")

_IMPORT_PROBE = (
    "import importlib.util, json, os, sys\n"
    "name = {name!r}\n"
    "expected = {expected!r}\n"
    "spec = importlib.util.find_spec(name)\n"
    "origin = getattr(spec, 'origin', None)\n"
    "if spec is not None and spec.submodule_search_locations:\n"
    "    origin = origin or list(spec.submodule_search_locations)[0]\n"
    "root = os.path.realpath(expected)\n"
    "inside = False\n"
    "if origin:\n"
    "    actual = os.path.realpath(origin)\n"
    "    try:\n"
    "        inside = os.path.normcase(os.path.commonpath([actual, root])) == os.path.normcase(root)\n"
    "    except ValueError:\n"
    "        inside = False\n"
    "print(json.dumps({{'origin': origin, 'expected': root, 'inside': bool(inside),\n"
    "                   'search_path': [str(entry) for entry in sys.path[:10]]}}))\n"
)


@contextmanager
def _without_credentials():
    """Run the body with credential-shaped environment variables removed.

    Yields the names that were removed, never their values. The harness builds the child
    environment from ``os.environ`` at spawn time, so removing the names here is what keeps
    them out of the public checks' process. The mapping is restored afterwards so the
    operator's shell is unaffected.
    """
    removed = {}
    for name in list(os.environ):
        if any(marker in name.upper() for marker in CREDENTIAL_NAME_MARKERS):
            removed[name] = os.environ.pop(name)
    try:
        yield sorted(removed)
    finally:
        os.environ.update(removed)


class PreflightError(RuntimeError):
    """A trusted input cannot be checked at all (as opposed to failing the check)."""


def _run(args: list[str], *, timeout_seconds: float, cwd: Path | None = None,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout_seconds, cwd=cwd, env=env)
    except subprocess.TimeoutExpired:
        raise PreflightError(f"Command timed out: {Path(args[0]).name}") from None
    except OSError as error:
        raise PreflightError(f"Command is unavailable: {Path(args[0]).name} ({type(error).__name__})") from None


def _git(checkout: Path, *args: str, timeout_seconds: float = 30) -> bytes:
    result = _run(["git", "-C", str(checkout), *args], timeout_seconds=timeout_seconds)
    if result.returncode != 0:
        raise PreflightError(
            f"git {' '.join(args)} failed: {result.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return result.stdout


def check_policy_snapshot(
    path: Path, *, max_duration_seconds: int = DEFAULT_MAX_DURATION_SECONDS,
    max_model_tokens: int = DEFAULT_MAX_MODEL_TOKENS,
) -> dict:
    """Validate one snapshot and report whether it can carry this run's limits."""
    from devflow_ai.benchmark_task import check_snapshot_supports_run, load_policy_snapshot

    snapshot = load_policy_snapshot(Path(path))
    problems = check_snapshot_supports_run(
        snapshot, max_duration_seconds=max_duration_seconds, max_model_tokens=max_model_tokens)
    return {
        "check": "policy", "ok": not problems, "path": str(Path(path)),
        "policy_version": snapshot.policy_version,
        "model_id_source": ("DEVFLOW_DEEPSEEK_MODEL plus the pack's policy reference; a Policy "
                            "snapshot carries budgets and permissions, not a model id."),
        "budget": {
            "max_run_duration_seconds": snapshot.budget.max_run_duration_seconds,
            "max_llm_tokens_per_run": snapshot.budget.max_llm_tokens_per_run,
            "max_retries_per_task": snapshot.budget.max_retries_per_task,
            "max_rediagnose_attempts": snapshot.budget.max_rediagnose_attempts,
            "max_graph_steps_per_run": snapshot.budget.max_graph_steps_per_run,
            "max_tool_calls_per_run": snapshot.budget.max_tool_calls_per_run,
        },
        "permissions": {
            role.value.lower(): sorted(tools)
            for role, tools in snapshot.permissions.allowed_tools_by_role.items()
        },
        "problems": problems,
    }


def check_interpreter(executable: str) -> dict:
    """Check the pinned interpreter: absolute, runnable, and able to import pytest."""
    path = Path(executable)
    problems = []
    if not path.is_absolute():
        problems.append("the interpreter must be an absolute path")
    elif not path.exists():
        problems.append("the interpreter does not exist")
    elif os.name == "nt" and path.suffix.lower() != ".exe":
        problems.append("on Windows the pinned interpreter must end in .exe")
    if problems:
        return {"check": "interpreter", "ok": False, "path": str(path), "problems": problems}
    result = _run([str(path), "-B", "-I", "-c", _INTERPRETER_PROBE], timeout_seconds=60)
    if result.returncode != 0:
        return {"check": "interpreter", "ok": False, "path": str(path),
                "problems": ["the interpreter could not report its version"],
                "stderr": result.stderr.decode("utf-8", "replace").strip()[:400]}
    try:
        info = json.loads(result.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return {"check": "interpreter", "ok": False, "path": str(path),
                "problems": ["the interpreter version probe returned no JSON"]}
    if info.get("pytest") is None:
        problems.append("pytest cannot be imported by the pinned interpreter")
    return {"check": "interpreter", "ok": not problems, "path": str(path),
            "version": info.get("version"), "pytest": info.get("pytest"),
            "pytest_path": info.get("pytest_path"), "problems": problems}


def check_checkout(
    checkout: Path, base_commit: str, *, python_executable: str | None = None,
    pythonpath_relative: str | None = None, import_name: str | None = None,
) -> dict:
    """Check the target checkout is pinned, clean, and imports from itself."""
    root = Path(checkout)
    problems = []
    if not root.is_dir():
        return {"check": "checkout", "ok": False, "path": str(root),
                "problems": ["the checkout directory does not exist"]}
    head, changed = None, ""
    try:
        head = _git(root, "rev-parse", "HEAD").decode("ascii").strip()
        changed = _git(root, "status", "--porcelain").decode("utf-8", "replace").strip()
    except PreflightError as error:
        # A directory that is not a Git checkout is a failed check, not a crash.
        problems.append(f"the checkout cannot be read as a Git repository: {error}")
    if head is not None and head != base_commit:
        problems.append("HEAD is not the pinned base commit")
    if changed:
        problems.append("the checkout has local changes or untracked files")
    source = None
    if pythonpath_relative is not None:
        source = root / pythonpath_relative
        if not source.is_dir():
            problems.append(f"the declared import path '{pythonpath_relative}' is not a directory")
    imported_from = None
    import_inside = None
    import_search_path: list[str] = []
    if import_name and python_executable and source is not None:
        if not all(part.isidentifier() for part in import_name.split(".")):
            return {"check": "checkout", "ok": False, "path": str(root), "head": head,
                    "problems": [f"'{import_name}' is not a plain module name"]}
        # The child decides where the package came from: it compares the real paths on the
        # platform that actually ran the import, instead of re-deriving path semantics in
        # the parent (case, separators and junctions differ per platform).
        environment = {**os.environ, "PYTHONPATH": str(source)}
        probe = _run([python_executable, "-B", "-c", _IMPORT_PROBE.format(
            name=import_name, expected=str(source))], timeout_seconds=120, cwd=root, env=environment)
        if probe.returncode != 0:
            problems.append(f"'{import_name}' cannot be imported from the checkout: "
                            + probe.stderr.decode("utf-8", "replace").strip()[:300])
        else:
            try:
                observed = json.loads(probe.stdout.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                problems.append("the import probe returned no JSON")
                observed = {}
            imported_from = observed.get("origin")
            import_inside = observed.get("inside")
            import_search_path = list(observed.get("search_path") or [])
            if not imported_from:
                problems.append(f"'{import_name}' was not found on the interpreter's path")
            elif import_inside is not True:
                problems.append(f"'{import_name}' is imported from outside the pinned import "
                                f"path: {imported_from} (expected under {source})")
    return {
        "check": "checkout", "ok": not problems, "path": str(root), "head": head,
        "base_commit": base_commit, "dirty": bool(changed),
        "import_path": (str(source) if source else None),
        "imported_from": imported_from, "import_inside": import_inside,
        "import_search_path": import_search_path,
        "import_decided_by": ("the child process, using real paths on the platform that ran "
                              "the import"),
        "problems": problems,
    }


def check_environment(names: tuple[str, ...] = MODEL_ENVIRONMENT_NAMES) -> dict:
    """Report which model settings exist; never read a secret back."""
    present = {name: bool(os.environ.get(name)) for name in names}
    return {
        "check": "environment", "ok": all(present.values()), "present": present,
        "model_id": os.environ.get("DEVFLOW_DEEPSEEK_MODEL") or None,
        "note": "Presence only; secret values are never read back, printed or logged.",
    }


def check_docker(*, timeout_seconds: float = 15) -> dict:
    """Ask the Docker daemon for its version; this starts no container."""
    result = _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout_seconds=timeout_seconds)
    version = result.stdout.decode("utf-8", "replace").strip()
    if result.returncode != 0 or not version:
        return {"check": "docker", "ok": False, "server_version": None,
                "problems": ["the Docker daemon did not answer"]}
    return {"check": "docker", "ok": True, "server_version": version, "problems": []}


def project_migration_head(migrations_directory: Path | None = None) -> str | None:
    """Return the migration this checkout expects, read from the local scripts."""
    directory = Path(migrations_directory) if migrations_directory is not None else (
        Path(__file__).resolve().parents[2] / "migrations")
    if not Path(directory).is_dir():
        return None
    try:
        from alembic.script import ScriptDirectory

        return ScriptDirectory(str(directory)).get_current_head()
    except Exception:  # an unreadable script tree is reported as "unknown", not guessed
        return None


def check_database(*, timeout_seconds: float = 15, migrations_directory: Path | None = None) -> dict:
    """Read the configured database without writing anything to it.

    Reporting the database name and the recorded Alembic revision is what lets an operator
    confirm the connection points where they think it does, and whether that target is at
    the revision this checkout expects. It still proves nothing about isolation: a schema
    can sit at head and share a database with unrelated data. Nothing here migrates.
    """
    from sqlalchemy import text

    from devflow_ai.database import create_db_engine

    engine = None
    database = revision = None
    try:
        engine = create_db_engine()
        with engine.connect() as connection:
            connection.execute(text("select 1"))
            database = connection.execute(text("select current_database()")).scalar_one()
            schema = connection.execute(text("select current_schema()")).scalar_one()
            tables = connection.execute(text(
                "select count(*) from information_schema.tables where table_schema = current_schema()"
            )).scalar_one()
            try:
                revision = connection.execute(
                    text("select version_num from alembic_version")).scalar_one_or_none()
            except Exception:
                connection.rollback()
                revision = None
    except Exception as error:  # an unreachable or unconfigured database is the answer
        return {"check": "database", "ok": False, "connected": False,
                "problems": [f"database is not reachable or not configured: {type(error).__name__}"]}
    finally:
        if engine is not None:
            engine.dispose()
    head = project_migration_head(migrations_directory)
    problems = []
    at_head = None if (revision is None or head is None) else revision == head
    if at_head is False:
        problems.append(
            f"the database is at revision {revision} but this checkout expects {head}; "
            f"running here would exercise a schema the code does not describe")
    return {
        "check": "database", "ok": not problems, "connected": True,
        "database": database, "schema": schema, "table_count": int(tables),
        "alembic_revision": revision, "project_head": head, "at_head": at_head,
        "means": ("a reachable database, its name, and the Alembic revision recorded there; "
                  "it does not prove the schema is complete, that it is isolated from other "
                  "data, or that this target is the one you meant to use"),
        "problems": problems,
    }


def _docker_image_present(image: str, *, timeout_seconds: float = 15) -> tuple[bool, str | None]:
    """Ask the local Docker daemon whether the image is already there; it is never pulled."""
    try:
        result = _run(["docker", "image", "inspect", image, "--format", "{{.Id}}"],
                      timeout_seconds=timeout_seconds)
    except PreflightError:
        return False, None
    if result.returncode != 0:
        return False, None
    return True, (result.stdout.decode("utf-8", "replace").strip() or None)


def check_independent_material(
    pack_dir: Path, material_path: Path, *, probe_image: bool = True,
    image_probe=None,
) -> dict:
    """Check the trusted evaluation material without running any of it.

    Only identity is inspected: structure, suite version against the pack, the calibration
    workspaces and their commit, and whether the image is already present locally. Case
    bytes are read by the same strict loader a run uses and are never returned, printed
    or logged — this check reports a count, not content.
    """
    from devflow_ai.benchmark_run import BenchmarkRunError, load_independent_material

    manifest = load_manifest(Path(pack_dir))
    suite_version = (manifest.independent_suite.suite_version
                     if manifest.independent_suite is not None else None)
    expected_sha256 = (manifest.independent_suite.materials_sha256
                       if manifest.independent_suite is not None else None)
    problems = []
    if suite_version is None:
        problems.append("the pack does not pin an independent suite version yet")
    if expected_sha256 is None:
        problems.append("the pack does not pin a materials_sha256, so the file cannot be verified")
    try:
        file_sha256 = sha256(Path(material_path).read_bytes()).hexdigest()
    except OSError as error:
        return {"check": "independent_material", "ok": False,
                "materials_sha256": expected_sha256, "file_sha256": None, "digest_matches": None,
                "problems": [*problems,
                             f"the material file is unreadable: {type(error).__name__}"]}
    digest_matches = None if expected_sha256 is None else file_sha256 == expected_sha256
    try:
        material, declared_version = load_independent_material(
            Path(material_path), expected_suite_version=suite_version,
            expected_sha256=expected_sha256)
    except BenchmarkRunError as error:
        return {"check": "independent_material", "ok": False,
                "materials_sha256": expected_sha256, "file_sha256": file_sha256,
                "digest_matches": digest_matches, "problems": [*problems, str(error)]}
    baseline, corrected, expected_commit = material.calibration_workspaces
    for name, workspace in (("baseline", baseline), ("corrected", corrected)):
        if not workspace.is_dir():
            problems.append(f"the {name} calibration workspace is not a directory")
    if baseline.resolve() == corrected.resolve():
        problems.append("the two calibration workspaces are the same directory")
    corrected_head = None
    if corrected.is_dir():
        try:
            corrected_head = _git(corrected, "rev-parse", "HEAD").decode("ascii").strip()
        except PreflightError as error:
            problems.append(f"the corrected calibration workspace is not a Git repository: {error}")
        else:
            if corrected_head != expected_commit:
                problems.append("the corrected calibration workspace is not at the declared commit")
    image_present, image_id = None, None
    if probe_image:
        probe = image_probe or _docker_image_present
        image_present, image_id = probe(material.image)
        if image_present is False:
            problems.append("the candidate image is not present locally (it is never pulled here)")
    return {
        "check": "independent_material", "ok": not problems,
        "suite_version": declared_version, "pack_suite_version": suite_version,
        # The digest pins the exact bytes that were calibrated; it identifies the file and
        # says nothing about its origin, isolation or whether the cases are any good.
        "materials_sha256": expected_sha256, "file_sha256": file_sha256,
        "digest_matches": digest_matches,
        "image": material.image, "image_present": image_present, "image_id": image_id,
        "case_count": len(material.cases), "adapter_argv_length": len(material.command),
        "calibration": {"baseline_workspace": str(baseline), "corrected_workspace": str(corrected),
                        "expected_commit": expected_commit, "corrected_head": corrected_head},
        "note": ("Identity only: case bytes are loaded but never returned, printed or logged."),
        # Passing this check says nothing about whether the suite can tell a defect from a
        # fix. That is calibration, and only the formal path runs it.
        "formal_gate": {
            "calibration_executed_by": "independent_evaluation.calibrate_stdio_suite",
            "requirements": ["the defect baseline must fail the trusted cases",
                             "the reference/corrected version must pass them"],
            "not_checked_here": ("no container is started and no calibration claim is made; a "
                                 "matching digest identifies bytes, it does not prove the suite "
                                 "is correct, isolated, or well designed"),
        },
        "problems": problems,
    }


def observe_public_test_connections(pack_dir: Path) -> dict:
    """List static network hints in the pack's public tests; declares nothing."""
    root = Path(pack_dir)
    hits = {}
    for path in sorted(root.glob("*.py")):
        text = path.read_bytes()
        found = sorted({hint.decode("ascii") for hint in NETWORK_HINTS if hint in text})
        if found:
            hits[path.name] = found
    return {
        "check": "external_connections_observation", "ok": True,
        "hits": hits,
        "declaration": "unchanged",
        "note": ("Static observation only. It cannot change the pack's declaration; only a "
                 "trusted verifier may set `external_connections` to none or required."),
    }


def run_public_baseline(
    pack_dir: Path, checkout: Path, python_executable: str, *,
    artifacts_root: Path | None = None, timeout_seconds: float = DEFAULT_BASELINE_TIMEOUT_SECONDS,
    include_output: bool = False,
) -> dict:
    """Run the pack's public checks exactly as a Run would assemble them, with no model.

    Writes only into a preflight directory: no Run, no ToolCall, no Verification. The
    child runs without credential-shaped environment variables, and its output is only
    returned when the caller explicitly asks for it — a benchmark test must not be the
    reason a key ends up in a log.
    """
    manifest = load_manifest(Path(pack_dir))
    config = to_verification_config(
        manifest, task_id=uuid4(), public_tests_root=pack_dir, python_executable=python_executable)
    artifacts = (Path(artifacts_root) if artifacts_root is not None
                 else Path(tempfile.mkdtemp(prefix="devflow-preflight-")))
    artifacts.mkdir(parents=True, exist_ok=True)
    modules = public_test_modules(config)
    copy_root = public_tests_copy_root(artifacts)
    copy_root.mkdir(parents=True, exist_ok=True)
    for name, data in modules.items():
        (copy_root / name).write_bytes(data)
    command = public_check_command(config, artifact_root=artifacts, modules=modules)
    expected = expected_command_evidence(config, artifacts)
    report_dir = artifacts / "pytest"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "report.xml"
    registry = {config.command_name: (*command, f"--junitxml={report_path}")}

    problems = []
    exit_code = None
    timed_out = False
    stdout = stderr = b""
    start_failure = None
    with _without_credentials() as removed_names:
        try:
            result = run_registered_command(
                Path(checkout), config.command_name, registry=registry, timeout_seconds=timeout_seconds,
                stop_event=Event(), artifact_root=artifacts, pythonpath_relative=config.pythonpath_relative,
            )
            exit_code, stdout, stderr = result.returncode, result.preview.stdout, result.preview.stderr
        except GitCommandFailed as error:
            exit_code, stdout, stderr = error.returncode, error.preview.stdout, error.preview.stderr
        except GitCommandTimedOut:
            timed_out = True
            problems.append("the public checks did not finish within the preflight limit")
        except (ValueError, OSError) as error:
            start_failure = type(error).__name__
    if start_failure is not None:
        return {
            "check": "public_baseline", "ok": False, "writes_evidence": False,
            "problems": [f"the public checks could not start: {start_failure}"],
            "command": list(command), "artifacts_root": str(artifacts),
            "child_environment_removed": removed_names,
        }

    observed: dict[str, str] = {}
    if report_path.is_file():
        try:
            parsed = parse_pytest_junit_report(
                report_path.read_bytes(), tool_call_id=uuid4(), command_name=config.command_name)
            observed = {test.name: test.status.value for test in parsed.tests}
        except ValueError as error:
            problems.append(f"the JUnit report could not be parsed: {error}")
    else:
        problems.append("no JUnit report was written")

    declared = tuple(config.target_tests)
    missing = [name for name in declared if name not in observed]
    target_statuses = {name: observed.get(name) for name in declared}
    controls = {name: status for name, status in observed.items() if name not in declared}
    controls_passed = sum(1 for status in controls.values() if status == "passed")
    if exit_code not in (0, 1) and not timed_out:
        problems.append(f"the public checks exited with {exit_code}, which is not a pytest result")
    if missing:
        problems.append("declared target tests are absent from the report: " + ", ".join(missing))
    not_failing = [name for name, status in target_statuses.items() if status != "failed"]
    if not_failing:
        problems.append("declared target tests did not fail at the base commit: "
                        + ", ".join(not_failing))
    if manifest.baseline.passed_controls is not None and controls_passed != manifest.baseline.passed_controls:
        problems.append(f"{controls_passed} control test(s) passed, the pack records "
                        f"{manifest.baseline.passed_controls}")
    if copied_public_tests_sha256(artifacts) != expected.public_tests_sha256:
        problems.append("the preflight copy does not match the pack it was copied from")
    copied_into_checkout = [name for name in modules if (Path(checkout) / name).exists()]

    return {
        "check": "public_baseline", "ok": not problems, "writes_evidence": False,
        "pack_id": manifest.pack_id, "base_commit": manifest.base_commit,
        "declared_targets": list(declared), "observed": observed,
        "target_statuses": target_statuses, "controls": controls,
        "controls_passed": controls_passed, "exit_code": exit_code, "timed_out": timed_out,
        "command": list(command), "configured_command": list(config.command),
        "command_sha256": expected.command_sha256,
        "public_tests_sha256": expected.public_tests_sha256,
        "artifacts_root": str(artifacts), "report_path": str(report_path),
        "files_in_checkout": copied_into_checkout,
        "child_environment_removed": removed_names,
        # The command digest covers the assembled command, which contains this run's copy
        # path. Two different artifact roots therefore produce two different digests; only
        # the per-file content digest can be compared across runs.
        "command_sha256_scope": ("covers the assembled command including the copy path under "
                                 "artifacts_root; compare it only against evidence produced "
                                 "under the same artifact root"),
        "comparable_across_runs": ["public_tests_sha256"],
        "stdout_tail": (stdout.decode("utf-8", "replace")[-2000:] if include_output else None),
        "stderr_tail": (stderr.decode("utf-8", "replace")[-1000:] if include_output else None),
        "output_included": include_output,
        "problems": problems,
    }


def formal_acceptance_blockers(manifest, *, material_supplied: bool) -> list[str]:
    """List what still stands between this pack and a formal acceptance.

    Read from the pack and from what the operator passed in — never inferred, never filled
    in. An empty list means nothing here blocks the formal gate; it does not mean the run
    will pass, and it does not replace the trusted side's own verification.
    """
    blockers = []
    if manifest.external_connections != "none":
        blockers.append(
            f"external_connections is '{manifest.external_connections}'; the formal gate "
            f"requires a trusted 'none'")
    if manifest.policy is None:
        blockers.append("the pack does not pin a Policy reference yet")
    if manifest.independent_suite is None:
        blockers.append("the pack does not pin an independent suite")
    if not material_supplied:
        blockers.append("no trusted evaluation material was supplied for this run")
    if manifest.baseline.status.value != "verified_here":
        blockers.append(
            f"the pack's baseline is '{manifest.baseline.status.value}', not re-verified here")
    return blockers


def _report(result: dict) -> None:
    print(json.dumps(result, ensure_ascii=False, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check the trusted inputs for one benchmark pack and observe its public baseline. "
                    "No model is called and no evidence is written.")
    parser.add_argument("--pack", required=True, help="Task-pack directory.")
    parser.add_argument("--checkout", help="Target checkout at the pinned base commit.")
    parser.add_argument("--python", dest="python_executable", help="Pinned interpreter (absolute path).")
    parser.add_argument("--policy", help="Policy snapshot JSON to validate.")
    parser.add_argument("--artifacts-root", help="Directory for the preflight copy and report.")
    parser.add_argument("--import-name", help="Package the checkout must provide, e.g. click.")
    parser.add_argument("--material", help="Trusted independent-evaluation material (JSON). Its "
                                           "identity is checked; its cases are never shown.")
    parser.add_argument("--skip-docker", action="store_true", help="Do not probe the Docker daemon.")
    parser.add_argument("--check-db", action="store_true", help="Read the configured database (no writes).")
    parser.add_argument("--show-output", action="store_true",
                        help="Include the public checks' stdout/stderr tails in the result. Off by "
                             "default so captured output cannot leak into a log; the child already "
                             "runs without credential-shaped environment variables.")
    parser.add_argument("--json", action="store_true", help="Emit one JSON object per line.")
    args = parser.parse_args(argv)

    pack_dir = Path(args.pack)
    manifest = load_manifest(pack_dir)
    results: list[dict] = []
    if args.policy:
        results.append(check_policy_snapshot(Path(args.policy)))
    if args.python_executable:
        results.append(check_interpreter(args.python_executable))
    if args.checkout:
        results.append(check_checkout(
            Path(args.checkout), manifest.base_commit, python_executable=args.python_executable,
            pythonpath_relative=manifest.public_checks.pythonpath_relative,
            import_name=args.import_name))
    if args.material:
        results.append(check_independent_material(
            pack_dir, Path(args.material), probe_image=not args.skip_docker))
    results.append(check_environment())
    if not args.skip_docker:
        results.append(check_docker())
    if args.check_db:
        results.append(check_database())
    results.append(observe_public_test_connections(pack_dir))
    if args.checkout and args.python_executable:
        results.append(run_public_baseline(
            pack_dir, Path(args.checkout), args.python_executable,
            artifacts_root=(Path(args.artifacts_root) if args.artifacts_root else None),
            include_output=args.show_output))

    for result in results:
        _report(result)
    failed = [result["check"] for result in results if not result.get("ok", False)]
    print(json.dumps({
        "preflight": "failed" if failed else "ok", "failed_checks": failed,
        # Being able to run and being able to reach a formal acceptance are two questions.
        "formal_acceptance_blockers": formal_acceptance_blockers(
            manifest, material_supplied=bool(args.material)),
    }))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
