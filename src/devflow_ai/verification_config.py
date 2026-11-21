"""Public verification settings supplied by a trusted operator, not an Agent."""
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Annotated, Literal, Mapping
from pathlib import Path, PureWindowsPath
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator
from devflow_ai.task_pack import ScopeSpec

NonBlank = Annotated[str, StringConstraints(min_length=1)]

# Public tests that live in a task pack are copied under this run-scoped directory so the
# checkout keeps matching its commit while the command still runs the trusted files.
PUBLIC_TESTS_COPY_DIRECTORY = "public_tests"


class VerificationConfig(BaseModel):
    """Bind one public test command and target set to a specific Task and base."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: UUID
    base_commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    command_name: NonBlank
    command: tuple[NonBlank, ...] = Field(min_length=1)
    target_tests: tuple[NonBlank, ...] = Field(min_length=1)
    pythonpath_relative: NonBlank | None = None
    # Absolute interpreter path supplied by the trusted caller. The harness refuses a
    # relative executable (and requires `.exe` on Windows), and an Agent must never be
    # able to choose the interpreter, so this stays part of the trusted config.
    python_executable: NonBlank | None = None
    # Absolute directory holding the public test files when they live outside the
    # checkout (a task pack). The worker copies them into the run's artifact directory
    # and points the command there, so the checkout itself stays untouched.
    public_tests_root: NonBlank | None = None
    external_connections: Literal["unknown", "none", "required"] = "unknown"
    independent_suite_version: NonBlank | None = None
    # Trusted task-pack scope for optional diagnostic Skill; not a formal pass condition.
    scope: ScopeSpec | None = None

    @field_validator("command_name", "pythonpath_relative", "independent_suite_version",
                     "public_tests_root", "python_executable")
    @classmethod
    def nonblank_field(cls, value):
        if value is not None and not value.strip():
            raise ValueError("Value must not be blank")
        return value

    @model_validator(mode="after")
    def absolute_interpreter_only(self):
        executable = self.python_executable
        if executable is not None and not (
                Path(executable).is_absolute() or PureWindowsPath(executable).is_absolute()):
            raise ValueError("python_executable must be an absolute path")
        return self

    @model_validator(mode="after")
    def unique_targets(self):
        if (any(not value.strip() for value in (*self.command, *self.target_tests))
                or len(set(self.target_tests)) != len(self.target_tests)):
            raise ValueError("Command and unique target tests must be nonblank")
        return self


def public_tests_copy_root(artifact_root: Path | str) -> Path:
    """Return the run-scoped directory the trusted public tests are copied into."""
    return Path(artifact_root) / PUBLIC_TESTS_COPY_DIRECTORY


def public_test_modules(config: VerificationConfig) -> dict[str, bytes]:
    """Read the trusted public test modules exactly as they must be executed.

    The bytes are the evidence: the command and the recorded digest are both derived
    from this one read, so a copy that drifts from the pack cannot stay consistent with
    either. An empty result means the pack keeps its tests inside the checkout.
    """
    root = config.public_tests_root
    if root is None:
        return {}
    try:
        source = Path(root).resolve(strict=True)
    except OSError:
        raise ValueError("Public tests root is unavailable") from None
    if not source.is_dir():
        raise ValueError("Public tests root must be a directory")
    modules = {path.name: path.read_bytes() for path in sorted(source.glob("*.py"))}
    if not modules:
        raise ValueError("Public tests root contains no Python test files")
    return modules


def public_tests_sha256(modules: Mapping[str, bytes]) -> dict[str, str]:
    """Digest each copied module so the executed content is pinned, not just its path."""
    return {name: sha256(data).hexdigest() for name, data in sorted(modules.items())}


def copied_public_tests_sha256(artifact_root: Path | str) -> dict[str, str]:
    """Digest the public tests that are currently materialised for one run."""
    root = public_tests_copy_root(artifact_root)
    if not root.is_dir():
        return {}
    return public_tests_sha256({path.name: path.read_bytes() for path in sorted(root.glob("*.py"))})


def public_check_command(
    config: VerificationConfig, *, artifact_root: Path | str,
    modules: Mapping[str, bytes] | None = None,
) -> tuple[str, ...]:
    """Derive the exact command the trusted config requires; writes nothing.

    Execution and formal acceptance both call this, so the command that runs and the
    command that is checked cannot be assembled two different ways.
    """
    if config.public_tests_root is None:
        return config.command
    modules = public_test_modules(config) if modules is None else modules
    target = public_tests_copy_root(artifact_root)
    rewritten = []
    replaced = False
    for part in config.command:
        name = Path(part).name
        if not part.startswith("-") and name in modules:
            rewritten.append(str(target / name))
            replaced = True
        else:
            rewritten.append(part)
    if not replaced:
        rewritten.extend(str(target / name) for name in sorted(modules))
    rewritten.extend((f"--rootdir={target}", f"--confcutdir={target}"))
    return tuple(rewritten)


def command_evidence_sha256(command: tuple[str, ...], pythonpath_relative: str | None) -> str:
    """Fingerprint one assembled command together with the import path it relies on."""
    hashed = tuple(command) if pythonpath_relative is None else {
        "command": list(command), "pythonpath": pythonpath_relative}
    return sha256(json.dumps(hashed, ensure_ascii=True, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExpectedCommandEvidence:
    """What a run must have recorded to be checkable against its trusted config."""

    command: tuple[str, ...]
    command_sha256: str
    public_tests_sha256: dict[str, str]


def expected_command_evidence(
    config: VerificationConfig, artifact_root: Path | str,
) -> ExpectedCommandEvidence:
    """Derive the command, its digest, and the public test content for one run."""
    modules = public_test_modules(config)
    command = public_check_command(config, artifact_root=artifact_root, modules=modules)
    return ExpectedCommandEvidence(
        command=command,
        command_sha256=command_evidence_sha256(command, config.pythonpath_relative),
        public_tests_sha256=public_tests_sha256(modules),
    )
