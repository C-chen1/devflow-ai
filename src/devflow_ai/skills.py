"""Small, explicitly invoked DevFlow skills; no independent tool executor or new Agent."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from threading import Event
from uuid import UUID

from sqlalchemy import select

from devflow_ai.enums import AgentRole
from devflow_ai.models import AgentExecution, Run, Task
from devflow_ai.task_pack import ScopeSpec, admission_has_scope_digest, scope_evidence_sha256
from devflow_ai.worker import RunDirectories, Worker


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    version: str
    applicable_role: str
    inputs: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    steps: tuple[str, ...]
    completion_evidence: tuple[str, ...]


REGISTERED_CHECK = SkillDefinition(
    name="run_registered_check", version="1", applicable_role="VERIFIER",
    inputs=("run_id", "verifier_execution_id", "registered_command_name"),
    allowed_tools=("run_command",),
    steps=("Require a trusted, exact registered command name", "Execute through the bound Verifier tool",
           "Return the recorded ToolCall outcome"),
    completion_evidence=("run_id", "verifier_execution_id", "tool_call_id", "tool_status", "exit_code"),
)

REGISTERED_CHECK_V2 = SkillDefinition(
    name=REGISTERED_CHECK.name, version="2", applicable_role="VERIFIER",
    inputs=(*REGISTERED_CHECK.inputs, "trusted_pytest_capture", "trusted_pythonpath_relative",
            "trusted_require_unpatched", "trusted_verify_baseline", "trusted_public_tests_digest"),
    allowed_tools=REGISTERED_CHECK.allowed_tools,
    steps=(*REGISTERED_CHECK.steps, "Return captured Artifact references when available"),
    completion_evidence=(*REGISTERED_CHECK.completion_evidence, "command_sha256", "artifacts"),
)

DIFF_SCOPE_CHECK = SkillDefinition(
    name="check_diff_scope", version="1", applicable_role="VERIFIER",
    inputs=("run_id", "verifier_execution_id", "trusted_scope", "max_diff_bytes"),
    allowed_tools=("git_diff",),
    steps=("Validate literal scope rules", "Collect and verify the recorded workspace diff",
           "Compare all reported paths with the trusted rules"),
    completion_evidence=("run_id", "verifier_execution_id", "tool_call_id", "diff_artifact_id",
                         "numstat_artifact_id", "diff_sha256", "observed_paths"),
)

DIFF_SCOPE_CHECK_V2 = SkillDefinition(
    name=DIFF_SCOPE_CHECK.name, version="2", applicable_role=DIFF_SCOPE_CHECK.applicable_role,
    inputs=DIFF_SCOPE_CHECK.inputs, allowed_tools=DIFF_SCOPE_CHECK.allowed_tools,
    steps=("Check Task admission pins this scope", *DIFF_SCOPE_CHECK.steps,
           "Recheck Task admission after collecting evidence"),
    completion_evidence=DIFF_SCOPE_CHECK.completion_evidence,
)


class SkillNotApplicable(ValueError):
    """The caller did not supply the conditions for this exact skill version."""


def _literal_scope_path(value: str, *, rule: bool) -> str:
    """Accept exact POSIX file names or slash-terminated directory prefixes only."""
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise SkillNotApplicable("Scope path is empty or too long")
    if value.startswith("/") or "\\" in value or any(c in value for c in "\0:*?[]"):
        raise SkillNotApplicable("Scope paths must be literal relative POSIX paths")
    if not rule and value.endswith("/"):
        raise SkillNotApplicable("A changed path must name a file")
    segments = (value[:-1] if value.endswith("/") else value).split("/")
    if any(part in ("", ".", "..") for part in segments):
        raise SkillNotApplicable("Scope paths must not contain empty or traversal segments")
    return value


def _matches_scope(path: str, rule: str) -> bool:
    return path.startswith(rule) if rule.endswith("/") else path == rule


def supports_diff_scope(scope: ScopeSpec, max_diff_bytes: int) -> bool:
    """Whether the optional diagnostic supports these trusted inputs."""
    if (not isinstance(scope, ScopeSpec) or not 1 <= len(scope.allowed_paths) <= 256
            or len(scope.forbidden_paths) > 256 or not 1 <= max_diff_bytes <= 5 * 1024 * 1024):
        return False
    try:
        for path in (*scope.allowed_paths, *scope.forbidden_paths):
            _literal_scope_path(path, rule=True)
    except SkillNotApplicable:
        return False
    return True


def check_diff_scope(*, version: str, worker: Worker, run_id: UUID, verifier_execution_id: UUID,
                     directories: RunDirectories, trusted_scope: ScopeSpec,
                     max_diff_bytes: int, stop_event: Event) -> dict:
    """Check recorded Git paths against a trusted literal scope, without granting permissions.

    The resulting comparison is diagnostic, not a workspace sandbox or formal acceptance.
    Git diff's existing exclusions and external-modification limitations still apply.
    """
    if version not in (DIFF_SCOPE_CHECK.version, DIFF_SCOPE_CHECK_V2.version):
        raise SkillNotApplicable("Unsupported skill version")
    if not isinstance(trusted_scope, ScopeSpec):
        raise SkillNotApplicable("A trusted task-pack ScopeSpec is required")
    if not 1 <= len(trusted_scope.allowed_paths) <= 256 or len(trusted_scope.forbidden_paths) > 256:
        raise SkillNotApplicable("Scope must have 1 to 256 allowed and at most 256 forbidden rules")
    allowed = tuple(_literal_scope_path(path, rule=True) for path in trusted_scope.allowed_paths)
    forbidden = tuple(_literal_scope_path(path, rule=True) for path in trusted_scope.forbidden_paths)
    scope_data = {"allowed_paths": list(allowed), "forbidden_paths": list(forbidden)}
    scope_sha256 = scope_evidence_sha256(trusted_scope)
    if not isinstance(max_diff_bytes, int) or isinstance(max_diff_bytes, bool) or not 1 <= max_diff_bytes <= 5 * 1024 * 1024:
        raise SkillNotApplicable("Diff size limit must be between 1 byte and 5 MiB")
    with worker.session_factory() as session:
        execution = session.scalar(select(AgentExecution.id).where(
            AgentExecution.id == verifier_execution_id, AgentExecution.run_id == run_id,
            AgentExecution.agent_role == AgentRole.VERIFIER.value.lower()))
        if execution is None:
            raise SkillNotApplicable("The execution is not a Verifier for this Run")
        if version == "2":
            admission = session.scalar(select(Task.admission_record).join(Run, Run.task_id == Task.id)
                                       .where(Run.id == run_id))
            if not admission_has_scope_digest(admission, scope_sha256):
                raise SkillNotApplicable("The Task does not pin this scope in benchmark admission")
    read_diff = worker.build_verifier_diff(run_id, agent_execution_id=verifier_execution_id,
        directories=directories, max_bytes=max_diff_bytes, stop_event=stop_event,
        diff_scope_skill_sha256=scope_sha256, diff_scope_skill_version=version)
    evidence = read_diff()
    if version == "2":
        with worker.session_factory() as session:
            admission = session.scalar(select(Task.admission_record).join(Run, Run.task_id == Task.id)
                                       .where(Run.id == run_id))
            if not admission_has_scope_digest(admission, scope_sha256):
                raise SkillNotApplicable("The Task scope binding changed during inspection")
    paths, violations = [], []
    for item in evidence["statistics"]["files"]:
        path = item["path"]
        try:
            _literal_scope_path(path, rule=False)
        except SkillNotApplicable:
            if version != "2":
                raise
            paths.append(path)
            violations.append({"path": path, "reason": "unsupported_path"})
            continue
        paths.append(path)
        if any(_matches_scope(path, rule) for rule in forbidden):
            violations.append({"path": path, "reason": "forbidden"})
        elif not any(_matches_scope(path, rule) for rule in allowed):
            violations.append({"path": path, "reason": "not_allowed"})
    return {"skill": DIFF_SCOPE_CHECK.name, "version": version, "run_id": str(run_id),
            "verifier_execution_id": str(verifier_execution_id), "tool_call_id": evidence["tool_call_id"],
            "diff_artifact_id": evidence["artifact_id"], "numstat_artifact_id": evidence["numstat_artifact_id"],
            "diff_sha256": evidence["content_hash"], "base_commit": evidence["base_commit"],
            "scope": scope_data, "scope_sha256": scope_sha256,
            "observed_paths": paths, "violations": violations,
            "result": "outside_declared_scope" if violations else "within_declared_scope"}


def run_registered_check(*, version: str, worker: Worker, run_id: UUID, verifier_execution_id: UUID,
                         directories: RunDirectories, registry: Mapping[str, tuple[str, ...]],
                         command_name: str, stop_event: Event, trusted_pytest_capture: bool = False,
                         trusted_pythonpath_relative: str | None = None,
                         trusted_require_unpatched: bool = False, trusted_verify_baseline: bool = False,
                         trusted_public_tests_digest: Mapping[str, str] | None = None) -> dict:
    """Run one already-registered command using the existing controlled Verifier path.

    The registry and paths are supplied by the trusted control plane, never by a model.
    A settled ToolCall is evidence that the step was attempted, not evidence of a successful
    test, valid test suite, independent evaluation, or formal Verification.
    """
    if version not in (REGISTERED_CHECK.version, REGISTERED_CHECK_V2.version):
        raise SkillNotApplicable("Unsupported skill version")
    if not isinstance(trusted_pytest_capture, bool):
        raise SkillNotApplicable("Pytest capture must be a trusted boolean")
    if version == "1" and (trusted_pytest_capture or trusted_pythonpath_relative is not None
                           or trusted_require_unpatched or trusted_verify_baseline
                           or trusted_public_tests_digest is not None):
        raise SkillNotApplicable("Version 1 does not support report capture or Python path")
    if not isinstance(command_name, str) or not command_name.strip() or command_name != command_name.strip():
        raise SkillNotApplicable("A registered command name is required")
    if command_name not in registry:
        raise SkillNotApplicable("The requested command is not in the trusted registry")
    with worker.session_factory() as session:
        execution = session.scalar(select(AgentExecution.id).where(
            AgentExecution.id == verifier_execution_id, AgentExecution.run_id == run_id,
            AgentExecution.agent_role == AgentRole.VERIFIER.value.lower()))
        if execution is None:
            raise SkillNotApplicable("The execution is not a Verifier for this Run")
    # This bound tool delegates to RunCoordinator.execute_verifier_command, which rechecks
    # role, Policy, lease, workspace lock, budget and records the ToolCall/Artifacts.
    command = worker.build_verifier_command(run_id, agent_execution_id=verifier_execution_id,
        directories=directories, registry=registry, stop_event=stop_event,
        pytest_command_names=frozenset({command_name}) if trusted_pytest_capture else frozenset(),
        pythonpath_relative=trusted_pythonpath_relative, registered_check_skill_version=version,
        require_unpatched=trusted_require_unpatched, verify_baseline=trusted_verify_baseline,
        public_tests_digest=trusted_public_tests_digest)
    evidence = command(command_name)
    result = {"skill": REGISTERED_CHECK.name, "version": version,
            "run_id": str(run_id), "verifier_execution_id": str(verifier_execution_id),
            "tool_call_id": evidence["tool_call_id"], "tool_status": evidence["status"],
            "exit_code": evidence["exit_code"], "reason": evidence["reason"]}
    if version == "2":
        result["command_sha256"] = evidence["args"].get("command_sha256")
        result["artifacts"] = evidence["artifacts"]
        result["receipt"] = evidence
    return result
