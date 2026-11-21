"""The pinned execution manifest must be verified against the bytes that are running.

These tests use a temporary directory as the package root. What is real is the check: the
manifest digest, the file digests, and the refusal on any mismatch. Nothing here proves that a
process ran the code it verified — that limit is stated in the module and in the docs.
"""
import json
from hashlib import sha256
from pathlib import Path

import pytest

from devflow_ai.execution_binding import (
    ExecutionBindingError, load_execution_manifest, main, verify_execution_binding,
)
from devflow_ai.execution_manifest import build_manifest, manifest_sha256

PIN = "f" * 64


def _package(tmp_path, *, body="VALUE = 1\n"):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "module.py").write_text(body, encoding="utf-8")
    manifest = build_manifest(root, ["module.py"])
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")
    return root, manifest_path, manifest_sha256(manifest)


def test_a_matching_manifest_yields_the_pinned_digest(tmp_path):
    root, manifest_path, digest = _package(tmp_path)

    binding = verify_execution_binding(
        manifest_path=manifest_path, expected_sha256=digest, root=root)

    assert binding.sha256 == digest
    assert binding.file_count == 1
    assert binding.root == str(root.resolve())
    assert load_execution_manifest(manifest_path).files[0].path == "module.py"


def test_a_manifest_that_disagrees_with_the_pin_is_refused(tmp_path):
    root, manifest_path, digest = _package(tmp_path)

    with pytest.raises(ExecutionBindingError, match="pinned digest"):
        verify_execution_binding(
            manifest_path=manifest_path, expected_sha256=PIN, root=root)
    assert digest != PIN


def test_changed_or_missing_artifacts_are_refused(tmp_path):
    root, manifest_path, digest = _package(tmp_path)
    (root / "module.py").write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(ExecutionBindingError, match="digest does not match"):
        verify_execution_binding(
            manifest_path=manifest_path, expected_sha256=digest, root=root)

    (root / "module.py").unlink()
    with pytest.raises(ExecutionBindingError):
        verify_execution_binding(
            manifest_path=manifest_path, expected_sha256=digest, root=root)


def test_an_unusable_manifest_or_pin_is_refused(tmp_path):
    root, manifest_path, digest = _package(tmp_path)
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")

    with pytest.raises(ExecutionBindingError, match="not a valid file inventory"):
        load_execution_manifest(broken)
    with pytest.raises(ExecutionBindingError, match="unreadable"):
        load_execution_manifest(tmp_path / "missing.json")
    with pytest.raises(ExecutionBindingError, match="64 lowercase hexadecimal"):
        verify_execution_binding(
            manifest_path=manifest_path, expected_sha256="F" * 64, root=root)
    with pytest.raises(ExecutionBindingError, match="64 lowercase hexadecimal"):
        verify_execution_binding(
            manifest_path=manifest_path, expected_sha256="short", root=root)


def test_the_cli_reports_only_counts_and_digest(tmp_path, capsys):
    root, manifest_path, digest = _package(tmp_path)

    assert main(["--root", str(root), "--manifest", str(manifest_path),
                 "--expected-sha256", digest]) == 0
    printed = json.loads(capsys.readouterr().out.strip())
    assert printed == {"execution_sha256": digest, "verified_listed_files": 1}

    assert main(["--root", str(root), "--manifest", str(manifest_path),
                 "--expected-sha256", PIN]) == 1
    captured = capsys.readouterr()
    assert captured.out.strip() == ""
    assert "pinned digest" in captured.err
    # The manifest's own contents never reach stdout on failure.
    assert "module.py" not in captured.err


def test_the_digest_is_over_the_manifest_not_the_file_bytes(tmp_path):
    """A manifest re-serialized with different whitespace must not change the pin."""
    root, manifest_path, digest = _package(tmp_path)
    reformatted = tmp_path / "reformatted.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    reformatted.write_text(json.dumps(payload, indent=4), encoding="utf-8")

    binding = verify_execution_binding(
        manifest_path=reformatted, expected_sha256=digest, root=root)
    assert binding.sha256 == digest
    assert sha256(reformatted.read_bytes()).hexdigest() != digest


def test_the_shared_pair_rule_is_both_or_neither(tmp_path):
    """The helper verifies against the running package, so the manifest lists real files."""
    from devflow_ai.execution_binding import running_package_root, verify_optional_binding
    from devflow_ai.execution_manifest import build_manifest, manifest_sha256

    root = running_package_root()
    manifest = build_manifest(root, [name.name for name in sorted(root.glob("*.py"))][:2])
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")
    digest = manifest_sha256(manifest)

    # Neither: no version was pinned, which the caller records as unknown.
    assert verify_optional_binding(manifest_path=None, expected_sha256=None) is None
    assert verify_optional_binding(
        manifest_path=manifest_path, expected_sha256=digest) == digest
    with pytest.raises(ExecutionBindingError, match="must be supplied together"):
        verify_optional_binding(manifest_path=manifest_path, expected_sha256=None)
    with pytest.raises(ExecutionBindingError, match="must be supplied together"):
        verify_optional_binding(manifest_path=None, expected_sha256=digest)
    with pytest.raises(ExecutionBindingError, match="pinned digest"):
        verify_optional_binding(manifest_path=manifest_path, expected_sha256=PIN)


def test_an_incomplete_inventory_is_refused_only_when_completeness_is_required(tmp_path):
    from devflow_ai.execution_binding import package_modules
    from devflow_ai.execution_manifest import build_manifest, manifest_sha256

    root, manifest_path, digest = _package(tmp_path)
    (root / "extra.py").write_text("OTHER = 2\n", encoding="utf-8")
    partial = build_manifest(root, ["module.py"])
    partial_path = tmp_path / "partial.json"
    partial_path.write_text(partial.model_dump_json(), encoding="utf-8")
    partial_digest = manifest_sha256(partial)
    assert package_modules(root) == ["extra.py", "module.py"]

    # Without the flag the digest only describes the listed files — which is the gap.
    assert verify_execution_binding(
        manifest_path=partial_path, expected_sha256=partial_digest, root=root).sha256 == (
        partial_digest)
    with pytest.raises(ExecutionBindingError, match="leaves out 1 package module"):
        verify_execution_binding(
            manifest_path=partial_path, expected_sha256=partial_digest, root=root,
            require_complete=True)

    complete = build_manifest(root, package_modules(root))
    complete_path = tmp_path / "complete.json"
    complete_path.write_text(complete.model_dump_json(), encoding="utf-8")
    binding = verify_execution_binding(
        manifest_path=complete_path, expected_sha256=manifest_sha256(complete), root=root,
        require_complete=True)
    assert binding.file_count == 2
    assert digest != manifest_sha256(complete)


def test_package_modules_skips_caches(tmp_path):
    from devflow_ai.execution_binding import package_modules

    root = tmp_path / "pkg"
    (root / "__pycache__").mkdir(parents=True)
    (root / "__pycache__" / "stale.py").write_text("X = 1\n", encoding="utf-8")
    (root / "module.py").write_text("Y = 2\n", encoding="utf-8")
    (root / "nested").mkdir()
    (root / "nested" / "inner.py").write_text("Z = 3\n", encoding="utf-8")

    assert package_modules(root) == ["module.py", "nested/inner.py"]


def test_loaded_modules_must_come_from_the_verified_directory(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    from devflow_ai.execution_binding import loaded_package_modules, running_package_root
    from devflow_ai.execution_manifest import build_manifest, manifest_sha256

    root = running_package_root()
    inside, outside = loaded_package_modules(root)
    assert inside and "execution_binding.py" in inside
    assert outside == []

    manifest = build_manifest(root, __import__(
        "devflow_ai.execution_binding", fromlist=["package_modules"]).package_modules(root))
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")
    digest = manifest_sha256(manifest)
    assert verify_execution_binding(
        manifest_path=manifest_path, expected_sha256=digest, root=root,
        require_complete=True).sha256 == digest

    # A package module imported from somewhere else means the inventory describes another copy
    # of the program, even though every listed byte still matches.
    monkeypatch.setitem(sys.modules, "devflow_ai._outside_probe",
                        SimpleNamespace(__file__=str(tmp_path / "elsewhere" / "probe.py")))
    with pytest.raises(ExecutionBindingError, match="imported from outside"):
        verify_execution_binding(
            manifest_path=manifest_path, expected_sha256=digest, root=root,
            require_complete=True)
