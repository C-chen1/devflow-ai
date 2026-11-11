from hashlib import sha256
from io import BytesIO
from unittest.mock import Mock, call
from uuid import uuid4

import pytest

from devflow_ai.artifacts import ArtifactVerificationError, copy_artifact_content, read_verified_artifact, write_artifact_file


def test_read_verified_artifact_rejects_tampering_and_wrong_location(tmp_path):
    artifact_id = uuid4()
    path, digest = write_artifact_file(tmp_path, artifact_id, BytesIO(b"patch content"), chunk_size=4)
    assert read_verified_artifact(
        tmp_path, artifact_id, content_ref=str(path), content_hash=digest, max_bytes=32,
    ) == b"patch content"
    with pytest.raises(ArtifactVerificationError, match="location"):
        read_verified_artifact(
            tmp_path, artifact_id, content_ref=str(tmp_path / "other.bin"), content_hash=digest, max_bytes=32,
        )
    with pytest.raises(ArtifactVerificationError, match="size"):
        read_verified_artifact(tmp_path, artifact_id, content_ref=str(path), content_hash=digest, max_bytes=3)
    path.write_bytes(b"modified")
    with pytest.raises(ArtifactVerificationError, match="hash"):
        read_verified_artifact(tmp_path, artifact_id, content_ref=str(path), content_hash=digest, max_bytes=32)


def test_copy_artifact_content_reads_and_hashes_multiple_chunks():
    content = b"a\x00\xffbcdef"
    source = Mock(wraps=BytesIO(content))
    destination = BytesIO()

    digest = copy_artifact_content(source, destination, chunk_size=3)

    assert source.read.call_args_list == [call(3)] * 4
    assert destination.getvalue() == content
    assert digest == sha256(content).hexdigest()


def test_copy_artifact_content_rejects_incomplete_write():
    source = BytesIO(b"abc")
    destination = Mock()
    destination.write.return_value = 2

    with pytest.raises(OSError, match="Incomplete write"):
        copy_artifact_content(source, destination, chunk_size=3)


def test_write_artifact_file_preserves_an_existing_artifact(tmp_path):
    artifact_id = uuid4()
    content = b"original content"

    path, digest = write_artifact_file(tmp_path, artifact_id, BytesIO(content), chunk_size=3)

    assert path == tmp_path / f"{artifact_id}.bin"
    assert path.read_bytes() == content
    assert digest == sha256(content).hexdigest()

    with pytest.raises(FileExistsError):
        write_artifact_file(tmp_path, artifact_id, BytesIO(b"replacement"), chunk_size=3)
    assert path.read_bytes() == content


def test_write_artifact_file_removes_partial_file_after_copy_failure(tmp_path):
    artifact_id = uuid4()
    source = Mock()
    source.read.side_effect = [b"abc", OSError("source read failed")]

    with pytest.raises(OSError, match="source read failed"):
        write_artifact_file(tmp_path, artifact_id, source, chunk_size=3)

    assert not (tmp_path / f"{artifact_id}.bin").exists()


def test_write_artifact_file_preserves_previously_read_probe_byte(tmp_path):
    content = b"abc!defgh"
    source_path = tmp_path / "source.txt"
    source_path.write_bytes(content)
    max_bytes = 3

    with source_path.open("rb") as source:
        initial_data = source.read(max_bytes + 1)
        path, digest = write_artifact_file(
            tmp_path, uuid4(), source, chunk_size=2, initial_data=initial_data,
        )

    assert initial_data[:max_bytes] == b"abc"
    assert path.read_bytes() == content
    assert digest == sha256(content).hexdigest()
