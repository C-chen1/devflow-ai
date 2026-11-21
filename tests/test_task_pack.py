import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from devflow_ai.task_pack import TaskPackManifest, load_manifest, main, validate_manifest_file

REPO_ROOT = Path(__file__).resolve().parents[1]
CLICK_PACK = REPO_ROOT / "benchmarks" / "click_3019_public"


def _manifest(**overrides):
    payload = {
        "pack_id": "demo-pack",
        "schema_version": 1,
        "title": "Demo pack",
        "source": {
            "issue_url": "https://example.invalid/issues/1",
            "repository_url": "https://example.invalid/repo",
            "license": "MIT",
            "issue_reference": "demo#1",
        },
        "base_commit": "a" * 40,
        "environment": {
            "python_requires": ">=3.11",
            "dependencies": ["pytest==9.1.1"],
            "platform_notes": "Linux",
        },
        "issue": {
            "sanitized_problem": "A sanitized problem statement.",
            "observable_error": "An observable wrong output.",
            "expected_behavior": "The expected behaviour.",
        },
        "scope": {"allowed_paths": ["src/"]},
        "public_checks": {
            "command": ["python", "-m", "pytest"],
            "command_name": "pytest",
            "target_tests": ["tests/test_demo.py::test_target"],
            "pythonpath_relative": "src",
        },
        "baseline": {"status": "verified_here"},
        "external_connections": "none",
        "policy": {"policy_version": "demo-v1", "model_id": "demo-model"},
        "independent_suite": {
            "suite_version": "suite-1",
            "materials_sha256": "b" * 64,
            "digest_scope": "bytes of the suite archive as delivered",
            "digest_method": "sha256 over those bytes",
            "provided_by": "trusted_control_plane",
            "declaration": "Material is provided by the trusted control plane.",
        },
    }
    payload.update(overrides)
    return payload


def test_a_complete_manifest_validates():
    manifest = TaskPackManifest.model_validate(_manifest())

    assert manifest.pack_id == "demo-pack"
    assert manifest.unverified == ()


@pytest.mark.parametrize("extra", [{"hidden_cases": []}, {"reference_patch": "diff"}])
def test_unknown_keys_are_rejected_so_material_cannot_be_smuggled_in(extra):
    with pytest.raises(ValidationError):
        TaskPackManifest.model_validate(_manifest(**extra))


def test_hidden_material_inside_the_suite_reference_is_rejected():
    suite = dict(_manifest()["independent_suite"], cases=[{"input": "x", "expected": "y"}])

    with pytest.raises(ValidationError):
        TaskPackManifest.model_validate(_manifest(independent_suite=suite))


def test_a_gap_must_be_declared_instead_of_left_implicit():
    with pytest.raises(ValidationError) as error:
        TaskPackManifest.model_validate(_manifest(
            policy=None, independent_suite=None,
            baseline={"status": "recorded_elsewhere"}, unverified=[]))

    detail = str(error.value)
    for field in ("policy", "independent_suite", "baseline"):
        assert field in detail


def test_a_declared_gap_is_accepted():
    manifest = TaskPackManifest.model_validate(_manifest(
        policy=None, independent_suite=None,
        baseline={"status": "not_reverified"},
        unverified=[
            {"field": "policy", "reason": "not pinned"},
            {"field": "independent_suite", "reason": "held elsewhere"},
            {"field": "baseline", "reason": "not re-run"},
        ]))

    assert [item.field for item in manifest.unverified] == [
        "policy", "independent_suite", "baseline"]


def test_the_digest_and_commit_must_be_exact():
    with pytest.raises(ValidationError):
        TaskPackManifest.model_validate(_manifest(
            independent_suite=dict(_manifest()["independent_suite"], materials_sha256="abc")))
    with pytest.raises(ValidationError):
        TaskPackManifest.model_validate(_manifest(base_commit="abc1234"))


def test_the_click_pack_manifest_is_valid_and_states_its_gaps():
    report = validate_manifest_file(CLICK_PACK)

    assert report["valid"] is True
    assert report["pack_id"] == "click-3019"
    assert report["base_commit"] == "00fadb8904387158ce6e9aa1573be770446895c1"
    # The public baseline was re-observed automatically on 2026-11-12 (preflight), which is
    # what "verified_here" means; it still is not a Run-persisted baseline.
    assert report["baseline_status"] == "verified_here"
    # The trusted side pinned the suite, candidate policy, and verification dependency declaration.
    assert report["independent_suite"] == "click3019-private-v1"
    manifest = load_manifest(CLICK_PACK)
    assert manifest.independent_suite.materials_sha256 == (
        "c108f73a2222591e15202ff14c46fb6702412bd1371e788a6e3d3962c0c62f40")
    assert manifest.independent_suite.provided_by == "trusted_control_plane"
    fields = {item["field"] for item in report["unverified"]}
    assert manifest.policy.policy_version == "benchmark-click3019-v1"
    assert manifest.policy.model_id == "deepseek-flash"
    assert manifest.external_connections == "none"
    assert not ({"policy", "external_connections", "independent_suite", "baseline"} & fields)
    for item in report["unverified"]:
        assert item["reason"].strip()


def test_the_click_pack_target_tests_use_the_recorded_report_naming():
    """Declared targets must match how the JUnit parser names results.

    ``parse_pytest_junit_report`` records ``f"{classname}::{name}"`` and pytest writes
    ``classname`` as the module stem, so a pytest node id such as
    ``scenarios.py::test_prompt_suffix`` can never match a recorded result. A wrong
    declaration would only surface as ``target_test_result unavailable`` after a full
    benchmark run, so it is checked here instead.
    """
    manifest = load_manifest(CLICK_PACK)

    targets = manifest.public_checks.target_tests
    assert targets == (
        "scenarios::test_prompt_suffix[empty-suffix]",
        "scenarios::test_confirm_suffix[empty-suffix]",
    )
    assert all(".py::" not in target for target in targets)


def test_validation_reports_an_invalid_manifest_without_raising(tmp_path):
    broken = tmp_path / "task.json"
    broken.write_text(json.dumps(_manifest(base_commit="not-a-hash")), encoding="utf-8")

    report = validate_manifest_file(broken)

    assert report["valid"] is False
    assert report["errors"]


def test_the_manifest_builds_the_verification_config_a_run_needs():
    from uuid import uuid4

    from devflow_ai.task_pack import to_verification_config

    manifest = TaskPackManifest.model_validate(_manifest())
    task_id = uuid4()

    config = to_verification_config(manifest, task_id=task_id)

    assert config.task_id == task_id
    assert config.base_commit == "a" * 40
    assert config.command_name == "pytest"
    assert config.command == ("python", "-m", "pytest")
    assert config.target_tests == ("tests/test_demo.py::test_target",)
    # The checkout must be imported, not whatever version is installed.
    assert config.pythonpath_relative == "src"
    assert config.external_connections == "none"
    assert config.independent_suite_version == "suite-1"
    assert config.scope == manifest.scope


def test_no_suite_version_is_invented_when_the_pack_has_none():
    from uuid import uuid4

    from devflow_ai.task_pack import to_verification_config

    manifest = TaskPackManifest.model_validate(_manifest(
        independent_suite=None,
        unverified=[{"field": "independent_suite", "reason": "held by the control plane"}]))

    config = to_verification_config(manifest, task_id=uuid4())

    assert config.independent_suite_version is None


def test_an_unverified_connection_status_must_be_declared():
    payload = _manifest()
    payload.pop("external_connections")

    with pytest.raises(ValidationError) as error:
        TaskPackManifest.model_validate(payload)
    assert "external_connections" in str(error.value)

    declared = dict(payload, unverified=[
        {"field": "external_connections", "reason": "no trusted verifier has declared it"}])
    manifest = TaskPackManifest.model_validate(declared)

    assert manifest.external_connections == "unknown"


def test_the_connection_status_is_carried_into_the_verification_config():
    from uuid import uuid4

    from devflow_ai.task_pack import to_verification_config

    manifest = TaskPackManifest.model_validate(_manifest())

    config = to_verification_config(manifest, task_id=uuid4())

    assert config.external_connections == "none"


def test_a_pack_without_a_declared_status_cannot_pass_the_formal_gate():
    from uuid import uuid4

    from devflow_ai.task_pack import to_verification_config

    payload = _manifest()
    payload.pop("external_connections")
    payload["unverified"] = [
        {"field": "external_connections", "reason": "not verified by a trusted party"}]
    manifest = TaskPackManifest.model_validate(payload)

    config = to_verification_config(manifest, task_id=uuid4())

    # "unknown" is what the formal-verification gate refuses; guessing "none" would
    # have silently manufactured that precondition.
    assert config.external_connections == "unknown"


def test_cli_exit_code_reflects_validation(tmp_path, capsys):
    broken = tmp_path / "task.json"
    broken.write_text("{not json", encoding="utf-8")

    assert main([str(CLICK_PACK)]) == 0
    assert main([str(broken)]) == 1
    emitted = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [item["valid"] for item in emitted] == [True, False]


def test_the_second_task_pack_is_valid_and_states_what_is_unverified():
    pack = REPO_ROOT / "benchmarks" / "click_3084_public"
    report = validate_manifest_file(pack)
    manifest = load_manifest(pack)

    assert report["valid"] is True
    assert report["pack_id"] == "click-3084"
    # A full 40-character commit, taken from the stable branch the fix was merged onto.
    assert manifest.base_commit == "7f7bbe4569ea68e8dabee232eade069ef3310aea"
    assert len(manifest.base_commit) == 40
    assert manifest.baseline.status.value == "verified_here"
    assert (manifest.baseline.failed_targets, manifest.baseline.passed_controls) == (1, 2)
    fields = {item.field for item in manifest.unverified}
    # This suite was independently calibrated for this task; no private contents live here.
    assert manifest.independent_suite.suite_version == "click3084-private-v1"
    assert manifest.independent_suite.materials_sha256 == "a25ed3a4a82e3a3b453e45163f7f96408eebb7289c336dbe48e21c1cbc924d97"
    assert manifest.policy.policy_version == "benchmark-click3084-v1"
    assert manifest.policy.model_id == "deepseek-flash"
    assert manifest.external_connections == "none"
    assert not ({"policy", "independent_suite", "external_connections"} & fields)
    # The formal candidate environment is the one Codex verified on Windows.
    assert manifest.environment.dependencies == ("pytest==9.1.1", "colorama==0.4.6")
    assert "Windows" in manifest.environment.platform_notes
    assert "3.11.15" in manifest.baseline.environment_note
    # The third-party checkout source is a limitation that stays declared.
    assert "base_commit" in fields


def test_the_second_task_target_uses_the_recorded_report_naming():
    manifest = load_manifest(REPO_ROOT / "benchmarks" / "click_3084_public")

    targets = manifest.public_checks.target_tests
    assert targets == ("scenarios::test_flag_value_used_when_the_flag_is_given_alone",)
    assert all(".py::" not in target for target in targets)


def test_the_second_task_public_scenarios_use_only_the_public_api():
    scenarios = (REPO_ROOT / "benchmarks" / "click_3084_public" / "scenarios.py").read_text(
        encoding="utf-8")

    # The public check must not reach into private helpers or the hidden suite.
    assert "click.testing" in scenarios
    assert "from click._" not in scenarios and "import click._" not in scenarios
    # It is deliberately not collected by the project's own test run.
    assert not (REPO_ROOT / "benchmarks" / "click_3084_public" / "test_scenarios.py").exists()


def test_the_second_task_ships_a_public_adapter_without_any_cases():
    """The adapter is public; the cases it will be paired with stay with the trusted side."""
    adapter = (REPO_ROOT / "benchmarks" / "click_3084_public" / "stdio_adapter.py").read_text(
        encoding="utf-8")

    assert "DEVFLOW_CANDIDATE_ROOT" in adapter
    assert "stdin" in adapter and "json" in adapter
    # No hidden-case content, no credentials, and it is not collected as a test module.
    assert "stdin_base64" not in adapter and "expected_stdout" not in adapter
    assert "api_key" not in adapter.lower()
    assert not (REPO_ROOT / "benchmarks" / "click_3084_public" / "test_stdio_adapter.py").exists()


def test_the_public_adapter_reproduces_the_defect_when_checkouts_are_provided(tmp_path):
    """Skipped by default: it needs the two verified checkouts of the public repository."""
    import json
    import os
    import subprocess
    import sys

    base = os.environ.get("DEVFLOW_CLICK3084_BASE_CHECKOUT")
    fix = os.environ.get("DEVFLOW_CLICK3084_FIX_CHECKOUT")
    if not base or not fix:
        pytest.skip("set DEVFLOW_CLICK3084_BASE_CHECKOUT / _FIX_CHECKOUT to run this")
    adapter = REPO_ROOT / "benchmarks" / "click_3084_public" / "stdio_adapter.py"
    request = json.dumps({"flag_value": "Flag", "default": "Default", "argv": ["--name"]})

    def run(checkout):
        completed = subprocess.run(
            [sys.executable, "-B", str(adapter)], input=request, capture_output=True,
            text=True, timeout=60, env={**os.environ, "DEVFLOW_CANDIDATE_ROOT": checkout})
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    # The baseline must fail the documented behaviour; the reference must satisfy it.
    assert run(base)["exit_code"] == 2
    assert run(fix) == {"exit_code": 0, "output": "Hello, Flag!\n"}


def test_the_second_task_ships_a_public_evaluation_image():
    """The image may contain the public adapter and nothing else of ours."""
    doctext = (REPO_ROOT / "benchmarks" / "click_3084_public" / "Dockerfile").read_text(
        encoding="utf-8")

    copies = [line.strip() for line in doctext.splitlines()
              if line.strip().upper().startswith("COPY ")]
    assert copies == ["COPY stdio_adapter.py /opt/devflow/click3084_adapter.py"]
    assert "cases" in doctext.lower()          # says where they live: not in the image
    assert "secret" not in doctext.lower() and "api_key" not in doctext.lower()
    # The adapter path in the image matches what the run plan tells the operator to use.
    assert "/opt/devflow/click3084_adapter.py" in doctext
