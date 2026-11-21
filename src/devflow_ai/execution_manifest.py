"""File inventory for a trusted operator, not a runtime version enforcement gate.

Callers choose the files. This module neither discovers dependencies nor proves that
the inventoried files are the ones later loaded by a process.
"""

from __future__ import annotations

import hashlib
import json
import argparse
import stat
import sys
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class ManifestError(ValueError):
    """An inventory or the corresponding files cannot be verified."""


class FileEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        if (not value or "\\" in value or ":" in value or "\x00" in value
                or any(part in ("", ".", "..") for part in value.split("/"))
                or PurePosixPath(value).is_absolute()):
            raise ValueError("Expected a normalized relative POSIX file path")
        return value


class FileManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = Field(default=1, ge=1, le=1, strict=True)
    files: tuple[FileEntry, ...] = Field(min_length=1, max_length=10000)

    @field_validator("files")
    @classmethod
    def sorted_unique_paths(cls, files: tuple[FileEntry, ...]) -> tuple[FileEntry, ...]:
        paths = [entry.path for entry in files]
        if paths != sorted(set(paths)):
            raise ValueError("File paths must be unique and sorted")
        return files


def _digest(root: Path, relative: str) -> str:
    # Reject links in every component; resolve containment before reading any bytes.
    path = root
    try:
        for part in relative.split("/"):
            path = path / part
            info = path.lstat()
            if (stat.S_ISLNK(info.st_mode) or
                    getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise ManifestError("Symbolic links and reparse points are not supported")
        if not path.resolve(strict=True).is_relative_to(root) or not path.is_file():
            raise ManifestError("Inventory entry is not a regular file under the root")
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except (OSError, RuntimeError) as exc:
        raise ManifestError("Inventory file cannot be read") from exc


def _root(path: Path) -> Path:
    try:
        result = Path(path).resolve(strict=True)
        if not result.is_dir():
            raise ManifestError("Inventory root must be a directory")
        return result
    except (OSError, RuntimeError) as exc:
        raise ManifestError("Inventory root cannot be read") from exc


def build_manifest(root: Path, paths: list[str]) -> FileManifest:
    """Hash explicitly selected files. The caller is responsible for selecting safe files."""
    if not paths or len(paths) > 10000 or len(set(paths)) != len(paths):
        raise ManifestError("Expected 1 to 10000 distinct file paths")
    root = _root(root)
    entries = []
    for path in sorted(paths):
        # Validate before touching the filesystem.
        entry = FileEntry(path=path, sha256="0" * 64)
        entries.append(FileEntry(path=entry.path, sha256=_digest(root, entry.path)))
    return FileManifest(files=tuple(entries))


def manifest_sha256(manifest: FileManifest) -> str:
    """Identify the canonical manifest, not the source or trustworthiness of its files."""
    payload = json.dumps(manifest.model_dump(mode="json"), sort_keys=True,
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def verify_manifest(root: Path, manifest: FileManifest, expected_sha256: str) -> None:
    """Require a separately trusted manifest digest and matching listed file bytes."""
    if manifest_sha256(manifest) != expected_sha256:
        raise ManifestError("Manifest digest does not match the trusted reference")
    root = _root(root)
    for entry in manifest.files:
        if _digest(root, entry.path) != entry.sha256:
            raise ManifestError(f"Inventory file digest does not match: {entry.path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inventory explicitly selected files; not a runtime security gate")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="write manifest JSON to stdout and its digest to stderr")
    create.add_argument("--root", type=Path, required=True)
    create.add_argument("--file", action="append", required=True, dest="files")
    verify = commands.add_parser("verify", help="verify using a separately trusted digest")
    verify.add_argument("--root", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--expected-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            manifest = build_manifest(args.root, args.files)
            print(manifest.model_dump_json(indent=2))
            print(f"manifest_sha256={manifest_sha256(manifest)}", file=sys.stderr)
        else:
            manifest = FileManifest.model_validate_json(args.manifest.read_bytes())
            verify_manifest(args.root, manifest, args.expected_sha256)
            print(json.dumps({"verified_listed_files": len(manifest.files)}))
    except (ManifestError, ValidationError, OSError) as exc:
        # Do not print submitted manifest contents, which may contain sensitive values.
        print(f"File inventory failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
