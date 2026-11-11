"""Controller-owned black-box cases for a candidate with a stdin/stdout adapter.

This runner is separate from Verifier's public pytest command. It does not itself
authenticate a Run or write a Verification; the caller must bind any result.
"""
from dataclasses import dataclass, field
from pathlib import Path
import subprocess
from typing import Literal
from uuid import uuid4


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
