"""Task-pack manifest: structure and completeness checks for benchmark packs.

A manifest describes one benchmark task closely enough that a run is reproducible and
its claims are auditable. Validation here proves the format and the checkable fields
only; it never proves that the task, its environment or its hidden material are
trustworthy — that stays with the trusted control plane. ``extra="forbid"`` rejects
fields the schema does not define, but it cannot stop someone from pasting hidden
content into a free-text field such as ``notes``: isolation comes from the trusted
control plane holding and injecting that material, not from this schema.

Hidden material must never appear in a manifest. Only its suite version, an explicit
digest with a stated byte scope and calculation method, and a declaration that the
trusted control plane provides it are allowed.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import sys
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

MANIFEST_FILENAME = "task.json"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
COMMIT_PATTERN = r"^[0-9a-f]{40}$"


class BaselineStatus(StrEnum):
    VERIFIED_HERE = "verified_here"
    RECORDED_ELSEWHERE = "recorded_elsewhere"
    NOT_REVERIFIED = "not_reverified"


class SourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issue_url: str = Field(pattern=r"^https://")
    repository_url: str = Field(pattern=r"^https://")
    license: str = Field(min_length=1)
    issue_reference: str = Field(min_length=1)


class EnvironmentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    python_requires: str = Field(min_length=1)
    dependencies: tuple[str, ...] = Field(min_length=1)
    platform_notes: str = Field(min_length=1)


class IssueSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sanitized_problem: str = Field(min_length=1)
    observable_error: str = Field(min_length=1)
    expected_behavior: str = Field(min_length=1)


class ScopeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_paths: tuple[str, ...] = Field(min_length=1)
    forbidden_paths: tuple[str, ...] = ()


def scope_evidence_sha256(scope: ScopeSpec) -> str:
    """Digest the explicit scope as encoded in trusted admission evidence, version 1."""
    payload = {"allowed_paths": list(scope.allowed_paths), "forbidden_paths": list(scope.forbidden_paths)}
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")).hexdigest()


def admission_has_scope_digest(admission: object, digest: str) -> bool:
    """Verify an exact single pack marker and scope declaration; never infer missing values."""
    if not isinstance(admission, dict) or not isinstance(admission.get("evidence"), list):
        return False
    evidence = admission["evidence"]
    packs = [item for item in evidence if isinstance(item, str) and item.startswith("pack:")]
    scopes = [item for item in evidence if isinstance(item, str) and item.startswith("scope-sha256-v1:")]
    return len(packs) == 1 and len(packs[0]) > len("pack:") and scopes == ["scope-sha256-v1:" + digest]


def optional_scope_binding_matches(admission: object, manifest: "TaskPackManifest") -> bool:
    """Compare pinned scope and pack when present; unmarked legacy Tasks remain compatible.

    Absence is not evidence of authenticity: an old Task and a stripped marker cannot be
    distinguished by this legacy-compatible format.
    """
    if not isinstance(admission, dict) or not isinstance(admission.get("evidence"), list):
        return True
    evidence = admission["evidence"]
    scopes = [item for item in evidence if isinstance(item, str) and item.startswith("scope-sha256-v1:")]
    if not scopes:
        return True
    packs = [item for item in evidence if isinstance(item, str) and item.startswith("pack:")]
    return (packs == [f"pack:{manifest.pack_id}"] and
            scopes == [f"scope-sha256-v1:{scope_evidence_sha256(manifest.scope)}"])


class PublicChecks(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command: tuple[str, ...] = Field(min_length=1)
    command_name: str = Field(min_length=1)
    target_tests: tuple[str, ...] = Field(min_length=1)
    # Relative to the pinned checkout when the tests must import the checkout itself
    # rather than whatever version happens to be installed.
    pythonpath_relative: str | None = None
    notes: str = ""


class BaselineRecord(BaseModel):
    """The public baseline as last observed, labelled with how it was observed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: BaselineStatus
    recorded_at: str | None = None
    environment_note: str = ""
    failed_targets: int | None = Field(default=None, ge=0)
    passed_controls: int | None = Field(default=None, ge=0)
    notes: str = ""


class PolicyReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: str = Field(min_length=1)
    model_id: str = Field(min_length=1)


class IndependentSuiteRef(BaseModel):
    """Identity of hidden material held outside this repository.

    Only identity and digest are allowed. The manifest must never carry the material
    itself, expected outputs, reference patches or commits, nor any path or URL that
    would let a repair Agent reach them. A digest and a declaration identify material;
    they do not by themselves prove its origin or the isolation it runs under.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    suite_version: str = Field(min_length=1)
    materials_sha256: str = Field(pattern=SHA256_PATTERN)
    digest_scope: str = Field(min_length=1)
    digest_method: str = Field(min_length=1)
    provided_by: str = Field(pattern=r"^trusted_control_plane$")
    declaration: str = Field(min_length=1)


class UnverifiedItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class TaskPackManifest(BaseModel):
    """One benchmark task pack. Unknown keys are rejected so hidden material cannot be smuggled in."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pack_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    schema_version: int = Field(ge=1, le=1)
    title: str = Field(min_length=1)
    source: SourceSpec
    base_commit: str = Field(pattern=COMMIT_PATTERN)
    environment: EnvironmentSpec
    issue: IssueSpec
    scope: ScopeSpec
    public_checks: PublicChecks
    baseline: BaselineRecord
    external_connections: Literal["unknown", "none", "required"] = "unknown"
    policy: PolicyReference | None = None
    independent_suite: IndependentSuiteRef | None = None
    unverified: tuple[UnverifiedItem, ...] = ()

    @model_validator(mode="after")
    def require_a_state_for_every_gap(self) -> "TaskPackManifest":
        """A gap must be declared, not left to the reader's assumptions."""
        declared = {item.field for item in self.unverified}
        missing = []
        if self.policy is None and not any(name.startswith("policy") for name in declared):
            missing.append("policy")
        if self.independent_suite is None and not any(
                name.startswith("independent_suite") for name in declared):
            missing.append("independent_suite")
        if self.baseline.status != BaselineStatus.VERIFIED_HERE and not any(
                name.startswith("baseline") for name in declared):
            missing.append("baseline")
        if self.external_connections == "unknown" and not any(
                name.startswith("external_connections") for name in declared):
            missing.append("external_connections")
        if missing:
            raise ValueError(
                "An unverified entry is required for: " + ", ".join(sorted(missing)))
        return self


def to_verification_config(
    manifest: TaskPackManifest, *, task_id, public_tests_root: Path | str | None = None,
    python_executable: str | None = None,
) -> "VerificationConfig":
    """Build the trusted verification input a run needs from the manifest.

    The manifest is the single source for the pinned commit, the registered command
    and the target tests, so a run cannot silently disagree with the pack it claims to
    exercise. External connections and the suite version are carried over only as the
    manifest declares them: an unverified pack yields ``unknown`` and therefore cannot
    pass the formal-verification gate, which is the intended outcome rather than a
    config to be filled in by guesswork. The hidden material itself is never part of
    this configuration.
    """
    from devflow_ai.verification_config import VerificationConfig

    # The interpreter is fixed by the trusted caller, never by the pack or an Agent: the
    # harness refuses a relative executable and an Agent must not pick one.
    command = (manifest.public_checks.command if python_executable is None
               else (python_executable, *manifest.public_checks.command[1:]))
    return VerificationConfig(
        task_id=task_id,
        base_commit=manifest.base_commit,
        scope=manifest.scope,
        command_name=manifest.public_checks.command_name,
        command=command,
        python_executable=python_executable,
        target_tests=manifest.public_checks.target_tests,
        pythonpath_relative=manifest.public_checks.pythonpath_relative,
        external_connections=manifest.external_connections,
        public_tests_root=(None if public_tests_root is None else str(Path(public_tests_root))),
        independent_suite_version=(None if manifest.independent_suite is None
                                   else manifest.independent_suite.suite_version),
    )


def load_manifest(path: Path) -> TaskPackManifest:
    """Load and validate one manifest; these checks prove format, not trust."""
    path = Path(path)
    if path.is_dir():
        path = path / MANIFEST_FILENAME
    if not path.is_file():
        raise ValueError(f"Manifest not found: {path.name}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Manifest is not valid JSON: {error.msg}") from None
    return TaskPackManifest.model_validate(payload)


def validate_manifest_file(path: Path) -> dict:
    """Return a JSON-ready report; ``valid`` means the format and fields check out."""
    path = Path(path)
    try:
        manifest = load_manifest(path)
    except ValidationError as error:
        return {"valid": False, "path": path.name, "errors": [
            {"loc": list(item["loc"]), "type": item["type"]} for item in error.errors()]}
    except ValueError as error:
        return {"valid": False, "path": path.name, "errors": [{"detail": str(error)}]}
    return {
        "valid": True,
        "path": path.name,
        "pack_id": manifest.pack_id,
        "base_commit": manifest.base_commit,
        "baseline_status": manifest.baseline.status.value,
        "independent_suite": (None if manifest.independent_suite is None
                              else manifest.independent_suite.suite_version),
        "unverified": [{"field": item.field, "reason": item.reason}
                       for item in manifest.unverified],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate one benchmark task-pack manifest; no task is executed.")
    parser.add_argument("paths", nargs="+", help="Manifest file or pack directory.")
    args = parser.parse_args(argv)
    reports = [validate_manifest_file(Path(item)) for item in args.paths]
    for report in reports:
        print(json.dumps(report))
    return 0 if all(item["valid"] for item in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
