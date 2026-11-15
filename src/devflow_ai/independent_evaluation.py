"""Controller-owned black-box cases for a candidate with a stdin/stdout adapter.

This runner is separate from Verifier's public pytest command. It does not itself
authenticate a Run or write a Verification; the caller must bind any result.
"""
from dataclasses import dataclass, field
import os
from pathlib import Path
import subprocess
from typing import Literal, Mapping
from urllib.parse import unquote, urlparse
from uuid import uuid4


class MaterialPlacementError(ValueError):
    """Trusted material was left somewhere the trusted side does not control."""


# Schemes that identify a repository the local filesystem cannot place.
REMOTE_REPOSITORY_SCHEMES = frozenset({"http", "https", "ssh", "git", "git+ssh", "ssh+git"})


def local_repository_path(repository: str | None) -> Path | None:
    """The local checkout directory a Task names, or ``None`` when there is none.

    ``None`` means the repository is remote (or unset), so this check cannot say anything
    about where the material sits relative to a checkout — the boundary is stated rather
    than guessed. A ``file://`` URL (including a UNC share such as ``file://server/share``)
    or a plain path (Windows drive paths, UNC paths, relative paths) is local.
    """
    if not repository or not repository.strip():
        return None
    value = repository.strip()
    parsed = urlparse(value)
    scheme = parsed.scheme.lower()
    # "C:\repos\x" and "D:/repos" parse with a one-letter "scheme"; they are drive paths.
    is_drive_path = len(scheme) == 1 and value[1:2] == ":"
    if scheme == "file":
        return local_path_from_file_url(value)
    if scheme in REMOTE_REPOSITORY_SCHEMES:
        return None
    if "@" in value and ":" in value.partition("@")[2].split("/")[0]:
        # scp-like SSH remote (git@host:org/repo.git): no local directory either.
        return None
    if scheme and not is_drive_path:
        return None
    return Path(value)


def local_path_from_file_url(url: str) -> Path | None:
    """Turn one ``file:`` URL into the local path it names, or ``None`` if it names none.

    Three shapes have to survive: ``file:///C:/repos/x`` and ``file://localhost/C:/repos/x``
    name a local path, ``file:///srv/repos/x`` names a POSIX path, and
    ``file://server/share/repo`` names a Windows network share — dropping that server would
    turn the path into the drive-relative ``\\share\\repo`` and lose the machine the material
    has to be compared against. Percent-encoding (including ``%20``) is decoded.
    """
    parsed = urlparse(url)
    server = unquote(parsed.netloc or "").split("@")[-1]
    path = unquote(parsed.path or "")
    if server and server.lower() != "localhost":
        share = path.lstrip("/").replace("/", "\\")
        return Path(f"\\\\{server}\\{share}") if share else None
    if len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]  # file:///C:/repos/x -> C:/repos/x
    return Path(path) if path else None


def is_within(child: Path | str, parent: Path | str) -> bool:
    """Whether ``child`` resolves inside ``parent``.

    Real paths (symlinks and junctions resolved) and the platform's case rules are used, so
    a link into the pack or a differently-cased Windows path is still recognised.
    """
    try:
        child_path = os.path.normcase(os.path.realpath(child))
        parent_path = os.path.normcase(os.path.realpath(parent))
    except (OSError, TypeError, ValueError):
        return False
    return child_path == parent_path or child_path.startswith(
        parent_path.rstrip(os.sep) + os.sep)


def require_material_placement(
    material_path: Path | str, *, pack_dir: Path | str, workspaces_root: Path | str,
    artifacts_root: Path | str, repository: str | None,
) -> None:
    """Refuse material kept inside anything the trusted side does not hold exclusively.

    The pack is published, the workspace and artifact roots are handed to the agent and
    copied around, and a local Task repository is the code under test. Material inside any
    of them is no longer the control plane's own copy, so it is rejected before it is read.
    A remote repository says nothing about the local filesystem: that case is reported as
    outside this check's reach instead of being silently treated as safe.
    """
    for label, root in (("task pack", pack_dir),
                        ("workspaces root", workspaces_root),
                        ("artifacts root", artifacts_root)):
        if is_within(material_path, root):
            raise MaterialPlacementError(
                f"The trusted material must be kept outside the {label}")
    checkout = local_repository_path(repository)
    if checkout is not None and is_within(material_path, checkout):
        raise MaterialPlacementError(
            "The trusted material must be kept outside the source repository")


@dataclass(frozen=True)
class PrivateCase:
    stdin: bytes = field(repr=False)
    expected_stdout: bytes = field(repr=False)


@dataclass(frozen=True)
class IndependentResult:
    outcome: Literal["passed", "failed", "unavailable"]
    planned_case_count: int
    image_id: str | None = None


@dataclass(frozen=True)
class IndependentEvaluationMaterial:
    """Trusted-controller inputs for one calibrated independent evaluation.

    Cases and their expected outputs stay out of repr and are never persisted;
    callers must keep this object out of Agent/model context and normal logs.
    """

    image: str
    command: tuple[str, ...]
    cases: tuple[PrivateCase, ...] = field(repr=False)
    calibration_workspaces: tuple[Path, Path, str]
    timeout_seconds: float = 15


@dataclass(frozen=True)
class SuiteCalibration:
    outcome: Literal["verified", "invalid", "unavailable"]
    baseline_outcome: Literal["passed", "failed", "unavailable"]
    corrected_outcome: Literal["passed", "failed", "unavailable"]
    image_id: str | None = None


def evaluate_stdio_candidate(*, workspace: Path, image: str, command: tuple[str, ...],
                             cases: tuple[PrivateCase, ...], timeout_seconds: float = 15) -> IndependentResult:
    """Execute each controller-held case without placing the suite in the candidate mount.

    Only the aggregate outcome and count leave this interface. Docker and the
    image are trusted operator configuration; this is not a container escape
    guarantee, and an adapter must use stdin/stdout for the target behavior.
    """
    if (not cases or not command or not image.strip() or timeout_seconds <= 0
            or any(not isinstance(case, PrivateCase) or not isinstance(case.stdin, bytes)
                   or not isinstance(case.expected_stdout, bytes) for case in cases)):
        raise ValueError("An image, command, cases and positive timeout are required")
    root = workspace.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Workspace must be a directory")
    try:
        inspected = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                                   capture_output=True, text=True, timeout=10, check=True)
        image_id = inspected.stdout.strip()
        if not image_id.startswith("sha256:") or len(image_id) != 71:
            return IndependentResult("unavailable", len(cases))
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return IndependentResult("unavailable", len(cases))
    docker_args = ["--rm", "--pull", "never", "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "64",
        "--memory", "256m", "--cpus", "1", "--user", "65534:65534", "--interactive",
        "--mount", f"type=bind,source={root},destination=/candidate,readonly", "--workdir", "/candidate",
        "--entrypoint", command[0], image_id, *command[1:]]
    failed = False
    for case in cases:
        container_name = f"devflow-eval-{uuid4().hex}"
        docker_command = ["docker", "run", "--name", container_name, *docker_args]
        try:
            process = subprocess.run(docker_command, input=case.stdin, capture_output=True,
                                     timeout=timeout_seconds, check=False)
        except subprocess.TimeoutExpired:
            try:
                subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, timeout=10, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
            return IndependentResult("unavailable", len(cases), image_id)
        except OSError:
            return IndependentResult("unavailable", len(cases), image_id)
        if process.returncode != 0:
            return IndependentResult("unavailable", len(cases), image_id)
        if process.stdout != case.expected_stdout:
            failed = True
    return IndependentResult("failed" if failed else "passed", len(cases), image_id)


def calibrate_stdio_suite(*, baseline_workspace: Path, corrected_workspace: Path, image: str,
                          command: tuple[str, ...], cases: tuple[PrivateCase, ...],
                          timeout_seconds: float = 15) -> SuiteCalibration:
    """Check that a controller-supplied suite rejects a baseline and accepts a known correction.

    The trusted caller is responsible for supplying the actual baseline and known correction.
    """
    if baseline_workspace.resolve(strict=True) == corrected_workspace.resolve(strict=True):
        raise ValueError("Baseline and corrected workspaces must be distinct")
    baseline = evaluate_stdio_candidate(workspace=baseline_workspace, image=image, command=command,
                                       cases=cases, timeout_seconds=timeout_seconds)
    corrected = evaluate_stdio_candidate(workspace=corrected_workspace, image=image, command=command,
                                        cases=cases, timeout_seconds=timeout_seconds)
    if "unavailable" in (baseline.outcome, corrected.outcome) or baseline.image_id != corrected.image_id:
        outcome = "unavailable"
    elif (baseline.outcome, corrected.outcome) == ("failed", "passed"):
        outcome = "verified"
    else:
        outcome = "invalid"
    return SuiteCalibration(outcome, baseline.outcome, corrected.outcome,
                            baseline.image_id if baseline.image_id == corrected.image_id else None)
