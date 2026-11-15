"""Create and admit the Task a benchmark pack describes.

This command turns a validated pack plus a caller-supplied Policy snapshot into one
ELIGIBLE Task pinned to the pack's base commit. It never invents a Policy: permissions
and budgets come from the snapshot file, and the snapshot is checked to be able to
carry the pinned run limits before anything is created. A missing tool permission or a
budget that cannot cover the run is a configuration error, not something to patch at
runtime.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import ValidationError

from devflow_ai.enums import AgentRole, TaskStatus
from devflow_ai.policy import PolicyConfig
from devflow_ai.services import AdmissionRecord, TaskService
from devflow_ai.task_pack import TaskPackManifest, load_manifest

DEFAULT_MAX_DURATION_SECONDS = 1200
DEFAULT_MAX_MODEL_TOKENS = 80_000

# Each role needs at least these tools for one benchmark attempt to be executable.
REQUIRED_TOOLS: dict[AgentRole, tuple[str, ...]] = {
    AgentRole.EXPLORER: ("read_file",),
    AgentRole.CODER: ("apply_patch",),
    AgentRole.VERIFIER: ("run_command", "git_diff"),
}
MINIMUM_GRAPH_STEPS = 4

__all__ = [
    "BenchmarkTaskError", "REQUIRED_TOOLS", "check_snapshot_supports_run",
    "create_benchmark_task", "load_policy_snapshot", "main",
]


class BenchmarkTaskError(RuntimeError):
    """The Task cannot be created from this pack and snapshot."""


def load_policy_snapshot(path: Path) -> PolicyConfig:
    """Read one Policy snapshot; the caller owns its content and review."""
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BenchmarkTaskError(f"Policy snapshot is unreadable: {type(error).__name__}") from None
    try:
        return PolicyConfig.model_validate(payload)
    except ValidationError as error:
        raise BenchmarkTaskError(
            "Policy snapshot is not a valid PolicyConfig") from None


def check_snapshot_supports_run(
    policy: PolicyConfig, *, max_duration_seconds: int, max_model_tokens: int,
) -> list[str]:
    """Return what this snapshot cannot carry; an empty list means it can.

    A first-attempt benchmark runs exactly one Run, so the snapshot itself must not
    authorise retries: relying on the entry to abstain would leave the budget as a
    standing permission for some other caller to create another Run.
    """
    problems = []
    if policy.budget.max_retries_per_task != 0:
        problems.append(
            f"max_retries_per_task {policy.budget.max_retries_per_task} is not 0; a "
            f"single-attempt benchmark must not authorise extra Runs")
    if policy.budget.max_rediagnose_attempts != 0:
        problems.append(
            f"max_rediagnose_attempts {policy.budget.max_rediagnose_attempts} is not 0; a "
            f"rediagnosis Run is also an extra Run under a single-attempt benchmark")
    if policy.budget.max_run_duration_seconds < max_duration_seconds:
        problems.append(
            f"max_run_duration_seconds {policy.budget.max_run_duration_seconds} is below "
            f"the pinned {max_duration_seconds}")
    if policy.budget.max_llm_tokens_per_run > max_model_tokens:
        problems.append(
            f"max_llm_tokens_per_run {policy.budget.max_llm_tokens_per_run} exceeds the "
            f"pinned {max_model_tokens}")
    if policy.budget.max_graph_steps_per_run < MINIMUM_GRAPH_STEPS:
        problems.append(
            f"max_graph_steps_per_run {policy.budget.max_graph_steps_per_run} is below "
            f"{MINIMUM_GRAPH_STEPS}")
    for role, tools in REQUIRED_TOOLS.items():
        granted = set(policy.permissions.allowed_tools_by_role.get(role, ()))
        for tool in tools:
            if tool not in granted:
                problems.append(f"{role.value.lower()} is not granted {tool}")
    return problems


def create_benchmark_task(
    session_factory, manifest: TaskPackManifest, *, repo: str, policy: PolicyConfig,
    operator: str, title: str | None = None,
) -> UUID:
    """Create the pack's Task and admit it at the pack's base commit."""
    if not isinstance(repo, str) or not repo.strip():
        raise BenchmarkTaskError("repo must be a non-blank clone source")
    if not isinstance(operator, str) or not operator.strip():
        raise BenchmarkTaskError("operator must be a non-blank name")
    with session_factory() as session:
        task, _ = TaskService(session, policy_factory=lambda: policy).create_task(
            title=title or manifest.title, description=manifest.issue.sanitized_problem,
            repo=repo, idempotency_key=uuid4())
        task_id = task.id
    with session_factory() as session:
        TaskService(session).apply_admission_decision(
            task_id, TaskStatus.ELIGIBLE, base_commit=manifest.base_commit,
            admission_record=AdmissionRecord(
                reason=f"benchmark pack {manifest.pack_id} admitted by {operator}",
                evidence=[f"pack:{manifest.pack_id}", "policy_snapshot:reviewed"]))
    return task_id


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    parser = argparse.ArgumentParser(
        description="Create one benchmark Task from a pack and a Policy snapshot; no Run starts.")
    parser.add_argument("--pack", required=True, help="Task-pack directory containing task.json.")
    parser.add_argument("--policy", required=True, help="Policy snapshot JSON file.")
    parser.add_argument("--repo", required=True, help="Clone source for the Task.")
    parser.add_argument("--operator", required=True, help="Who prepared this Task.")
    parser.add_argument("--title", help="Override the Task title.")
    parser.add_argument("--max-duration-seconds", type=int,
                        default=DEFAULT_MAX_DURATION_SECONDS)
    parser.add_argument("--max-model-tokens", type=int, default=DEFAULT_MAX_MODEL_TOKENS)
    args = parser.parse_args(argv)

    try:
        manifest = load_manifest(Path(args.pack))
        policy = load_policy_snapshot(Path(args.policy))
    except (ValueError, BenchmarkTaskError) as error:
        print(json.dumps({"event": "benchmark_task_configuration_error",
                          "detail": str(error)}), file=sys.stderr)
        return 2
    problems = check_snapshot_supports_run(
        policy, max_duration_seconds=args.max_duration_seconds,
        max_model_tokens=args.max_model_tokens)
    if problems:
        print(json.dumps({"event": "benchmark_task_configuration_error",
                          "detail": "Policy snapshot cannot carry this run",
                          "problems": problems}), file=sys.stderr)
        return 2

    engine = None
    try:
        if session_factory is None:
            from devflow_ai.database import create_db_engine, create_session_factory

            engine = create_db_engine()
            session_factory = create_session_factory(engine)
        task_id = create_benchmark_task(
            session_factory, manifest, repo=args.repo, policy=policy, operator=args.operator,
            title=args.title)
    except Exception as error:
        print(json.dumps({"event": "benchmark_task_failed",
                          "error_type": type(error).__name__,
                          "detail": "Task creation failed"}), file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()

    print(json.dumps({"event": "benchmark_task_created", "task_id": str(task_id),
                      "pack_id": manifest.pack_id, "base_commit": manifest.base_commit,
                      "operator": args.operator}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
