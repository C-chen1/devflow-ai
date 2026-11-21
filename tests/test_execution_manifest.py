import json
import os
import subprocess
import sys

import pytest
from pydantic import ValidationError

from devflow_ai.execution_manifest import (
    FileManifest, ManifestError, build_manifest, manifest_sha256, verify_manifest,
)


def test_inventory_is_stable_and_detects_changes(tmp_path):
    (tmp_path / "code.py").write_bytes(b"code\n")
    (tmp_path / "lock.txt").write_bytes(b"dependency\n")
    first = build_manifest(tmp_path, ["lock.txt", "code.py"])
    second = build_manifest(tmp_path, ["code.py", "lock.txt"])
    assert first == second
    digest = manifest_sha256(first)
    parsed = FileManifest.model_validate_json(json.dumps(first.model_dump(mode="json")))
    verify_manifest(tmp_path, parsed, digest)
    (tmp_path / "code.py").write_bytes(b"changed\n")
    with pytest.raises(ManifestError, match="file digest"):
        verify_manifest(tmp_path, parsed, digest)
    (tmp_path / "code.py").unlink()
    with pytest.raises(ManifestError, match="cannot be read"):
        verify_manifest(tmp_path, parsed, digest)


def test_manifest_reference_and_structure_are_checked(tmp_path):
    (tmp_path / "file").write_bytes(b"data")
    manifest = build_manifest(tmp_path, ["file"])
    with pytest.raises(ManifestError, match="trusted reference"):
        verify_manifest(tmp_path, manifest, "0" * 64)
    data = manifest.model_dump(mode="json")
    altered = FileManifest.model_validate({**data, "files": [{"path": "file", "sha256": "1" * 64}]})
    with pytest.raises(ManifestError, match="trusted reference"):
        verify_manifest(tmp_path, altered, manifest_sha256(manifest))
    with pytest.raises(ValidationError):
        FileManifest.model_validate({**data, "unexpected": "value"})
    with pytest.raises(ValidationError):
        FileManifest.model_validate({**data, "files": data["files"] * 2})
    with pytest.raises(ManifestError, match="distinct"):
        build_manifest(tmp_path, ["file", "file"])


@pytest.mark.parametrize("path", ["../secret", "/absolute", "a/../b", "a//b", "a/./b", "C:/x", "a\\b"])
def test_inventory_rejects_unsafe_paths_before_reading(tmp_path, path):
    with pytest.raises(ValidationError):
        build_manifest(tmp_path, [path])


def test_inventory_rejects_symbolic_link(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.write_bytes(b"outside")
    link = tmp_path / "link"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic link creation unavailable")
    with pytest.raises(ManifestError, match="Symbolic links"):
        build_manifest(tmp_path, ["link"])


def test_inventory_rejects_intermediate_symbolic_link(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "code.py").write_bytes(b"code")
    try:
        (tmp_path / "alias").symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic link creation unavailable")
    with pytest.raises(ManifestError, match="Symbolic links"):
        build_manifest(tmp_path, ["alias/code.py"])


def test_inventory_does_not_claim_unlisted_files_are_checked(tmp_path):
    (tmp_path / "listed").write_bytes(b"unchanged")
    manifest = build_manifest(tmp_path, ["listed"])
    (tmp_path / "unlisted").write_bytes(b"new")
    verify_manifest(tmp_path, manifest, manifest_sha256(manifest))


@pytest.mark.skipif(os.name != "nt", reason="Windows junction test")
def test_inventory_rejects_windows_directory_junction(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "code.py").write_bytes(b"code")
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(tmp_path / "alias"), str(real)],
                            capture_output=True, check=False)
    if result.returncode:
        pytest.skip("directory junction creation unavailable")
    with pytest.raises(ManifestError, match="reparse points"):
        build_manifest(tmp_path, ["alias/code.py"])


def test_cli_create_verify_and_reject_changed_files(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "code.py").write_text("fixture", encoding="utf-8")
    command = [sys.executable, "-m", "devflow_ai.execution_manifest"]
    created = subprocess.run(command + ["create", "--root", str(root), "--file", "code.py"],
                             text=True, capture_output=True, check=False)
    assert created.returncode == 0
    digest = created.stderr.strip().removeprefix("manifest_sha256=")
    assert digest == manifest_sha256(FileManifest.model_validate_json(created.stdout))
    inventory = tmp_path / "manifest.json"
    inventory.write_text(created.stdout, encoding="utf-8")
    verify = command + ["verify", "--root", str(root), "--manifest", str(inventory),
                        "--expected-sha256", digest]
    result = subprocess.run(verify, text=True, capture_output=True, check=False)
    assert result.returncode == 0 and json.loads(result.stdout) == {"verified_listed_files": 1}
    (root / "code.py").write_text("changed", encoding="utf-8")
    result = subprocess.run(verify, text=True, capture_output=True, check=False)
    assert result.returncode == 1 and not result.stdout
    inventory.write_text('{"secret":"do-not-print"}', encoding="utf-8")
    result = subprocess.run(verify, text=True, capture_output=True, check=False)
    assert result.returncode == 1 and "do-not-print" not in result.stderr
