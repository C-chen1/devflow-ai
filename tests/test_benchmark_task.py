import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from devflow_ai.benchmark_task import (
    BenchmarkTaskError, check_snapshot_supports_run, create_benchmark_task, main,
    load_policy_snapshot,
)
from devflow_ai.enums import AgentRole, TaskStatus

REPO_ROOT = Path(__file__).resolve().parents[1]
from devflow_ai.models import Task
from devflow_ai.policy import build_default_policy
from devflow_ai.task_pack import load_manifest


def _pack(tmp_path):
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir(exist_ok=True)
    manifest = {
        "pack_id": "demo-pack", "schema_version": 1, "title": "Demo pack",
        "source": {"issue_url": "https://example.invalid/issues/1",
                   "repository_url": "https://example.invalid/repo",
                   "license": "MIT", "issue_reference": "demo#1"},
        "base_commit": "a" * 40,
        "environment": {"python_requires": ">=3.11", "dependencies": ["pytest==9.1.1"],
                        "platform_notes": "Linux"},
        "issue": {"sanitized_problem": "A sanitized problem statement.",
                  "observable_error": "An observable wrong output.",
                  "expected_behavior": "The expected behaviour."},
        "scope": {"allowed_paths": ["src/"]},
        "public_checks": {"command": ["python", "-m", "pytest"], "command_name": "pytest",
                          "target_tests": ["tests/test_demo.py::test_target"]},
        "baseline": {"status": "verified_here"},
        "external_connections": "none",
        "policy": {"policy_version": "demo-v1", "model_id": "demo-model"},
        "independent_suite": {"suite_version": "suite-1", "materials_sha256": "b" * 64,
                              "digest_scope": "bytes of the suite archive as delivered",
                              "digest_method": "sha256 over those bytes",
                              "provided_by": "trusted_control_plane",
                              "declaration": "Provided by the trusted control plane."},
    }
    (pack_dir / "task.json").write_text(json.dumps(manifest), encoding="utf-8")
    return pack_dir


def _policy(**overrides):
    policy = build_default_policy()
    policy.permissions.allowed_tools_by_role[AgentRole.EXPLORER] = ["read_file"]
    policy.permissions.allowed_tools_by_role[AgentRole.CODER] = ["apply_patch"]
    policy.permissions.allowed_tools_by_role[AgentRole.VERIFIER] = ["run_command", "git_diff"]
    policy.budget.max_run_duration_seconds = 1200
    policy.budget.max_llm_tokens_per_run = 80_000
    policy.budget.max_graph_steps_per_run = 8
    policy.budget.max_retries_per_task = 0
    policy.budget.max_rediagnose_attempts = 0
    for key, value in overrides.items():
        setattr(policy.budget, key, value)
    return policy


def test_a_snapshot_without_the_role_tools_is_refused():
    policy = _policy()
    policy.permissions.allowed_tools_by_role[AgentRole.CODER] = []

    problems = check_snapshot_supports_run(
        policy, max_duration_seconds=1200, max_model_tokens=80_000)

    assert problems == ["coder is not granted apply_patch"]


def test_a_snapshot_that_cannot_carry_the_limits_is_refused():
    narrow = _policy(max_run_duration_seconds=600)
    wide = _policy(max_llm_tokens_per_run=200_000)

    assert any("max_run_duration_seconds" in item for item in check_snapshot_supports_run(
        narrow, max_duration_seconds=1200, max_model_tokens=80_000))
    assert any("max_llm_tokens_per_run" in item for item in check_snapshot_supports_run(
        wide, max_duration_seconds=1200, max_model_tokens=80_000))


def test_a_snapshot_that_authorises_retries_is_refused():
    policy = _policy(max_retries_per_task=3)

    problems = check_snapshot_supports_run(
        policy, max_duration_seconds=1200, max_model_tokens=80_000)

    assert problems == [
        "max_retries_per_task 3 is not 0; a single-attempt benchmark must not authorise "
        "extra Runs"]


def test_a_snapshot_that_authorises_a_rediagnosis_is_refused():
    policy = _policy(max_rediagnose_attempts=2)

    problems = check_snapshot_supports_run(
        policy, max_duration_seconds=1200, max_model_tokens=80_000)

    assert problems == [
        "max_rediagnose_attempts 2 is not 0; a rediagnosis Run is also an extra Run "
        "under a single-attempt benchmark"]


def test_a_complete_snapshot_passes():
    assert check_snapshot_supports_run(
        _policy(), max_duration_seconds=1200, max_model_tokens=80_000) == []


def test_the_task_is_created_and_admitted_at_the_pack_commit(run_session_factory, tmp_path):
    factory = run_session_factory
    manifest = load_manifest(_pack(tmp_path))
    policy = _policy()

    task_id = create_benchmark_task(
        factory, manifest, repo="example-repo", policy=policy, operator="operator-a")

    with factory() as session:
        task = session.get(Task, task_id)
        assert task.status == TaskStatus.ELIGIBLE
        assert task.base_commit == manifest.base_commit
        assert task.policy_snapshot["policy_version"] == policy.policy_version
        roles = task.policy_snapshot["permissions"]["allowed_tools_by_role"]
        coder_tools = next(value for key, value in roles.items() if key.lower() == "coder")
        assert coder_tools == ["apply_patch"]


def test_creating_a_task_needs_a_repo_and_an_operator(run_session_factory, tmp_path):
    manifest = load_manifest(_pack(tmp_path))
    for overrides in ({"repo": " "}, {"operator": ""}):
        arguments = dict(repo="example-repo", policy=_policy(), operator="operator-a")
        arguments.update(overrides)
        with pytest.raises(BenchmarkTaskError):
            create_benchmark_task(run_session_factory, manifest, **arguments)


def test_the_cli_reports_a_usable_snapshot(run_session_factory, tmp_path, capsys):
    pack_dir = _pack(tmp_path)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(_policy().model_dump(mode="json")), encoding="utf-8")

    exit_code = main(["--pack", str(pack_dir), "--policy", str(policy_path),
                      "--repo", "example-repo", "--operator", "operator-a"],
                     session_factory=run_session_factory)

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "benchmark_task_created"
    assert payload["base_commit"] == "a" * 40


def test_the_cli_refuses_a_snapshot_that_cannot_carry_the_run(tmp_path, capsys):
    pack_dir = _pack(tmp_path)
    policy = _policy(max_llm_tokens_per_run=200_000)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy.model_dump(mode="json")), encoding="utf-8")

    exit_code = main(["--pack", str(pack_dir), "--policy", str(policy_path),
                      "--repo", "example-repo", "--operator", "operator-a"])

    assert exit_code == 2
    assert "cannot carry this run" in capsys.readouterr().err


def test_the_committed_policy_draft_parses_and_meets_the_structural_check():
    """Pins the draft's content so a review is about these exact values, not a moving file.

    Passing this is not approval of the content, and not evidence that a real run works.
    """
    path = (Path(__file__).resolve().parents[1] / "benchmarks" / "click_3019_public"
            / "policy.draft.json")

    policy = load_policy_snapshot(path)

    assert policy.policy_version == "benchmark-click3019-v1"
    # Budget: exactly one Run, and the pinned first-attempt limits.
    assert policy.budget.max_retries_per_task == 0
    assert policy.budget.max_rediagnose_attempts == 0
    assert policy.budget.max_run_duration_seconds == 1200
    assert policy.budget.max_llm_tokens_per_run == 80_000
    assert policy.budget.max_graph_steps_per_run == 30
    assert policy.budget.max_tool_calls_per_run == 60
    assert policy.budget.max_output_repairs_per_agent_execution == 1
    assert policy.budget.tool_call_timeout_seconds == 300
    # Permissions: only the tools each role's code path actually uses.
    assert policy.permissions.allowed_tools_by_role[AgentRole.SUPERVISOR] == []
    assert policy.permissions.allowed_tools_by_role[AgentRole.PLANNER] == []
    assert policy.permissions.allowed_tools_by_role[AgentRole.EXPLORER] == ["read_file"]
    assert policy.permissions.allowed_tools_by_role[AgentRole.CODER] == [
        "read_file", "apply_patch"]
    assert policy.permissions.allowed_tools_by_role[AgentRole.VERIFIER] == [
        "read_file", "run_command", "git_diff"]
    assert check_snapshot_supports_run(
        policy, max_duration_seconds=1200, max_model_tokens=80_000) == []


def test_an_unreadable_snapshot_is_a_configuration_error(tmp_path):
    with pytest.raises(BenchmarkTaskError):
        load_policy_snapshot(tmp_path / "missing.json")


def test_the_second_task_policy_draft_is_a_new_version_that_carries_the_pinned_limits():
    from devflow_ai.benchmark_task import check_snapshot_supports_run, load_policy_snapshot

    draft = REPO_ROOT / "benchmarks" / "click_3084_public" / "policy.draft.json"
    snapshot = load_policy_snapshot(draft)

    # A per-task version, not the Click #3019 one: the two tasks are separate samples.
    assert snapshot.policy_version == "benchmark-click3084-v1"
    assert "click3019" not in snapshot.policy_version
    assert check_snapshot_supports_run(
        snapshot, max_duration_seconds=1200, max_model_tokens=80_000) == []
    assert snapshot.budget.max_llm_tokens_per_run == 80_000
    assert snapshot.budget.max_run_duration_seconds == 1200


def test_the_second_task_pack_creates_an_admitted_task_at_its_commit(run_session_factory):
    """The real pack and its draft snapshot go through the production entry, not a fixture."""
    factory = run_session_factory
    manifest = load_manifest(REPO_ROOT / "benchmarks" / "click_3084_public")
    policy = load_policy_snapshot(
        REPO_ROOT / "benchmarks" / "click_3084_public" / "policy.draft.json")

    task_id = create_benchmark_task(
        factory, manifest, repo="example-repo", policy=policy, operator="operator-a")

    with factory() as session:
        task = session.get(Task, task_id)
        assert task.status == TaskStatus.ELIGIBLE
        assert task.base_commit == "7f7bbe4569ea68e8dabee232eade069ef3310aea"
        assert task.repo == "example-repo"
        snapshot = task.policy_snapshot
        assert snapshot["policy_version"] == "benchmark-click3084-v1"
        assert snapshot["budget"]["max_llm_tokens_per_run"] == 80_000
        assert snapshot["budget"]["max_run_duration_seconds"] == 1200
        assert snapshot["budget"]["max_retries_per_task"] == 0
        roles = {key.lower(): value for key, value in
                 snapshot["permissions"]["allowed_tools_by_role"].items()}
        assert roles["verifier"] == ["read_file", "run_command", "git_diff"]
        assert roles["coder"] == ["read_file", "apply_patch"]
