import base64
from hashlib import sha256
import json
import os
import subprocess
import sys
from pathlib import Path

import devflow_ai.preflight as preflight_module
from devflow_ai.preflight import (
    check_checkout, check_database, check_environment, check_independent_material,
    check_interpreter, check_policy_snapshot, main, observe_public_test_connections,
    project_migration_head, run_public_baseline,
)
from devflow_ai.task_pack import load_manifest

CLICK_PACK = Path(__file__).resolve().parents[1] / "benchmarks" / "click_3019_public"


def test_the_committed_policy_snapshot_passes_the_preflight():
    result = check_policy_snapshot(CLICK_PACK / "policy.draft.json")

    assert result["ok"] is True, result["problems"]
    assert result["policy_version"] == "benchmark-click3019-v1"
    assert result["budget"] == {
        "max_run_duration_seconds": 1200, "max_llm_tokens_per_run": 80_000,
        "max_retries_per_task": 0, "max_rediagnose_attempts": 0,
        "max_graph_steps_per_run": 30, "max_tool_calls_per_run": 60,
    }
    assert result["permissions"]["verifier"] == ["git_diff", "read_file", "run_command"]


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          timeout=15).stdout


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("original\n", encoding="utf-8")
    (repo / "src" / "mypkg").mkdir(parents=True)
    (repo / "src" / "mypkg" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "README.md", "src/mypkg/__init__.py")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-qm", "initial")
    return repo, _git(repo, "rev-parse", "HEAD").decode("ascii").strip()


def _pack(tmp_path, *, base_commit, target_tests=("scenarios::test_target",),
          test_source=None, suite_version=None, materials_sha256="b" * 64,
          baseline_status="verified_here", external="unknown"):
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "scenarios.py").write_text(
        test_source or (
            "from pathlib import Path\n\n"
            "def test_target():\n"
            "    assert Path(\"README.md\").read_bytes() == b\"updated\\n\"\n\n"
            "def test_control():\n"
            "    assert True\n"
        ), encoding="utf-8")
    manifest = {
        "pack_id": "demo-pack", "schema_version": 1, "title": "Demo pack",
        "source": {"issue_url": "https://example.invalid/issues/1",
                   "repository_url": "https://example.invalid/repo",
                   "license": "MIT", "issue_reference": "demo#1"},
        "base_commit": base_commit,
        "environment": {"python_requires": ">=3.11", "dependencies": ["pytest"],
                        "platform_notes": "CI"},
        "issue": {"sanitized_problem": "A sanitized problem.", "observable_error": "An error.",
                  "expected_behavior": "The expected behaviour."},
        "scope": {"allowed_paths": ["src/"]},
        "public_checks": {"command": ["python", "-B", "-m", "pytest", "-q",
                                      "-p", "no:cacheprovider", "scenarios.py"],
                          "command_name": "pytest", "target_tests": list(target_tests),
                          "pythonpath_relative": "src"},
        "baseline": {"status": baseline_status, "failed_targets": 1, "passed_controls": 1},
        "external_connections": external,
        "independent_suite": (
            {"suite_version": suite_version, "materials_sha256": materials_sha256,
             "digest_scope": "bytes as delivered", "digest_method": "sha256 over those bytes",
             "provided_by": "trusted_control_plane",
             "declaration": "Provided by the trusted control plane."}
            if suite_version else None),
        "unverified": (
            [{"field": "policy", "reason": "Not pinned for this fixture."}]
            + ([] if suite_version else
               [{"field": "independent_suite", "reason": "Held by the trusted side."}])
            + ([{"field": "external_connections", "reason": "No trusted declaration yet."}]
               if external == "unknown" else [])
            + ([] if baseline_status == "verified_here" else
               [{"field": "baseline", "reason": "Recorded elsewhere, not re-verified here."}])),
    }
    (pack_dir / "task.json").write_text(json.dumps(manifest), encoding="utf-8")
    return pack_dir


def test_the_public_baseline_separates_the_target_failure_from_the_control(tmp_path):
    repo, base_commit = _repo(tmp_path)
    pack_dir = _pack(tmp_path, base_commit=base_commit)

    result = run_public_baseline(pack_dir, repo, sys.executable)

    assert result["ok"] is True, result["problems"]
    # A preflight observation is not a recorded baseline and must not pretend to be one.
    assert result["writes_evidence"] is False
    assert result["target_statuses"] == {"scenarios::test_target": "failed"}
    assert result["controls"] == {"scenarios::test_control": "passed"}
    assert result["exit_code"] == 1
    # The command that ran is the assembled one, not the configured one.
    assert result["command"] != result["configured_command"]
    assert "scenarios.py" in result["configured_command"]
    assert "--rootdir" in " ".join(result["command"])
    # The checkout is untouched, so a real baseline integrity check would still hold.
    assert result["files_in_checkout"] == []
    assert _git(repo, "status", "--porcelain").strip() == b""
    assert Path(result["report_path"]).is_file()
    # The command digest contains this run's copy path, so it is only comparable inside the
    # same artifact root; the per-file content digest is what survives a change of root.
    assert result["comparable_across_runs"] == ["public_tests_sha256"]
    assert "same artifact root" in result["command_sha256_scope"]


def test_a_node_id_style_target_is_reported_as_absent(tmp_path):
    """The report records `classname::name`; a `.py::` target can never match it."""
    repo, base_commit = _repo(tmp_path)
    pack_dir = _pack(tmp_path, base_commit=base_commit,
                     target_tests=("scenarios.py::test_target",))

    result = run_public_baseline(pack_dir, repo, sys.executable)

    assert result["ok"] is False
    assert any("absent from the report" in problem for problem in result["problems"])
    # The observed name is reported so the declaration can be corrected from evidence.
    assert "scenarios::test_target" in result["observed"]


def test_a_target_that_does_not_fail_at_the_base_commit_is_reported(tmp_path):
    repo, base_commit = _repo(tmp_path)
    pack_dir = _pack(tmp_path, base_commit=base_commit, test_source=(
        "def test_target():\n"
        "    assert True\n"))

    result = run_public_baseline(pack_dir, repo, sys.executable)

    assert result["ok"] is False
    assert any("did not fail at the base commit" in problem for problem in result["problems"])


def test_a_dirty_or_unpinned_checkout_is_refused(tmp_path):
    repo, base_commit = _repo(tmp_path)
    (repo / "README.md").write_text("changed\n", encoding="utf-8")

    dirty = check_checkout(repo, base_commit, python_executable=sys.executable,
                           pythonpath_relative="src", import_name="mypkg")
    assert dirty["ok"] is False and dirty["dirty"] is True
    assert any("local changes" in problem for problem in dirty["problems"])

    clean = check_checkout(repo, "a" * 40, python_executable=sys.executable,
                           pythonpath_relative="src", import_name="mypkg")
    assert clean["ok"] is False
    assert any("pinned base commit" in problem for problem in clean["problems"])


def test_the_checkout_check_reports_where_the_package_is_imported_from(tmp_path):
    repo, base_commit = _repo(tmp_path)

    result = check_checkout(repo, base_commit, python_executable=sys.executable,
                            pythonpath_relative="src", import_name="mypkg")

    assert result["ok"] is True, result["problems"]
    assert result["import_inside"] is True
    # The child decides containment with real paths; the test compares the same way, so a
    # case or junction difference on another platform cannot turn a real import into a fail.
    expected = os.path.realpath(str(repo / "src"))
    actual = os.path.realpath(result["imported_from"])
    assert os.path.normcase(os.path.commonpath([actual, expected])) == os.path.normcase(expected)


def test_a_package_imported_from_outside_the_checkout_is_refused(tmp_path):
    repo, base_commit = _repo(tmp_path)

    result = check_checkout(repo, base_commit, python_executable=sys.executable,
                            pythonpath_relative="src", import_name="json")

    assert result["ok"] is False
    assert result["import_inside"] is False
    assert any("outside the pinned import path" in problem for problem in result["problems"])
    # Both paths are reported so the operator can see what actually happened.
    assert "expected under" in result["problems"][0]


def test_a_relative_interpreter_is_refused():
    result = check_interpreter("python")

    assert result["ok"] is False
    assert result["problems"] == ["the interpreter must be an absolute path"]

    absolute = check_interpreter(sys.executable)
    assert absolute["ok"] is True and absolute["version"]
    assert absolute["pytest"]


def test_the_public_checks_cannot_see_model_credentials(tmp_path, monkeypatch):
    repo, base_commit = _repo(tmp_path)
    pack_dir = _pack(tmp_path, base_commit=base_commit, test_source=(
        "import os\n"
        "from pathlib import Path\n\n"
        "def test_target():\n"
        "    assert Path(\"README.md\").read_bytes() == b\"updated\\n\"\n\n"
        "def test_control():\n"
        "    assert os.environ.get(\"DEEPSEEK_API_KEY\") is None\n"
        "    assert os.environ.get(\"DEVFLOW_DB_PASSWORD\") is None\n"
    ))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "super-secret-value")
    monkeypatch.setenv("DEVFLOW_DB_PASSWORD", "another-secret")
    monkeypatch.setenv("DEVFLOW_DEEPSEEK_MODEL", "deepseek-flash")

    result = run_public_baseline(pack_dir, repo, sys.executable)

    # The control test only passes if the child really could not see the values.
    assert result["ok"] is True, result["problems"]
    assert result["controls"] == {"scenarios::test_control": "passed"}
    assert {"DEEPSEEK_API_KEY", "DEVFLOW_DB_PASSWORD"}.issubset(
        result["child_environment_removed"])
    # Only names are handled: the operator's own environment is restored untouched.
    assert os.environ["DEEPSEEK_API_KEY"] == "super-secret-value"
    assert "super-secret-value" not in json.dumps(result)


def test_captured_test_output_is_not_returned_by_default(tmp_path):
    repo, base_commit = _repo(tmp_path)
    pack_dir = _pack(tmp_path, base_commit=base_commit, test_source=(
        "def test_target():\n"
        "    print('noise from the target')\n"
        "    assert False\n\n"
        "def test_control():\n"
        "    assert True\n"))

    default = run_public_baseline(pack_dir, repo, sys.executable)
    shown = run_public_baseline(pack_dir, repo, sys.executable, include_output=True)

    assert default["output_included"] is False and default["stdout_tail"] is None
    assert shown["output_included"] is True
    assert "noise from the target" in shown["stdout_tail"]


def test_an_unconfigured_database_is_reported_rather_than_crashing(monkeypatch):
    for name in ("DEVFLOW_DB_NAME", "DEVFLOW_DB_USER", "DEVFLOW_DB_PASSWORD",
                 "DEVFLOW_DB_HOST", "DEVFLOW_DB_PORT"):
        monkeypatch.delenv(name, raising=False)

    result = check_database()

    assert result["ok"] is False and result["connected"] is False
    assert result["problems"]


def test_model_settings_are_reported_without_their_values(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "super-secret-value")
    monkeypatch.setenv("DEVFLOW_DEEPSEEK_MODEL", "deepseek-flash")
    monkeypatch.delenv("UNRELATED_SETTING", raising=False)

    result = check_environment()

    assert result["ok"] is True
    assert result["present"] == {"DEEPSEEK_API_KEY": True, "DEVFLOW_DEEPSEEK_MODEL": True}
    assert result["model_id"] == "deepseek-flash"
    assert "super-secret-value" not in json.dumps(result)


def test_network_hints_are_observed_but_never_declared(tmp_path):
    repo, base_commit = _repo(tmp_path)
    pack_dir = _pack(tmp_path, base_commit=base_commit, test_source=(
        "import socket\n\n"
        "def test_target():\n"
        "    assert True\n"))

    result = observe_public_test_connections(pack_dir)

    assert result["hits"] == {"scenarios.py": ["import socket"]}
    assert result["declaration"] == "unchanged"
    # A static hit is a question for the trusted side, not a declaration of its own.
    assert load_manifest(pack_dir).external_connections == "unknown"


def test_the_command_line_reports_a_failed_check(tmp_path, monkeypatch, capsys):
    repo, base_commit = _repo(tmp_path)
    pack_dir = _pack(tmp_path, base_commit=base_commit)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "placeholder")
    monkeypatch.setenv("DEVFLOW_DEEPSEEK_MODEL", "deepseek-flash")

    ok_exit = main(["--pack", str(pack_dir), "--checkout", str(repo), "--python", sys.executable,
                    "--skip-docker"])
    output = capsys.readouterr().out
    assert ok_exit == 0, output
    verdict = json.loads(output.strip().splitlines()[-1])
    assert verdict["preflight"] == "ok" and verdict["failed_checks"] == []
    # Runnability and formal acceptance are separate verdicts; this pack is not ready for
    # the second one, and the summary says why without inventing any value.
    assert any("external_connections" in blocker
               for blocker in verdict["formal_acceptance_blockers"])

    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    failed_exit = main(["--pack", str(pack_dir), "--checkout", str(repo), "--python", sys.executable,
                        "--skip-docker"])
    assert failed_exit == 1
    assert "checkout" in json.loads(capsys.readouterr().out.strip().splitlines()[-1])["failed_checks"]


def _material_file(tmp_path, *, suite_version, expected_commit, marker=b"expected-output"):
    payload = {
        "suite_version": suite_version,
        "image": "python:3.11-slim",
        "command": ["python", "-B", "adapter.py"],
        "cases": [{"stdin_base64": base64.b64encode(b"input").decode("ascii"),
                   "stdout_base64": base64.b64encode(marker).decode("ascii")}],
        "calibration": {"baseline_workspace": str(tmp_path / "baseline"),
                        "corrected_workspace": str(tmp_path / "corrected"),
                        "expected_commit": expected_commit},
        "timeout_seconds": 15,
    }
    path = tmp_path / "material.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def _git_repo_at(path):
    """Create a real repository at exactly this path (no links: they need privileges)."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text("facts\n", encoding="utf-8")
    _git(path, "init", "-q")
    _git(path, "add", "README.md")
    _git(path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-qm", "initial")
    return _git(path, "rev-parse", "HEAD").decode("ascii").strip()


def _calibration_workspaces(tmp_path):
    _git_repo_at(tmp_path / "baseline")
    return _git_repo_at(tmp_path / "corrected")


def test_the_material_check_accepts_matching_identity(tmp_path):
    commit = _calibration_workspaces(tmp_path)
    material_path = _material_file(
        tmp_path, suite_version="hidden-v1", expected_commit=commit,
        marker=b"SECRET-EXPECTED-OUTPUT")
    pack_dir = _pack(tmp_path, base_commit="a" * 40, suite_version="hidden-v1",
                     materials_sha256=_digest(material_path))

    result = check_independent_material(
        pack_dir, material_path, image_probe=lambda image: (True, "sha256:abc"))

    assert result["ok"] is True, result["problems"]
    assert result["suite_version"] == "hidden-v1" and result["pack_suite_version"] == "hidden-v1"
    assert result["case_count"] == 1
    assert result["image_present"] is True and result["image_id"] == "sha256:abc"
    assert result["calibration"]["corrected_head"] == commit
    # The pinned digest is checked against the file's raw bytes before anything else.
    assert result["digest_matches"] is True
    assert result["file_sha256"] == _digest(material_path)
    # Identity only: the case payload never appears in the report.
    assert "SECRET-EXPECTED-OUTPUT" not in json.dumps(result)
    # A matching digest survives nothing on its own: calibration is the formal path's job.
    assert result["formal_gate"]["requirements"] == [
        "the defect baseline must fail the trusted cases",
        "the reference/corrected version must pass them",
    ]
    assert "no container is started" in result["formal_gate"]["not_checked_here"]


def test_the_material_check_names_an_unpinned_pack_suite_version(tmp_path):
    commit = _calibration_workspaces(tmp_path)
    material_path = _material_file(tmp_path, suite_version="hidden-v1", expected_commit=commit)
    pack_dir = _pack(tmp_path, base_commit="a" * 40)

    result = check_independent_material(
        pack_dir, material_path, image_probe=lambda image: (True, "sha256:abc"))

    assert result["ok"] is False
    assert any("does not pin an independent suite version" in problem for problem in result["problems"])


def test_the_material_check_reports_a_wrong_calibration_commit(tmp_path):
    _calibration_workspaces(tmp_path)
    material_path = _material_file(
        tmp_path, suite_version="hidden-v1", expected_commit="d" * 40)
    pack_dir = _pack(tmp_path, base_commit="a" * 40, suite_version="hidden-v1",
                     materials_sha256=_digest(material_path))

    result = check_independent_material(
        pack_dir, material_path, image_probe=lambda image: (True, "sha256:abc"))

    assert result["ok"] is False
    assert any("declared commit" in problem for problem in result["problems"])


def test_the_material_check_reports_an_image_that_is_not_local(tmp_path):
    commit = _calibration_workspaces(tmp_path)
    material_path = _material_file(tmp_path, suite_version="hidden-v1", expected_commit=commit)
    pack_dir = _pack(tmp_path, base_commit="a" * 40, suite_version="hidden-v1",
                     materials_sha256=_digest(material_path))

    result = check_independent_material(
        pack_dir, material_path, image_probe=lambda image: (False, None))

    assert result["ok"] is False
    assert result["image_present"] is False
    assert any("not present locally" in problem for problem in result["problems"])


def test_the_material_check_reports_a_digest_mismatch(tmp_path):
    commit = _calibration_workspaces(tmp_path)
    material_path = _material_file(tmp_path, suite_version="hidden-v1", expected_commit=commit)
    pack_dir = _pack(tmp_path, base_commit="a" * 40, suite_version="hidden-v1",
                     materials_sha256="0" * 64)

    result = check_independent_material(
        pack_dir, material_path, image_probe=lambda image: (True, "sha256:abc"))

    assert result["ok"] is False
    assert result["digest_matches"] is False
    assert result["materials_sha256"] == "0" * 64
    assert result["file_sha256"] == _digest(material_path)
    assert any("materials_sha256" in problem for problem in result["problems"])


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value

    def scalar_one_or_none(self):
        return self._value


class _FakeConnection:
    def __init__(self, revision):
        self._revision = revision

    def execute(self, statement):
        sql = str(statement)
        if "alembic_version" in sql:
            if self._revision is None:
                raise RuntimeError("relation does not exist")
            return _FakeResult(self._revision)
        if "current_database" in sql:
            return _FakeResult("fake_db")
        # The table count query also mentions current_schema(), so match it first.
        if "information_schema.tables" in sql:
            return _FakeResult(3)
        if "current_schema" in sql:
            return _FakeResult("public")
        return _FakeResult(1)

    def rollback(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeEngine:
    def __init__(self, revision):
        self._revision = revision

    def connect(self):
        return _FakeConnection(self._revision)

    def dispose(self):
        return None


def test_the_database_check_reports_the_target_and_whether_it_is_at_head(
    monkeypatch, tmp_path,
):
    import devflow_ai.database as database_module

    monkeypatch.setattr(database_module, "create_db_engine", lambda: _FakeEngine("old-revision"))
    monkeypatch.setattr(preflight_module, "project_migration_head", lambda *_: "new-revision")

    stale = check_database(migrations_directory=tmp_path)

    assert stale["connected"] is True
    assert stale["database"] == "fake_db"
    assert stale["alembic_revision"] == "old-revision" and stale["project_head"] == "new-revision"
    assert stale["at_head"] is False and stale["ok"] is False
    assert any("expects new-revision" in problem for problem in stale["problems"])

    monkeypatch.setattr(database_module, "create_db_engine", lambda: _FakeEngine("new-revision"))
    current = check_database(migrations_directory=tmp_path)
    assert current["ok"] is True and current["at_head"] is True


def test_a_database_without_a_revision_table_is_reported_without_a_verdict(
    monkeypatch, tmp_path,
):
    import devflow_ai.database as database_module

    monkeypatch.setattr(database_module, "create_db_engine", lambda: _FakeEngine(None))
    monkeypatch.setattr(preflight_module, "project_migration_head", lambda *_: "new-revision")

    result = check_database(migrations_directory=tmp_path)

    assert result["connected"] is True and result["alembic_revision"] is None
    # No recorded revision is not evidence of being at head, and it is not an error either.
    assert result["at_head"] is None and result["ok"] is True


def test_the_expected_migration_is_read_from_this_checkout():
    head = project_migration_head()

    assert head is not None and len(head) == 12
    assert all(character in "0123456789abcdef" for character in head)


def test_the_formal_acceptance_blockers_name_every_open_gap(tmp_path):
    from devflow_ai.preflight import formal_acceptance_blockers

    manifest = load_manifest(_pack(tmp_path, base_commit="a" * 40, suite_version="hidden-v1"))

    blockers = formal_acceptance_blockers(manifest, material_supplied=False)
    assert any("external_connections" in item for item in blockers)
    assert any("Policy" in item for item in blockers)
    assert any("material" in item for item in blockers)
    # This fixture's baseline is verified here, so that is not a blocker for it.
    assert not any("baseline" in item for item in blockers)

    elsewhere = load_manifest(_pack(
        tmp_path, base_commit="a" * 40, suite_version="hidden-v1",
        baseline_status="recorded_elsewhere"))
    assert any("baseline" in item
               for item in formal_acceptance_blockers(elsewhere, material_supplied=True))

    pinned = load_manifest(_pack(
        tmp_path, base_commit="a" * 40, suite_version="hidden-v1", external="none"))
    remaining = formal_acceptance_blockers(pinned, material_supplied=True)
    # The fixture pins no Policy reference, so that is the one gap left; everything else
    # is satisfied, which shows an empty list is reachable rather than hard-coded.
    assert remaining and all("Policy" in item for item in remaining)


def test_a_directory_that_is_not_a_git_checkout_is_a_failed_check_not_a_crash(tmp_path):
    """A tarball checkout is a legitimate thing to hand in; it just fails the pin check."""
    plain = tmp_path / "plain"
    (plain / "src").mkdir(parents=True)

    result = check_checkout(plain, "a" * 40, python_executable=sys.executable,
                            pythonpath_relative="src", import_name="json")

    assert result["ok"] is False
    assert result["head"] is None
    assert any("cannot be read as a Git repository" in problem
               for problem in result["problems"])
