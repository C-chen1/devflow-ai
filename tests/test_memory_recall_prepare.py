"""The rehearsal preparation script builds one repository, one baseline and two Tasks.

Everything here is constructed: the repository and calibration copies are created in a temporary
directory, the evaluation material is a fixture, and no model is called. What is checked is that
the two problems really are distinct and open at the shared baseline, that the adapter honours the
entry's stdin/mounted-workspace contract, that the material is internally consistent and that the
recall scope conditions hold — not that a real source task exists or that recall improves anything.
The container calibration itself is only *attempted*: without Docker (or the locally built image)
the entry reports ``unavailable`` and that is what is asserted, never a pass derived from files.
"""
import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import text

from devflow_ai.benchmark_run import BenchmarkRunError, load_independent_material
from devflow_ai.coordinator import RunCoordinator
from devflow_ai.enums import TaskStatus
from devflow_ai.memory import MemoryScopeError, MemoryService, RepositoryScope
from devflow_ai.models import Task
from devflow_ai.task_pack import (
    admission_has_scope_digest, load_manifest, optional_scope_binding_matches,
    scope_evidence_sha256,
)
from rehearsals.memory_recall import prepare as rehearsal

ENVIRONMENT = "dev-box-win11-py311"
# task_reviews is a prerequisite of memory_items; the rest are the memory tables themselves.
MEMORY_REVISIONS = ("b7f2c31d8a90", "c4a7e2f19b30", "d18e42f70a91", "ec624a31d740",
                    "f6a92c3184d2", "a5810e46f2c9", "b41e9d2c8370")
SCRIPTS = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations"))


@pytest.fixture
def rehearsal_factory(worker_session_factory):
    """Tasks + Runs + reviews + artifacts + the memory tables, in this test's schema."""
    engine = worker_session_factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    with engine.begin() as connection:
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        with Operations.context(MigrationContext.configure(connection)):
            for revision in MEMORY_REVISIONS:
                SCRIPTS.get_revision(revision).module.upgrade()
    return worker_session_factory


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8", timeout=60).stdout.strip()


@pytest.fixture
def prepared(tmp_path):
    return rehearsal.prepare(tmp_path / "rehearsal")


def test_prepare_uses_one_repository_and_one_baseline_for_both_tasks(prepared):
    source, target = prepared["tasks"]["source"], prepared["tasks"]["target"]
    repository = prepared["repository"]

    # One repository, one baseline commit, two different Tasks on it.
    assert source["repo"] == target["repo"] == repository["repo"]
    assert source["base_commit"] == target["base_commit"] == repository["base_commit"]
    assert source["title"] != target["title"]
    assert _git(Path(repository["repo"]), "rev-parse", "HEAD") == repository["base_commit"]
    assert (Path(repository["repo"]) / "src" / "pricing.py").exists()

    # Two packs, both pinned to that same baseline, with distinct public checks.
    manifests = {}
    for entry in (source, target):
        manifest = load_manifest(Path(entry["pack"]))
        manifests[entry["role"]] = manifest
        assert manifest.base_commit == repository["base_commit"]
        assert manifest.scope.allowed_paths == ("src/",)
        assert manifest.scope.forbidden_paths == ("benchmarks/",)
        assert any(item.field.startswith("policy") for item in manifest.unverified)
    source_tests = (Path(source["pack"]) / "scenarios.py").read_text(encoding="utf-8")
    target_tests = (Path(target["pack"]) / "scenarios.py").read_text(encoding="utf-8")
    assert source_tests != target_tests
    assert manifests["target"].independent_suite is None
    assert any(item.field.startswith("independent_suite")
               for item in manifests["target"].unverified)
    suite = manifests["source"].independent_suite
    assert suite is not None
    assert suite.suite_version == prepared["material"]["suite_version"]
    assert suite.materials_sha256 == prepared["material"]["sha256"]

    plan = json.loads((Path(prepared["root"]) / "plan.json").read_text(encoding="utf-8"))
    assert set(plan["tasks"]) == {"source", "target"}
    assert plan["pending_review"]


def test_calibration_workspaces_are_different_commits_and_material_names_the_corrected_one(
        prepared):
    calibration = prepared["material"]["calibration"]
    baseline = Path(calibration["baseline_workspace"])
    corrected = Path(calibration["corrected_workspace"])

    # The two workspaces differ in commit: the corrected one carries the fix.
    assert calibration["baseline_commit"] != calibration["corrected_commit"]
    assert _git(baseline, "rev-parse", "HEAD") == calibration["baseline_commit"]
    assert _git(corrected, "rev-parse", "HEAD") == calibration["corrected_commit"]
    assert "if not prices" not in (baseline / "src" / "pricing.py").read_text(encoding="utf-8")
    assert "if not prices" in (corrected / "src" / "pricing.py").read_text(encoding="utf-8")
    # The prepared working repository keeps its baseline commit; only the copy is corrected.
    assert _git(Path(prepared["repository"]["repo"]), "rev-parse", "HEAD") == (
        calibration["baseline_commit"])

    payload = json.loads(Path(prepared["material"]["path"]).read_text(encoding="utf-8"))
    assert payload["calibration"]["expected_commit"] == calibration["corrected_commit"]

    # The entry loader accepts it against the pack pin. **The calibrated run itself has not been
    # executed**: that needs the evaluation container, and this test does not claim it happened.
    manifest = load_manifest(Path(prepared["tasks"]["source"]["pack"]))
    material, suite_version = load_independent_material(
        Path(prepared["material"]["path"]),
        expected_suite_version=manifest.independent_suite.suite_version,
        expected_sha256=manifest.independent_suite.materials_sha256)
    assert suite_version == rehearsal.SUITE_VERSION and len(material.cases) == 1


def _run_scenarios(pack: Path, workspace: Path) -> subprocess.CompletedProcess:
    """Run one pack's public check against a checkout, the way the entry does (pack outside it)."""
    env = {**os.environ, "PYTHONPATH": str(workspace), "PYTHONDONTWRITEBYTECODE": "1"}
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        env.pop(name, None)
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "scenarios.py"],
        cwd=pack, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)


def test_the_two_tasks_are_distinct_open_defects_on_the_same_baseline(prepared):
    repository = Path(prepared["repository"]["repo"])
    corrected = Path(prepared["material"]["calibration"]["corrected_workspace"])
    source_pack = Path(prepared["tasks"]["source"]["pack"])
    target_pack = Path(prepared["tasks"]["target"]["pack"])

    source_text = (source_pack / "scenarios.py").read_text(encoding="utf-8")
    target_text = (target_pack / "scenarios.py").read_text(encoding="utf-8")
    assert "average_price" in source_text and "slugify" not in source_text
    assert "slugify" in target_text and "average_price" not in target_text

    # Both defects are open at the shared baseline, so neither Task is already solved.
    assert _run_scenarios(source_pack, repository).returncode != 0
    assert _run_scenarios(target_pack, repository).returncode != 0
    # The correction that calibrates the source suite leaves the target defect open: what the
    # first Task can hand over is the repository's layout and public checks, not its own answer.
    assert _run_scenarios(source_pack, corrected).returncode == 0
    assert _run_scenarios(target_pack, corrected).returncode != 0


def test_calibration_is_attempted_through_the_existing_entry_and_never_asserted(prepared):
    """Call the real ``calibrate_stdio_suite`` and record whatever it reports."""
    result = rehearsal.calibrate(prepared)
    assert result["attempted"] is True
    assert result["image"] == prepared["adapter"]["image"]
    if result["outcome"] == "verified":
        assert (result["baseline_outcome"], result["corrected_outcome"]) == ("failed", "passed")
        assert result["image_id"] and result["image_id"].startswith("sha256:")
    else:
        # No Docker daemon in this environment, or the local image was never built. The entry
        # reports that; the preparation script records it instead of claiming a calibration.
        assert result["outcome"] == "unavailable"
        assert result["image_id"] is None
        assert (result["baseline_outcome"], result["corrected_outcome"]) == (
            "unavailable", "unavailable")

    plan = json.loads((Path(prepared["root"]) / "plan.json").read_text(encoding="utf-8"))
    # ``prepare`` alone does not write a calibration result; only ``--calibrate`` does.
    assert plan["calibration_result"] is None
    assert any("calibrat" in item for item in plan["pending_review"])


def test_the_adapter_uses_the_material_cases_against_the_mounted_workspace(prepared):
    payload = json.loads(Path(prepared["material"]["path"]).read_text(encoding="utf-8"))
    adapter = prepared["adapter"]
    dockerfile = Path(adapter["dockerfile"]).read_text(encoding="utf-8")

    # The adapter lives in the image, built locally and used with --pull never; the cases still
    # arrive on stdin and the candidate code is the only thing mounted into the container.
    assert Path(adapter["adapter"]).exists()
    assert "COPY adapter.py /suite/adapter.py" in dockerfile
    assert "FROM python:3.11-slim" in dockerfile
    assert payload["image"] == adapter["image"]
    assert payload["command"] == ["python", "/suite/adapter.py"]

    case = payload["cases"][0]
    stdin = base64.b64decode(case["stdin_base64"])
    expected = base64.b64decode(case["stdout_base64"])
    calibration = payload["calibration"]

    def run(workspace: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, adapter["adapter"]], input=stdin,
            env={**os.environ, "DEVFLOW_CANDIDATE_DIR": workspace},
            capture_output=True, timeout=60)

    rejected = run(calibration["baseline_workspace"])
    accepted = run(calibration["corrected_workspace"])
    # A wrong implementation has to be a wrong answer, not a crash: the entry treats a non-zero
    # exit as "unavailable", which would make a broken baseline look like a broken harness.
    assert rejected.returncode == 0 and accepted.returncode == 0
    # This check runs with the host Python; Windows emits CRLF. The Docker evaluator still
    # compares the container's raw stdout with the trusted expected bytes.
    rejected_stdout = rejected.stdout.replace(b"\r\n", b"\n")
    accepted_stdout = accepted.stdout.replace(b"\r\n", b"\n")
    assert rejected_stdout != expected and accepted_stdout == expected


def test_constructed_material_is_accepted_by_the_entry_placement_check(prepared, tmp_path):
    from devflow_ai.independent_evaluation import (
        MaterialPlacementError, require_material_placement,
    )

    source = prepared["tasks"]["source"]
    material_path = Path(prepared["material"]["path"])
    workspaces = tmp_path / "workspaces"
    artifacts = tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()

    require_material_placement(material_path, pack_dir=Path(source["pack"]),
                               workspaces_root=workspaces, artifacts_root=artifacts,
                               repository=source["repo"])

    inside = Path(source["pack"]) / "material.json"
    shutil.copyfile(material_path, inside)
    try:
        with pytest.raises(MaterialPlacementError):
            require_material_placement(inside, pack_dir=Path(source["pack"]),
                                       workspaces_root=workspaces, artifacts_root=artifacts,
                                       repository=source["repo"])
    finally:
        inside.unlink(missing_ok=True)


def test_one_changed_byte_of_the_material_is_refused(prepared):
    manifest = load_manifest(Path(prepared["tasks"]["source"]["pack"]))
    original = Path(prepared["material"]["path"])
    tampered = original.with_name(original.name + ".tampered")
    shutil.copyfile(original, tampered)
    try:
        tampered.write_bytes(tampered.read_bytes() + b" ")
        with pytest.raises(BenchmarkRunError, match="materials_sha256"):
            load_independent_material(
                tampered,
                expected_suite_version=manifest.independent_suite.suite_version,
                expected_sha256=manifest.independent_suite.materials_sha256)
    finally:
        tampered.unlink(missing_ok=True)


def test_admit_tasks_creates_two_eligible_development_tasks(prepared, rehearsal_factory):
    factory = rehearsal_factory
    admitted = rehearsal.admit_tasks(factory, prepared, operator="operator-a")

    with factory() as session:
        repos, bases = set(), set()
        for role in ("source", "target"):
            record = admitted[role]
            task = session.get(Task, UUID(record["task_id"]))
            assert record["created"] is True and record["admission"] == "admitted"
            assert task is not None and task.status is TaskStatus.ELIGIBLE
            assert task.policy_version and task.policy_snapshot
            evidence = task.admission_record["evidence"]
            assert evidence == [f"dev-task:operator-a:{rehearsal.ADMISSION_REASON}"]
            assert not any(item.startswith("pack:") for item in evidence)
            repos.add(task.repo)
            bases.add(task.base_commit)
        # The two Tasks share one repository and one baseline: what recall scope needs.
        assert len(repos) == 1 and len(bases) == 1

    again = rehearsal.admit_tasks(factory, prepared, operator="operator-a")
    for role in ("source", "target"):
        assert again[role]["task_id"] == admitted[role]["task_id"]
        assert again[role]["created"] is False


def test_scope_gate_accepts_the_shared_repository_and_refuses_another_one(
        prepared, rehearsal_factory, tmp_path):
    from devflow_ai.memory import _is_known_benchmark

    factory = rehearsal_factory
    admitted = rehearsal.admit_tasks(factory, prepared, operator="operator-a")
    target_id = UUID(admitted["target"]["task_id"])
    with factory() as session:
        target = session.get(Task, target_id)
        assert _is_known_benchmark(target) is False
        repository = target.repo
    with factory() as session:
        run, _ = RunCoordinator(session).start_initial_run(target_id, "rehearsal-worker", "scope-key")
        run_id = run.id

    common = dict(task_id=target_id, run_id=run_id, memory_id=uuid4(),
                  candidate_sha256="0" * 64, operator="operator-a",
                  purpose="scope gate check", target_environment=ENVIRONMENT)
    # Same repository as the Task: the scope gate passes and the lookup fails later, which is the
    # evidence that a recall could match this target at all.
    with factory() as session:
        with pytest.raises(MemoryScopeError, match="Candidate missing"):
            MemoryService(session).prepare_recall(
                scope=RepositoryScope(repository), idempotency_key=uuid4(), **common)
    # A different repository is refused before any candidate is considered.
    other_repo = str(tmp_path / "another-repository")
    with factory() as session:
        with pytest.raises(MemoryScopeError, match="Target Task missing"):
            MemoryService(session).prepare_recall(
                scope=RepositoryScope(other_repo), idempotency_key=uuid4(), **common)


def test_constructed_source_is_previewed_and_recalled_by_the_other_task_on_the_same_repo(
        prepared, rehearsal_factory):
    """A *constructed* source drives real preview and recall across the two Tasks.

    The Task, the PASSED Run and the ``trusted_v1`` Verification are rows this test writes, as
    ``tests/test_memory.py`` does: they exist so the recall rules can be exercised. They are not
    evidence that any task passed, and the resulting memory is still ``unverified_candidate``.
    """
    from datetime import datetime, timedelta, timezone

    from devflow_ai.enums import RunStatus, VerificationResult
    from devflow_ai.memory import MemoryCandidate
    from devflow_ai.models import Run, Verification
    from devflow_ai.services import AdmissionRecord, TaskService
    from devflow_ai.worker import Worker

    factory = rehearsal_factory
    repository = prepared["repository"]["repo"]
    base_commit = prepared["repository"]["base_commit"]

    with factory() as session:
        task, _ = TaskService(session).create_task(
            title="Constructed rehearsal source", description="Constructed source Task",
            repo=repository, idempotency_key=uuid4())
        source_id = task.id
    with factory() as session:
        TaskService(session).apply_admission_decision(
            source_id, TaskStatus.ELIGIBLE, base_commit=base_commit,
            admission_record=AdmissionRecord(
                reason="fixture", evidence=["dev-task:operator-a:fixture"]))
    source_run = Worker(factory, f"source-{uuid4().hex[:8]}").claim_once().run_id
    with factory.begin() as session:
        session.get(Run, source_run).status = RunStatus.PASSED
        verification = Verification(run_id=source_run, result=VerificationResult.PASSED,
                                    evidence={"scope": "trusted_v1",
                                              "suite_version": rehearsal.SUITE_VERSION})
        session.add(verification)
        session.flush()
        verification_id = verification.id

    with factory() as session:
        ingested = MemoryService(session).ingest(MemoryCandidate(
            identity_key="rehearsal-repo-layout", scope=RepositoryScope(repository=repository),
            source_kind="verification", task_id=source_id, run_id=source_run,
            verification_id=verification_id,
            content="Code lives under src/; public checks run pytest on scenarios.py outside "
                    "the checkout, so a fix belongs in src/ only.",
            topic="repo-layout", valid_until=datetime.now(timezone.utc) + timedelta(days=30),
            applicable_base_commit=base_commit, applicable_environment=ENVIRONMENT,
            created_by="operator-a"))
    memory_id = UUID(ingested["id"])
    assert ingested["content_status"] == "unverified_candidate"
    with factory() as session:
        shown = MemoryService(session).read(scope=RepositoryScope(repository=repository),
                                            memory_id=memory_id)
    with factory() as session:
        MemoryService(session).review(memory_id=memory_id,
                                      scope=RepositoryScope(repository=repository),
                                      candidate_sha256=shown["candidate_sha256"],
                                      conclusion="accepted", reviewer="reviewer-a",
                                      reason="constructed rehearsal text")

    admitted = rehearsal.admit_tasks(factory, prepared, operator="operator-a")
    target_id = UUID(admitted["target"]["task_id"])
    with factory() as session:
        run, _ = RunCoordinator(session).start_initial_run(
            target_id, "target-worker", f"target-key-{uuid4()}")
        target_run = run.id

    # Preview: the other Task, on the shared repository and baseline, sees the candidate.
    with factory() as session:
        preview = MemoryService(session).inspect_candidates(
            scope=RepositoryScope(repository=repository), target_task_id=target_id,
            topic="repo-layout", target_environment=ENVIRONMENT, limit=100)
    assert [item["id"] for item in preview] == [ingested["id"]]
    checks = preview[0]["candidate_checks"]
    assert checks["base_commit_matches"] is True
    assert checks["environment_declaration_matches"] is True
    assert checks["eligible_to_request_use"] is True
    # Without the environment label a preview can never look usable; that is not a recall rule.
    with factory() as session:
        unknown_environment = MemoryService(session).inspect_candidates(
            scope=RepositoryScope(repository=repository), target_task_id=target_id,
            topic="repo-layout", limit=100)
    assert unknown_environment[0]["candidate_checks"]["eligible_to_request_use"] is False
    # A different repository is refused before any candidate is considered.
    with factory() as session:
        with pytest.raises(MemoryScopeError, match="outside the declared scope"):
            MemoryService(session).inspect_candidates(
                scope=RepositoryScope(repository=str(prepared["root"]) + "/elsewhere"),
                target_task_id=target_id, topic="repo-layout", target_environment=ENVIRONMENT)

    # Recall: the declaration is recorded against the target Run, and the candidate comes back.
    with factory() as session:
        recalled = MemoryService(session).prepare_recall(
            idempotency_key=uuid4(), scope=RepositoryScope(repository=repository),
            task_id=target_id, run_id=target_run, memory_id=memory_id,
            candidate_sha256=shown["candidate_sha256"], operator="operator-a",
            purpose="constructed cross-task recall", target_environment=ENVIRONMENT)
    assert recalled["candidate"]["content"] == shown["content"]
    assert recalled["candidate"]["content_status"] == "unverified_candidate"
    assert recalled["candidate"]["candidate_sha256"] == recalled["use"]["candidate_sha256"]
    assert recalled["use"]["kind"] == "operator_declared"


def test_development_admission_evidence_carries_no_pack_marker(prepared):
    manifest = load_manifest(Path(prepared["tasks"]["source"]["pack"]))
    evidence = [f"dev-task:operator-a:{rehearsal.ADMISSION_REASON}"]
    admission = {"reason": rehearsal.ADMISSION_REASON, "evidence": evidence}

    assert not any(item.startswith("pack:") for item in evidence)
    # No scope digest either, so the scope Skill cannot be claimed to apply to these Tasks.
    assert admission_has_scope_digest(admission, scope_evidence_sha256(manifest.scope)) is False
    assert optional_scope_binding_matches(admission, manifest) is True


def _root_snapshot(root: Path) -> dict:
    """Every prepared byte except plan.json and the Git metadata inside the repositories."""
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file() and path.name != "plan.json" and ".git" not in path.parts}


def test_calibration_can_be_rerun_on_a_prepared_root_only_while_it_is_unchanged(prepared):
    """The image is built from ``<root>/adapter``, so calibration has to be re-runnable."""
    root = Path(prepared["root"])
    before = _root_snapshot(root)

    outcome = rehearsal.calibrate_prepared(root)
    result = outcome["calibration"]
    assert result["attempted"] is True
    assert result["image"] == prepared["adapter"]["image"]
    assert result["prerequisite"] in {"docker_missing", "image_missing", "present", "unknown"}
    assert _root_snapshot(root) == before
    plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    assert plan["calibration_result"] == result

    # Any change to what the calibration would run against is refused, not silently calibrated.
    adapter = Path(prepared["adapter"]["adapter"])
    original = adapter.read_bytes()
    adapter.write_bytes(original + b"\n# edited\n")
    try:
        with pytest.raises(SystemExit, match="adapter changed"):
            rehearsal.calibrate_prepared(root)
    finally:
        adapter.write_bytes(original)

    baseline = Path(prepared["material"]["calibration"]["baseline_workspace"])
    source = baseline / "src" / "pricing.py"
    original_source = source.read_bytes()
    source.write_bytes(original_source + b"\n# uncommitted change\n")
    with pytest.raises(SystemExit, match="uncommitted or untracked"):
        rehearsal.calibrate_prepared(root)
    source.write_bytes(original_source)
    _git(baseline, "-c", "user.name=Rehearsal Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", "extra")
    with pytest.raises(SystemExit, match="recorded commit"):
        rehearsal.calibrate_prepared(root)


def test_the_cli_calibrates_a_prepared_root_without_preparing_it_again(prepared):
    root = Path(prepared["root"])
    before = _root_snapshot(root)

    assert rehearsal.main(["--root", str(root), "--calibrate"]) == 0

    assert _root_snapshot(root) == before
    plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    assert plan["calibration_result"]["attempted"] is True
    assert plan["calibration_result"]["outcome"] in {"verified", "unavailable", "invalid"}


def test_confirm_is_disabled_and_a_non_empty_root_is_refused(prepared, tmp_path):
    # The CLI refuses to write Tasks until the isolation review finishes, and creates nothing.
    fresh_root = tmp_path / "confirm-refused"
    assert rehearsal.main(["--root", str(fresh_root), "--confirm", "--database", "anything"]) == 2
    assert not fresh_root.exists()
    # A prepared root is not reused: the script only writes into a missing or empty directory.
    with pytest.raises(SystemExit, match="non-empty rehearsal root"):
        rehearsal.prepare(Path(prepared["root"]))
