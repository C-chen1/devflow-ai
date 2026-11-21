"""Start the Worker entry **from a verified artifact copy**, on purpose and by name.

This is the control plane's smallest version-selection entry: an operator names a deployed copy
of the package, the manifest for it and the independently supplied pin. The launcher verifies
that copy here, then requests ``devflow_ai.worker_run`` with a fixed bootstrap and an argument
array, never a shell string. Interpreter startup hooks and installed packages remain outside
the manifest and can affect imports or execute before the entry's checks. If the copy is missing, changed or
does not match the pin, nothing is started.

What the launcher is and is not:

``artifact_root`` is the directory of the deployed package itself (the one that contains
``__init__.py``), which is exactly the root the child's own verification uses.

* it does **not** replace the Worker's own checks. ``worker_run`` verifies the pinned manifest
  against the package *it is running* before claiming, and ``Worker.execute_run`` verifies it
  again before any workspace work. The launcher hands those two flags down unchanged, so a
  process that ended up on another copy refuses instead of running it;
* ``require_complete`` here means "the inventory lists every Python module in that copy". The
  extra "modules this process imported from elsewhere" check only applies when the verified root
  *is* the running package, so it is skipped for a foreign copy — deliberately, and the child's
  own check still applies it to itself;
* the launcher cannot freeze the copy: files can change between this verification, the spawn and
  the child's own check. The pin makes that a refusal, not a silent fallback, but it is not an
  immutable deployment, a sandbox, or an isolation boundary between the artifact and the control
  plane.

**Startup order is part of the boundary.** A Python interpreter imports ``sitecustomize`` (and
processes ``.pth`` files) from everything on ``sys.path`` *before* any application code runs. If
the artifact's parent directory were on ``PYTHONPATH``, an unfixed ``sitecustomize.py`` sitting
next to the deployed package would run before the Worker ever checks the pin — reproduced by
test. So the artifact directory is **not** put on the import path by the environment at all: the
child is started as ``<python> -P -B -c <bootstrap> -- <artifact parent> <worker_run args…>`` and
the fixed bootstrap inserts that directory at ``sys.path[0]`` and runs the module as
``__main__``. This blocks a deploy-directory startup hook when that directory is absent from the
interpreter's existing startup paths. Site configuration or ``.pth`` files can themselves add it,
and hooks in ``site-packages`` can still run. This launcher does not pin that environment.

Credentials: the child gets an explicit allowlist (see ``child_environment``). Database settings
are inherited because the entry needs them; model credentials are **not** inherited unless the
operator names them, and no environment value is ever printed or returned.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from devflow_ai.execution_binding import (
    ExecutionBinding, ExecutionBindingError, verify_execution_binding,
)

WORKER_MODULE = "devflow_ai.worker_run"
# ``-P`` keeps the working directory off ``sys.path``; ``-B`` stops the child writing bytecode
# into the artifact, which would change the files the pin describes. The artifact directory is
# named on the command line and inserted by this bootstrap, never through ``PYTHONPATH``: that
# keeps interpreter startup from importing a ``sitecustomize`` that happens to sit next to the
# deployed package.
BOOTSTRAP = (
    "import runpy, sys\n"
    "separator = sys.argv.index('--')\n"
    "sys.path.insert(0, sys.argv[separator + 1])\n"
    "sys.argv = [sys.argv[0], *sys.argv[separator + 2:]]\n"
    "runpy.run_module('devflow_ai.worker_run', run_name='__main__', alter_sys=True)\n"
)
CONFIG_ENV_PREFIXES = ("DEVFLOW_DB_",)
DEFAULT_PASSTHROUGH = (
    "PATH", "SystemRoot", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR",
    "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "LC_ALL",
)
DEFAULT_TIMEOUT_SECONDS = None
_VERSION_FLAGS = ("--execution-manifest", "--execution-sha256")


class VersionLaunchError(ValueError):
    """The requested artifact cannot be verified, or the launch cannot be formed."""


@dataclass(frozen=True)
class LaunchResult:
    """What was verified and what the child returned; never contains the environment."""

    execution_sha256: str
    verified_files: int
    artifact_root: str
    python_executable: str
    returncode: int
    stdout: str = ""
    stderr: str = ""
    events: tuple[dict, ...] = field(default_factory=tuple)


def child_environment(
    artifact_root: Path | str, *, passthrough: Sequence[str] = (),
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the exact environment the child gets.

    Only the named allowlist, the ``DEVFLOW_DB_*`` settings and anything the operator names
    explicitly are copied. **No** ``PYTHONPATH`` is set (an inherited one is not passed on), the
    user site directory is disabled, and bytecode writing is off. The artifact directory travels
    on the child's command line and is inserted by the launcher's bootstrap, so interpreter
    startup does not receive it through PYTHONPATH. Site configuration or ``.pth`` files can
    still add that directory, and startup hooks there or in site-packages can affect imports.
    """
    source = os.environ if base is None else base
    env = {name: source[name] for name in DEFAULT_PASSTHROUGH if name in source}
    for name in source:
        if name.startswith(CONFIG_ENV_PREFIXES):
            env[name] = source[name]
    for name in passthrough:
        if not isinstance(name, str) or not name or name != name.strip():
            raise VersionLaunchError("passthrough names must be non-blank strings")
        if name in source:
            env[name] = source[name]
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    return env


def verify_artifact(
    *, artifact_root: Path | str, manifest_path: Path | str, execution_sha256: str,
) -> ExecutionBinding:
    """Verify the named copy against the independently supplied pin, before anything starts."""
    try:
        return verify_execution_binding(
            manifest_path=manifest_path, expected_sha256=execution_sha256,
            root=artifact_root, require_complete=True)
    except ExecutionBindingError as error:
        raise VersionLaunchError(str(error)) from None


def build_command(
    *, python_executable: Path | str, artifact_root: Path | str,
    worker_arguments: Sequence[str], manifest_path: Path | str, execution_sha256: str,
) -> list[str]:
    """One interpreter, one fixed bootstrap, one argument array — no shell, no defaults."""
    executable = Path(python_executable)
    if not executable.is_absolute():
        raise VersionLaunchError("python_executable must be an absolute path")
    if not executable.is_file():
        raise VersionLaunchError("python_executable does not name a usable interpreter")
    arguments = [str(argument) for argument in worker_arguments]
    if not arguments:
        raise VersionLaunchError("worker_arguments must not be empty")
    if any(argument == flag or argument.startswith(flag + "=")
           for argument in arguments for flag in _VERSION_FLAGS):
        raise VersionLaunchError(
            "The launcher supplies --execution-manifest and --execution-sha256 itself")
    deploy_directory = Path(artifact_root).resolve().parent
    if not deploy_directory.is_dir():
        raise VersionLaunchError("The artifact's deploy directory must exist")
    return [str(executable), "-P", "-B", "-c", BOOTSTRAP, "--", str(deploy_directory),
            *arguments, "--execution-manifest", str(Path(manifest_path).resolve()),
            "--execution-sha256", execution_sha256]


def _events(output: str) -> tuple[dict, ...]:
    """Parse JSON events from the supplied text; stream concatenation is not temporal order."""
    found = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and "event" in payload:
            found.append(payload)
    return tuple(found)


def launch_worker_run(
    *, artifact_root: Path | str, manifest_path: Path | str, execution_sha256: str,
    python_executable: Path | str, worker_arguments: Sequence[str],
    passthrough: Sequence[str] = (), cwd: Path | str | None = None,
    timeout_seconds: float | None = DEFAULT_TIMEOUT_SECONDS,
    environment: Mapping[str, str] | None = None,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> LaunchResult:
    """Verify the named copy, then start its own Worker entry and return what happened.

    Every refusal that can be decided here — an unverifiable copy, a pin that does not describe
    it, a missing interpreter, arguments that would fight the version flags — happens before the
    process is created. Once it is created, the child's exit code and events are returned as they
    are: a failing child is reported as failed, never as a successful task.
    """
    if Path(artifact_root).resolve().name != "devflow_ai":
        raise VersionLaunchError("artifact_root must be the devflow_ai package directory")
    binding = verify_artifact(artifact_root=artifact_root, manifest_path=manifest_path,
                              execution_sha256=execution_sha256)
    command = build_command(python_executable=python_executable,
                            artifact_root=artifact_root, worker_arguments=worker_arguments,
                            manifest_path=manifest_path, execution_sha256=execution_sha256)
    default_cwd = Path(artifact_root).resolve().parent
    working_directory = Path(cwd) if cwd is not None else default_cwd
    if not working_directory.is_dir():
        raise VersionLaunchError("The working directory must exist")
    env = child_environment(artifact_root, passthrough=passthrough, base=environment)
    try:
        completed = run(command, env=env, cwd=str(working_directory), shell=False,
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        # The child was stopped by the launcher; this is not a task outcome, so it is reported
        # as a launch failure rather than as an exit code.
        raise VersionLaunchError(
            "The worker entry did not finish within the launch timeout") from None
    except OSError:
        raise VersionLaunchError("The worker process could not be started") from None
    return LaunchResult(
        execution_sha256=binding.sha256, verified_files=binding.file_count,
        artifact_root=binding.root, python_executable=str(python_executable),
        returncode=completed.returncode, stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        events=_events((completed.stdout or "") + "\n" + (completed.stderr or "")))


def main(argv: list[str] | None = None) -> int:
    """CLI: ``-- <worker_run arguments>``; exit 2 when the launch itself is refused."""
    parser = argparse.ArgumentParser(
        description="Verify a deployed DevFlow copy and start its own worker_run entry")
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--execution-sha256", required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--passthrough", action="append", default=[], dest="passthrough")
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("worker_arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    worker_arguments = list(args.worker_arguments)
    if worker_arguments[:1] == ["--"]:
        worker_arguments = worker_arguments[1:]
    try:
        result = launch_worker_run(
            artifact_root=args.artifact_root, manifest_path=args.manifest,
            execution_sha256=args.execution_sha256, python_executable=args.python,
            worker_arguments=worker_arguments, passthrough=args.passthrough, cwd=args.cwd,
            timeout_seconds=args.timeout_seconds)
    except VersionLaunchError as error:
        print(json.dumps({"event": "version_launch_refused", "detail": str(error)}))
        return 2
    print(json.dumps({
        "event": "version_launch_finished", "execution_sha256": result.execution_sha256,
        "verified_files": result.verified_files, "returncode": result.returncode,
        "events": list(result.events),
    }))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
