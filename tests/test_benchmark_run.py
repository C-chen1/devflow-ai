import base64
from hashlib import sha256
import json
import tempfile
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
import sys
from uuid import uuid4

import pytest
from pydantic import ValidationError

import devflow_ai.benchmark_run as benchmark_run_module
from devflow_ai.benchmark_run import (
    BenchmarkLimits, BenchmarkRunError, load_independent_material, main,
)
from devflow_ai.policy import build_default_policy


def test_limits_refuse_retries_and_keep_the_v1_defaults():
    limits = BenchmarkLimits()

    assert (limits.max_duration_seconds, limits.max_model_tokens) == (1200, 80_000)
    assert (limits.attempts, limits.auto_retries) == (1, 0)


@pytest.mark.parametrize("overrides", [{"attempts": 2}, {"auto_retries": 1}])
def test_v1_forbids_more_than_one_attempt(overrides):
    with pytest.raises(ValidationError, match="one attempt"):
        BenchmarkLimits(**overrides)


@pytest.mark.parametrize("overrides", [
    {"max_duration_seconds": 0}, {"max_model_tokens": 0},
])
def test_limits_must_be_positive(overrides):
    with pytest.raises(ValidationError):
        BenchmarkLimits(**overrides)


def test_limits_reject_unknown_keys():
    with pytest.raises(ValidationError):
        BenchmarkLimits(max_retries=1)


def _pack(tmp_path, *, suite_version="click-3019-hidden-v1", external="unknown",
          materials_sha256="b" * 64):
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir(exist_ok=True)
    manifest = {
        "pack_id": "demo-pack", "schema_version": 1, "title": "Demo pack",
        "source": {"issue_url": "https://example.invalid/issues/1",
                   "repository_url": "https://example.invalid/repo",
                   "license": "MIT", "issue_reference": "demo#1"},
        "base_commit": "a" * 40,
        "environment": {"python_requires": ">=3.11", "dependencies": ["pytest"],
                        "platform_notes": "CI"},
        "issue": {"sanitized_problem": "A sanitized problem.", "observable_error": "An error.",
                  "expected_behavior": "The expected behaviour."},
        "scope": {"allowed_paths": ["src/"]},
        "public_checks": {"command": ["python", "-m", "pytest"], "command_name": "pytest",
                          "target_tests": ["tests/test_demo::test_target"]},
        "baseline": {"status": "verified_here"},
        "external_connections": external,
        "independent_suite": (
            {"suite_version": suite_version, "materials_sha256": materials_sha256,
             "digest_scope": "bytes as delivered", "digest_method": "sha256 over those bytes",
             "provided_by": "trusted_control_plane",
             "declaration": "Provided by the trusted control plane."}
            if suite_version else None),
        "unverified": (
            [{"field": "policy", "reason": "Not pinned for this fixture."}]
            + ([] if suite_version else [{"field": "independent_suite", "reason": "Held elsewhere."}])
            + ([{"field": "external_connections", "reason": "No trusted declaration yet."}]
               if external == "unknown" else [])),
    }
    (pack_dir / "task.json").write_text(json.dumps(manifest), encoding="utf-8")
    return pack_dir


def _material_file(tmp_path, *, suite_version="click-3019-hidden-v1", marker=b"expected-output",
                   **overrides):
    payload = {
        "suite_version": suite_version,
        "image": "python:3.11-slim",
        "command": ["python", "-B", "adapter.py"],
        "cases": [{"stdin_base64": base64.b64encode(b"input").decode("ascii"),
                   "stdout_base64": base64.b64encode(marker).decode("ascii")}],
        "calibration": {"baseline_workspace": str(tmp_path / "baseline"),
                        "corrected_workspace": str(tmp_path / "corrected"),
                        "expected_commit": "c" * 40},
        "timeout_seconds": 15,
    }
    payload.update(overrides)
    path = tmp_path / "material.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _digest(path):
    """Digest the raw bytes, the same way the entry does."""
    return sha256(Path(path).read_bytes()).hexdigest()


def _cli_args(tmp_path, pack, *, record, extra=()):
    for name in ("workspaces", "artifacts"):
        (tmp_path / name).mkdir(exist_ok=True)
    return [
        "--pack", str(pack), "--task-id", str(uuid4()), "--operator", "operator-a",
        "--python-executable", sys.executable,
        "--workspaces-root", str(tmp_path / "workspaces"),
        "--artifacts-root", str(tmp_path / "artifacts"),
        "--record", str(record), *extra,
    ]


def test_the_cli_will_not_start_without_confirmation(tmp_path, capsys):
    pack = _pack(tmp_path)
    record = tmp_path / "record.json"

    exit_code = main(_cli_args(tmp_path, pack, record=record))

    assert exit_code == 2
    assert "confirm-run" in capsys.readouterr().err
    assert not record.exists()


def test_the_cli_reports_missing_model_configuration_as_configuration(tmp_path, capsys):
    pack = _pack(tmp_path)
    record = tmp_path / "record.json"

    @contextmanager
    def unavailable(session_factory, worker_id):
        raise ValueError("DEEPSEEK_API_KEY is required")
        yield  # pragma: no cover - the raise above is the point

    exit_code = main(
        _cli_args(tmp_path, pack, record=record, extra=["--confirm-run"]),
        session_factory=lambda: None, gateways_factory=unavailable)

    assert exit_code == 2
    assert "Model gateways are not configured" in capsys.readouterr().err
    assert not record.exists()


def test_the_cli_runs_one_attempt_and_reports_where_its_evidence_went(
    tmp_path, monkeypatch, capsys,
):
    pack = _pack(tmp_path, external="unknown")
    record = tmp_path / "record.json"
    calls = []
    artifact_id = uuid4()

    def stub_run_one_task(request, **kwargs):
        calls.append({"request": request, **kwargs})
        record.write_text("{}", encoding="utf-8")
        return {"outcome": "incomplete", "run_id": str(uuid4()), "record_path": str(record),
                "reason": "run_not_running", "stages": 2, "stopped": "run_not_running",
                "finalization": {"outcome": "skipped", "artifact_id": artifact_id}, "deadline_fired": False}

    monkeypatch.setattr(benchmark_run_module, "run_one_task", stub_run_one_task)

    exit_code = main(
        _cli_args(tmp_path, pack, record=record, extra=["--confirm-run"]),
        session_factory=lambda: None,
        gateways_factory=lambda factory, worker_id: nullcontext(object()))

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["event"] == "benchmark_run_finished"
    assert summary["outcome"] == "incomplete"
    assert summary["finalization"]["artifact_id"] == str(artifact_id)
    assert summary["record_path"] == str(record)
    # The pinned limits are carried into the call, not only printed.
    assert calls[0]["request"].limits.max_duration_seconds == 1200
    assert calls[0]["request"].limits.max_model_tokens == 80_000
    assert summary["limits"] == {"max_duration_seconds": 1200, "max_model_tokens": 80_000,
                                 "max_stages": 32, "attempts": 1, "auto_retries": 0}
    assert summary["external_connections"] == "unknown"


def test_material_is_loaded_without_exposing_its_cases(tmp_path, monkeypatch, capsys):
    material_path = _material_file(tmp_path, marker=b"SECRET-EXPECTED-OUTPUT")
    pack = _pack(tmp_path, materials_sha256=_digest(material_path))
    record = tmp_path / "record.json"
    captured = {}

    def stub_run_one_task(request, **kwargs):
        captured.update(kwargs)
        record.write_text("{}", encoding="utf-8")
        return {"outcome": "incomplete", "run_id": str(uuid4()), "record_path": str(record)}

    monkeypatch.setattr(benchmark_run_module, "run_one_task", stub_run_one_task)

    exit_code = main(
        _cli_args(tmp_path, pack, record=record,
                  extra=["--confirm-run", "--material", str(material_path)]),
        session_factory=lambda: None,
        gateways_factory=lambda factory, worker_id: nullcontext(object()))

    assert exit_code == 0
    material = captured["independent_evaluation"]
    assert material.image == "python:3.11-slim"
    assert len(material.cases) == 1
    # The case payload stays out of every rendering path, and out of the CLI output.
    assert "SECRET-EXPECTED-OUTPUT" not in repr(material)
    assert "SECRET-EXPECTED-OUTPUT" not in capsys.readouterr().out


def test_material_that_does_not_match_the_pack_is_refused(tmp_path, capsys):
    material_path = _material_file(tmp_path, suite_version="some-other-suite")
    pack = _pack(tmp_path, materials_sha256=_digest(material_path))
    record = tmp_path / "record.json"

    exit_code = main(
        _cli_args(tmp_path, pack, record=record,
                  extra=["--confirm-run", "--material", str(material_path)]),
        session_factory=lambda: None,
        gateways_factory=lambda factory, worker_id: nullcontext(object()))

    assert exit_code == 2
    assert "suite version" in capsys.readouterr().err
    assert not record.exists()


def test_a_malformed_material_file_is_refused(tmp_path):
    path = tmp_path / "material.json"
    path.write_text(json.dumps({"suite_version": "v1"}), encoding="utf-8")

    with pytest.raises(BenchmarkRunError, match="documented fields"):
        load_independent_material(path, expected_sha256=_digest(path))


def test_a_file_that_does_not_match_the_pinned_digest_is_refused(tmp_path):
    path = _material_file(tmp_path)

    with pytest.raises(BenchmarkRunError, match="does not match the pack's materials_sha256"):
        load_independent_material(path, expected_sha256="0" * 64)

    loaded, version = load_independent_material(path, expected_sha256=_digest(path))
    assert version == "click-3019-hidden-v1" and len(loaded.cases) == 1


def test_material_cannot_be_loaded_when_the_pack_pins_no_digest(tmp_path):
    path = _material_file(tmp_path)

    with pytest.raises(BenchmarkRunError, match="does not pin a material digest"):
        load_independent_material(path)


def test_the_cli_refuses_material_whose_bytes_were_tampered_with(tmp_path, capsys):
    material_path = _material_file(tmp_path, marker=b"expected-output")
    pack = _pack(tmp_path, materials_sha256=_digest(material_path))
    record = tmp_path / "record.json"
    # Same JSON, one extra byte: the digest covers the raw bytes, so this must be refused.
    material_path.write_bytes(material_path.read_bytes() + b"\n")

    exit_code = main(
        _cli_args(tmp_path, pack, record=record,
                  extra=["--confirm-run", "--material", str(material_path)]),
        session_factory=lambda: None,
        gateways_factory=lambda factory, worker_id: nullcontext(object()))

    assert exit_code == 2
    assert "materials_sha256" in capsys.readouterr().err
    assert not record.exists()


class _TaskSession:
    """Stand-in for a session that answers a Task lookup and an existing-Run lookup."""

    def __init__(self, task, existing_run=None):
        self._task = task
        self._existing_run = existing_run

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get(self, model, key):
        return self._task

    def scalar(self, statement):
        return self._existing_run


def _prepared_policy(tmp_path):
    policy = build_default_policy()
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy.model_dump(mode="json")), encoding="utf-8")
    return policy, path


def test_policy_snapshot_differences_names_the_sections():
    from devflow_ai.benchmark_run import policy_snapshot_differences

    prepared, _ = _prepared_policy(Path(tempfile.mkdtemp()))
    stored = prepared.model_dump(mode="json")
    assert policy_snapshot_differences(stored, prepared) == []

    stored["budget"]["max_tool_calls_per_run"] += 1
    assert policy_snapshot_differences(stored, prepared) == ["budget"]

    stored["policy_version"] = "some-other-version"
    assert policy_snapshot_differences(stored, prepared) == ["budget", "policy_version"]


def test_the_cli_refuses_when_the_task_snapshot_differs_from_the_prepared_file(tmp_path, capsys):
    pack = _pack(tmp_path)
    record = tmp_path / "record.json"
    prepared, policy_path = _prepared_policy(tmp_path)
    stored = prepared.model_dump(mode="json")
    stored["budget"]["max_tool_calls_per_run"] += 1

    exit_code = main(
        _cli_args(tmp_path, pack, record=record,
                  extra=["--confirm-run", "--policy", str(policy_path)]),
        session_factory=lambda: _TaskSession(SimpleNamespace(policy_snapshot=stored)),
        gateways_factory=lambda factory, worker_id: nullcontext(object()))

    assert exit_code == 2
    assert "budget" in capsys.readouterr().err
    assert not record.exists()


def test_the_cli_runs_when_the_task_snapshot_matches(tmp_path, monkeypatch, capsys):
    pack = _pack(tmp_path)
    record = tmp_path / "record.json"
    prepared, policy_path = _prepared_policy(tmp_path)
    stored = prepared.model_dump(mode="json")

    def stub_run_one_task(request, **kwargs):
        record.write_text("{}", encoding="utf-8")
        return {"outcome": "incomplete", "run_id": str(uuid4()), "record_path": str(record)}

    monkeypatch.setattr(benchmark_run_module, "run_one_task", stub_run_one_task)

    exit_code = main(
        _cli_args(tmp_path, pack, record=record,
                  extra=["--confirm-run", "--policy", str(policy_path)]),
        session_factory=lambda: _TaskSession(SimpleNamespace(policy_snapshot=stored)),
        gateways_factory=lambda factory, worker_id: nullcontext(object()))

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["event"] == \
        "benchmark_run_finished"


def test_the_cli_refuses_an_existing_record_path_before_spending_anything(tmp_path, capsys):
    pack = _pack(tmp_path)
    record = tmp_path / "record.json"
    record.write_text("{}", encoding="utf-8")

    exit_code = main(
        _cli_args(tmp_path, pack, record=record, extra=["--confirm-run"]),
        session_factory=lambda: None,
        gateways_factory=lambda factory, worker_id: nullcontext(object()))

    assert exit_code == 2
    assert "never overwritten" in capsys.readouterr().err
    assert record.read_text(encoding="utf-8") == "{}"


def test_a_rehearsal_is_marked_and_reported_as_one(tmp_path, monkeypatch, capsys):
    pack = _pack(tmp_path)
    record = tmp_path / "record.json"
    calls = []

    def stub_run_one_task(request, **kwargs):
        calls.append(request)
        record.write_text("{}", encoding="utf-8")
        return {"outcome": "incomplete", "run_id": str(uuid4()), "record_path": str(record)}

    monkeypatch.setattr(benchmark_run_module, "run_one_task", stub_run_one_task)

    exit_code = main(
        _cli_args(tmp_path, pack, record=record,
                  extra=["--confirm-run", "--attempt-kind", "rehearsal"]),
        session_factory=lambda: None,
        gateways_factory=lambda factory, worker_id: nullcontext(object()))

    captured = capsys.readouterr()
    assert exit_code == 0
    assert calls[0].attempt_kind == "rehearsal"
    assert json.loads(captured.out.strip().splitlines()[-1])["attempt_kind"] == "rehearsal"
    # The operator is told, in the same breath, that this will not enter the formal sample.
    assert "exclude" in captured.err


def test_a_run_is_formal_unless_marked_otherwise(tmp_path, monkeypatch):
    pack = _pack(tmp_path)
    record = tmp_path / "record.json"
    calls = []

    def stub_run_one_task(request, **kwargs):
        calls.append(request)
        record.write_text("{}", encoding="utf-8")
        return {"outcome": "incomplete", "run_id": str(uuid4()), "record_path": str(record)}

    monkeypatch.setattr(benchmark_run_module, "run_one_task", stub_run_one_task)

    assert main(
        _cli_args(tmp_path, pack, record=record, extra=["--confirm-run"]),
        session_factory=lambda: None,
        gateways_factory=lambda factory, worker_id: nullcontext(object())) == 0
    assert calls[0].attempt_kind == "formal"


def test_the_cli_refuses_a_task_that_already_has_a_run(tmp_path, capsys):
    """A second attempt needs a new Task; reusing one would not produce a second record."""
    material = _material_file(tmp_path)
    pack = _pack(tmp_path, materials_sha256=_digest(material))
    record = tmp_path / "record.json"
    prepared, policy_path = _prepared_policy(tmp_path)
    stored = prepared.model_dump(mode="json")
    session = _TaskSession(SimpleNamespace(policy_snapshot=stored), existing_run=uuid4())

    exit_code = main(
        _cli_args(tmp_path, pack, record=record,
                  extra=["--confirm-run", "--policy", str(policy_path)]),
        session_factory=lambda: session,
        gateways_factory=lambda factory, worker_id: nullcontext(object()))

    assert exit_code == 2
    assert "already has a Run" in capsys.readouterr().err
    assert not record.exists()
