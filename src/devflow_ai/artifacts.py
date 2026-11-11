"""File content operations for Artifacts."""
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO
from uuid import UUID
from pydantic import BaseModel, ConfigDict


class ToolArtifactResult(BaseModel):
    """Metadata for a saved tool output awaiting database registration."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    kind: str
    content_ref: str
    content_hash: str


class ArtifactVerificationError(ValueError):
    """Artifact content or location does not match its recorded metadata."""


def read_verified_artifact(
    artifact_root: Path, artifact_id: UUID, *, content_ref: str, content_hash: str, max_bytes: int,
) -> bytes:
    """Read bounded Artifact bytes and verify their recorded location and SHA-256."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    root = artifact_root.resolve(strict=True)
    if not root.is_dir():
        raise ArtifactVerificationError("Artifact root must be a directory")
    expected = root / f"{artifact_id}.bin"
    recorded = Path(content_ref)
    if not recorded.is_absolute() or recorded != expected or expected.is_symlink():
        raise ArtifactVerificationError("Artifact location does not match its ID and root")
    try:
        with expected.open("rb") as source:
            data = source.read(max_bytes + 1)
    except OSError as error:
        raise ArtifactVerificationError("Artifact cannot be read") from error
    if len(data) > max_bytes:
        raise ArtifactVerificationError("Artifact exceeds the permitted size")
    if sha256(data).hexdigest() != content_hash:
        raise ArtifactVerificationError("Artifact content hash does not match")
    return data


def copy_artifact_content(
    source: BinaryIO, destination: BinaryIO, *, chunk_size: int, initial_data: bytes = b"",
) -> str:
    """Copy already-read bytes, then the rest of the open stream, and return its digest."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    hasher = sha256()
    chunk = initial_data or source.read(chunk_size)
    while chunk:
        if destination.write(chunk) != len(chunk):
            raise OSError("Incomplete write to Artifact destination")
        hasher.update(chunk)
        chunk = source.read(chunk_size)

    return hasher.hexdigest()


def write_artifact_file(
    artifact_root: Path, artifact_id: UUID, source: BinaryIO, *, chunk_size: int,
    initial_data: bytes = b"",
) -> tuple[Path, str]:
    """Create a new file in the trusted Artifact directory and return its path and digest."""
    destination_path = artifact_root / f"{artifact_id}.bin"
    destination = destination_path.open("xb")
    try:
        with destination:
            digest = copy_artifact_content(
                source, destination, chunk_size=chunk_size, initial_data=initial_data,
            )
    except BaseException as error:
        try:
            destination_path.unlink(missing_ok=True)
        except OSError as cleanup_error:
            error.add_note(f"Artifact cleanup failed: {cleanup_error}")
        raise

    return destination_path, digest
