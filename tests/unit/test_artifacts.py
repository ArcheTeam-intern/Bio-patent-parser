"""Artifact primitive tests: atomic writes, hashes, JSON read/write."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bio_pdf.errors import (
    ArtifactConflictError,
    ArtifactCorruptError,
    ArtifactWriteFailedError,
)
from bio_pdf.storage.artifacts import (
    atomic_write_bytes,
    atomic_write_json,
    compute_sha256_bytes,
    read_verified_bytes,
    read_verified_json,
    write_json_artifact,
)


def test_atomic_write_bytes_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "out.bin"
    ref = atomic_write_bytes(tmp_path, "out.bin", b"hello world")
    assert target.exists()
    assert compute_sha256_bytes(target.read_bytes()) == ref.sha256


def test_atomic_write_json_serialises_and_reads_back(tmp_path: Path) -> None:
    payload = {"b": 2, "a": {"nested": 1}}
    ref = write_json_artifact(tmp_path, "data.json", payload, producer_stage_run_id="stage-1")
    decoded = read_verified_json(ref, tmp_path)
    assert decoded == payload
    assert ref.media_type == "application/json"
    assert ref.producer_stage_run_id == "stage-1"


def test_atomic_write_rejects_parent_escape(tmp_path: Path) -> None:
    with pytest.raises(ArtifactWriteFailedError):
        atomic_write_bytes(tmp_path, "../escape.bin", b"data")


def test_atomic_write_rejects_absolute_path(tmp_path: Path) -> None:
    # On Windows, "/etc/passwd" may not start with a recognised drive letter
    # and is stripped by ``_normalise_relative_path``. Use a Windows-style
    # absolute path that the safety check rejects unambiguously.
    with pytest.raises(ArtifactWriteFailedError):
        atomic_write_bytes(tmp_path, "C:/Windows/system32/drivers/etc/hosts", b"data")


def test_atomic_write_rejects_existing_different_hash(tmp_path: Path) -> None:
    target = tmp_path / "shared.bin"
    target.write_bytes(b"first")
    with pytest.raises(ArtifactConflictError):
        atomic_write_bytes(tmp_path, "shared.bin", b"different")


def test_atomic_write_is_idempotent_for_same_content(tmp_path: Path) -> None:
    ref1 = atomic_write_bytes(tmp_path, "dup.bin", b"abc")
    ref2 = atomic_write_bytes(tmp_path, "dup.bin", b"abc")
    assert ref1.sha256 == ref2.sha256


def test_atomic_write_json_sorts_keys(tmp_path: Path) -> None:
    ref = atomic_write_json(tmp_path, "k.json", {"z": 1, "a": 2})
    decoded = json.loads(ref.relative_path and (tmp_path / "k.json").read_text(encoding="utf-8"))
    assert list(decoded.keys()) == ["a", "z"]


def test_read_verified_bytes_raises_on_missing(tmp_path: Path) -> None:
    ref = atomic_write_bytes(tmp_path, "x.bin", b"data")
    (tmp_path / "x.bin").unlink()
    with pytest.raises(ArtifactCorruptError):
        read_verified_bytes(ref, tmp_path)


def test_read_verified_bytes_raises_on_corruption(tmp_path: Path) -> None:
    ref = atomic_write_bytes(tmp_path, "x.bin", b"data")
    # Mutate the file contents without going through the writer.
    (tmp_path / "x.bin").write_bytes(b"tampered")
    with pytest.raises(ArtifactCorruptError):
        read_verified_bytes(ref, tmp_path)


def test_read_verified_json_rejects_non_json(tmp_path: Path) -> None:
    ref = atomic_write_bytes(tmp_path, "x.bin", b"not-json")
    with pytest.raises(ArtifactCorruptError):
        read_verified_json(ref, tmp_path)


def test_atomic_write_failure_does_not_leave_partial_file(tmp_path: Path, monkeypatch) -> None:
    """Simulate an OSError during rename and assert no leftover temp file."""

    import bio_pdf.storage.artifacts as art_mod

    real_replace = art_mod.os.replace

    def fake_replace(src, dst):
        raise OSError("simulated failure")

    monkeypatch.setattr(art_mod.os, "replace", fake_replace)
    with pytest.raises(ArtifactWriteFailedError):
        atomic_write_bytes(tmp_path, "x.bin", b"data")

    # Ensure no leftover temp file under the target directory.
    temp_files = [p for p in tmp_path.iterdir() if p.name.startswith(".tmp.")]
    assert temp_files == []
    assert not (tmp_path / "x.bin").exists()
    # Sanity: monkey-patch is reverted by pytest.
    assert art_mod.os.replace is fake_replace  # type: ignore[comparison]
    _ = real_replace