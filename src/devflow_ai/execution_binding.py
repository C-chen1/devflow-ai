"""Verify the pinned execution manifest before a Run is created for it.

This is the smallest version binding this deployment can actually check. The trusted control
plane fixes a file list and its canonical digest; the entry verifies **those bytes under a given
root** before it claims any work, and the verified digest is written to the Run row inside the
claim transaction. Missing or changed files, a manifest digest that differs from the pinned one,
or an unreadable manifest refuse the start — they never fall back to "unknown" silently.

What this does **not** prove, and must not be described as proving:

* that the process writing the digest is the one that ran the code. A process can state a digest
  it did not verify; this check only makes an honest entry refuse to start on a mismatch;
* that the manifest lists every file the process will import. The caller chooses the list; the
  inventory does not discover dependencies. Importing an unlisted module is possible;
* anything about the model provider, the workspace the agent later edits, or the evaluation
  environment. ``Task.base_commit`` pins the target repository baseline and stays a separate
  fact from this one.

The manifest itself is trusted control-plane material: it and its digest must reach the entry
through a channel the operator controls, and neither belongs in a task pack, an image, a log or
an agent context.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from re import fullmatch

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select

from devflow_ai.execution_manifest import (
    FileManifest, ManifestError, manifest_sha256, verify_manifest,
)
from devflow_ai.models import Run

_SHA256 = r"[0-9a-f]{64}"


class ExecutionBindingError(ValueError):
    """The pinned manifest cannot be verified against the artifacts it names."""


@dataclass(frozen=True)
class ExecutionBinding:
    """The digest a Run may record, and what was actually checked to earn it."""

    sha256: str
    file_count: int
    root: str


def package_modules(root: Path | str) -> list[str]:
    """Every Python module under ``root``, as normalized relative POSIX paths.

    This is what an inventory has to cover for the version claim to mean anything: the process
    can import a module the manifest never listed, so "these bytes match" only describes the
    program if the listing is complete.
    """
    base = Path(root)
    paths = []
    for path in base.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(base).as_posix()
        if any(part in ("", ".", "..") for part in relative.split("/")):
            continue
        paths.append(relative)
    return sorted(paths)


def loaded_package_modules(root: Path | str) -> tuple[list[str], list[str]]:
    """What this process actually imported from the package, by file.

    Returns ``(relative_paths_under_root, module_names_imported_elsewhere)``. The second list is
    the interesting one: a ``devflow_ai`` module loaded from outside the verified directory means
    the inventory describes some other copy of the program. What this cannot see: whether the
    code objects in memory still match the file on disk (they were read earlier), bytecode cache
    integrity, or any third-party dependency.
    """
    base = Path(root).resolve()
    inside: list[str] = []
    outside: list[str] = []
    for name, module in list(sys.modules.items()):
        if name != "devflow_ai" and not name.startswith("devflow_ai."):
            continue
        filename = getattr(module, "__file__", None)
        if not filename:
            continue
        try:
            path = Path(filename).resolve()
        except OSError:  # pragma: no cover - unreadable module path
            outside.append(name)
            continue
        if path == base or base in path.parents:
            inside.append(path.relative_to(base).as_posix())
        else:
            outside.append(name)
    return sorted(set(inside)), sorted(set(outside))


def load_execution_manifest(path: Path | str) -> FileManifest:
    """Read a manifest written by ``execution_manifest create``; never echo its contents."""
    manifest_path = Path(path)
    try:
        raw = manifest_path.read_bytes()
    except OSError as error:
        raise ExecutionBindingError(
            f"Execution manifest is unreadable: {type(error).__name__}") from None
    try:
        return FileManifest.model_validate_json(raw)
    except (ValidationError, ValueError):
        raise ExecutionBindingError(
            "Execution manifest is not a valid file inventory") from None


def running_package_root() -> Path:
    """The directory of the package this process actually imported.

    The default root is deliberately not settable from the command line: verifying some other
    directory would prove nothing about the code that is running. A process that re-deploys
    files under itself after import is outside this check's reach.
    """
    import devflow_ai

    return Path(devflow_ai.__file__).resolve().parent


def verify_execution_binding(
    *, manifest_path: Path | str, expected_sha256: str, root: Path | str | None = None,
    require_complete: bool = False,
) -> ExecutionBinding:
    """Verify the pinned manifest and the listed bytes under ``root``.

    ``expected_sha256`` must come from the trusted control plane, separately from the manifest
    file: a digest shipped next to the manifest only proves the two files agree. ``root``
    defaults to the running package directory; tests pass an explicit one.

    ``require_complete`` also refuses an inventory that leaves out a Python module under the
    root, and a ``devflow_ai`` module this process imported from outside that root. Without it
    the digest still only describes the listed files, and an unlisted module can be imported.

    What no option here can prove: that the bytes in memory are the bytes on disk (they were
    read before this check), that the bytecode cache is untampered, that third-party packages
    match any recorded version, or that the process is honest about the digest it reports.
    """
    if not isinstance(expected_sha256, str) or not fullmatch(_SHA256, expected_sha256):
        raise ExecutionBindingError(
            "expected_sha256 must be 64 lowercase hexadecimal characters")
    manifest = load_execution_manifest(manifest_path)
    declared = manifest_sha256(manifest)
    if declared != expected_sha256:
        raise ExecutionBindingError(
            "Execution manifest does not match the pinned digest; refusing to start")
    target = Path(root) if root is not None else running_package_root()
    try:
        verify_manifest(target, manifest, expected_sha256)
    except ManifestError as error:
        raise ExecutionBindingError(str(error)) from None
    except OSError as error:  # pragma: no cover - verify_manifest wraps its own failures
        raise ExecutionBindingError(
            f"Execution artifacts cannot be read: {type(error).__name__}") from None
    if require_complete and target.resolve() == running_package_root():
        # Only meaningful for the package this process is running: comparing the imported
        # modules with some other directory would report every real module as "outside".
        _loaded, outside = loaded_package_modules(target)
        if outside:
            shown = ", ".join(outside[:5])
            more = "" if len(outside) <= 5 else f" (+{len(outside) - 5} more)"
            raise ExecutionBindingError(
                f"{len(outside)} package module(s) were imported from outside the verified "
                f"directory: {shown}{more}")
    if require_complete:
        listed = {entry.path for entry in manifest.files}
        unlisted = [path for path in package_modules(target) if path not in listed]
        if unlisted:
            shown = ", ".join(unlisted[:5])
            more = "" if len(unlisted) <= 5 else f" (+{len(unlisted) - 5} more)"
            raise ExecutionBindingError(
                f"Execution inventory leaves out {len(unlisted)} package module(s): "
                f"{shown}{more}; an unlisted module can still be imported")
    return ExecutionBinding(
        sha256=declared, file_count=len(manifest.files),
        root=str(target.resolve()))


def verify_optional_binding(
    *, manifest_path: Path | str | None, expected_sha256: str | None,
    require_complete: bool = False,
) -> str | None:
    """Apply the "both or neither" rule every entry shares.

    Returns the verified digest, or None when the caller pinned nothing (recorded as unknown).
    A digest without a manifest cannot be verified; a manifest without an independently
    supplied digest only proves the file agrees with itself. Both refusals are raised here so
    every entry behaves the same way.
    """
    if manifest_path is None and expected_sha256 is None:
        return None
    if manifest_path is None or expected_sha256 is None:
        raise ExecutionBindingError(
            "execution_manifest and execution_sha256 must be supplied together")
    return verify_execution_binding(
        manifest_path=manifest_path, expected_sha256=expected_sha256,
        require_complete=require_complete).sha256


def verify_task_version(
    session, task_id: UUID, *, manifest_path: Path | str, artifact_root: Path | str,
) -> ExecutionBinding:
    """Re-verify the execution version one Task recorded, from an artifact copy.

    Rollback means choosing an older, still verifiable artifact for a **new** Task: a Task's
    own history may not switch versions. This is the read-only check that makes "choose it"
    mean something — the Task's Runs must all carry one recorded digest, and the artifact copy
    the operator points at must match the manifest whose digest is exactly that one. An unknown
    history, a mixed history, a manifest for another version, and changed or missing files are
    all refusals; nothing here falls back to "the newest artifact that happens to be around".

    What it proves: the bytes under ``artifact_root`` match the listed files, and the manifest
    is the one this Task recorded. What it does not prove: that the process which will run them
    is that copy (the entry verifies the *running* package and records the digest itself), that
    the inventory is complete (pass a complete manifest), or that the new Run will use it. It
    also says nothing about the model, the workspace the agent edits, or evaluation materials.
    """
    runs = session.scalars(select(Run.execution_sha256).where(Run.task_id == task_id)).all()
    recorded = {digest for digest in runs if digest is not None}
    if not recorded:
        raise ExecutionBindingError(
            "This Task's Runs recorded no execution version; an unknown version cannot be "
            "selected as an artifact to roll back to")
    if any(digest is None for digest in runs):
        raise ExecutionBindingError(
            "This Task has Runs with an unknown execution version; its history is ambiguous")
    if len(recorded) > 1:
        raise ExecutionBindingError(
            "This Task's Runs record more than one execution version; the artifact to roll "
            "back to is ambiguous")
    pinned = recorded.pop()
    return verify_execution_binding(
        manifest_path=manifest_path, expected_sha256=pinned, root=artifact_root)


def main(argv: list[str] | None = None) -> int:
    """Verify one pinned manifest without touching the database or starting anything."""
    parser = argparse.ArgumentParser(
        description="Verify a pinned execution manifest against local artifacts")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        binding = verify_execution_binding(
            manifest_path=args.manifest, expected_sha256=args.expected_sha256,
            root=args.root)
    except ExecutionBindingError as error:
        print(f"Execution binding failed: {error}", file=sys.stderr)
        return 1
    # Only counts and the digest: the file list itself stays with the control plane.
    print(json.dumps({"execution_sha256": binding.sha256,
                      "verified_listed_files": binding.file_count}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
