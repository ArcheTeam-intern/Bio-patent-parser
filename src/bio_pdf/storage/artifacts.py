"""Immutable artifact storage primitives.

Artifacts are content-addressed files written atomically. The hash on disk is
the source of truth — if the hash mismatches a recorded :class:`ArtifactRef`,
we raise :class:`ArtifactCorruptError` rather than silently returning bad
data.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bio_pdf.contracts import ArtifactRef
from bio_pdf.errors import (
    ArtifactConflictError,
    ArtifactCorruptError,
    ArtifactWriteFailedError,
)

#: Path components must not escape the artifact root. We allow only forward
#: slashes and disallow absolute paths / ``..`` segments.
_SAFE_REL_PATH = re.compile(r"^[A-Za-z0-9._/-]+$")


def compute_sha256_bytes(data: bytes) -> str:
    """Return the lowercase hex SHA-256 of ``data``."""

    return hashlib.sha256(data).hexdigest()


def _normalise_relative_path(path: str) -> str:
    """Return ``path`` normalised to forward slashes with no leading ``/``."""

    forward = path.replace("\\", "/").lstrip("/")
    if forward != forward.rstrip("/").lstrip("..").lstrip("/"):
        if ".." in forward.split("/"):
            raise ArtifactWriteFailedError(
                f"relative path contains '..': {path!r}",
                details={"path": path},
            )
    if not _SAFE_REL_PATH.match(forward):
        raise ArtifactWriteFailedError(
            f"unsafe relative path: {path!r}",
            details={"path": path},
        )
    return forward


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def atomic_replace_json(target_dir: Path, name: str, payload: Mapping[str, Any]) -> None:
    """Atomically REPLACE ``target_dir/name`` with new JSON content.

    Unlike :func:`atomic_write_bytes` this is a *pointer* write: publishing
    the current result manifest legitimately replaces the previous pointer.
    The replaced content stays available through the stage-run artifacts it
    pointed at. Crash-safe via tempfile + ``os.replace``.
    """

    target_dir.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode(
        "utf-8"
    )
    try:
        with tempfile.NamedTemporaryFile(
            dir=str(target_dir), delete=False, prefix=".tmp.", suffix=".json"
        ) as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
            tmp_path = handle.name
        os.replace(tmp_path, target_dir / name)
    except OSError as exc:
        if "tmp_path" in locals():
            Path(tmp_path).unlink(missing_ok=True)
        raise ArtifactWriteFailedError(
            f"failed to replace {name}: {exc}",
            details={"path": str(target_dir / name)},
        ) from exc


def atomic_write_bytes(
    target_dir: Path, rel_path: str, data: bytes
) -> ArtifactRef:
    """Atomically write ``data`` to ``target_dir/rel_path``.

    The write happens through ``tempfile`` + :func:`os.replace`, so a reader
    never observes a half-written file. Returns an :class:`ArtifactRef`
    pointing at the final location with the computed SHA-256.

    Args:
        target_dir: The artifact root (typically ``data/jobs/<job_id>``).
        rel_path: The relative path within ``target_dir``. Must not contain
            ``..`` segments or be absolute.
        data: The bytes to write.

    Raises:
        ArtifactWriteFailedError: If the path is unsafe or the rename fails.
        ArtifactConflictError: If the target file already exists with a
            different SHA-256.
    """

    rel = _normalise_relative_path(rel_path)
    target = (target_dir / rel).resolve()
    target_root = target_dir.resolve()
    if not str(target).startswith(str(target_root)):
        raise ArtifactWriteFailedError(
            f"relative path escapes artifact root: {rel_path!r}",
            details={"path": rel_path, "root": str(target_root)},
        )

    _ensure_parent(target)
    sha = compute_sha256_bytes(data)

    if target.exists():
        existing = target.read_bytes()
        if compute_sha256_bytes(existing) != sha:
            raise ArtifactConflictError(
                f"target file already exists with a different hash: {rel}",
                details={"path": str(target), "existing_sha256": compute_sha256_bytes(existing)},
            )
        # Same content → idempotent reuse.
        return ArtifactRef(
            artifact_id=f"sha256:{sha}",
            sha256=sha,
            relative_path=rel,
            media_type="application/octet-stream",
            producer_stage_run_id="",
        )

    try:
        with tempfile.NamedTemporaryFile(
            dir=str(target.parent), delete=False, prefix=".tmp.", suffix=Path(rel).suffix
        ) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            tmp_path = handle.name
        os.replace(tmp_path, target)
    except OSError as exc:
        # Clean up the temp file on failure so we don't leave debris.
        if "tmp_path" in locals():
            Path(tmp_path).unlink(missing_ok=True)
        raise ArtifactWriteFailedError(
            f"failed to write artifact {rel}: {exc}",
            details={"path": str(target)},
        ) from exc

    return ArtifactRef(
        artifact_id=f"sha256:{sha}",
        sha256=sha,
        relative_path=rel,
        media_type="application/octet-stream",
        producer_stage_run_id="",
    )


def atomic_write_json(
    target_dir: Path, rel_path: str, payload: Mapping[str, Any]
) -> ArtifactRef:
    """Atomically serialise ``payload`` as UTF-8 JSON.

    Keys are sorted to keep artifacts diff-friendly across runs.
    """

    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    ref = atomic_write_bytes(target_dir, rel_path, body.encode("utf-8"))
    return ArtifactRef(
        artifact_id=ref.artifact_id,
        sha256=ref.sha256,
        relative_path=ref.relative_path,
        media_type="application/json",
        producer_stage_run_id=ref.producer_stage_run_id,
    )


def write_json_artifact(
    target_dir: Path,
    rel_path: str,
    payload: Mapping[str, Any],
    *,
    producer_stage_run_id: str,
) -> ArtifactRef:
    """Variant of :func:`atomic_write_json` that records the producer.

    Stage handlers should prefer this variant so the resulting
    :class:`ArtifactRef` carries lineage information for the SQLite
    repository.
    """

    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    ref = atomic_write_bytes(target_dir, rel_path, body.encode("utf-8"))
    return ArtifactRef(
        artifact_id=ref.artifact_id,
        sha256=ref.sha256,
        relative_path=ref.relative_path,
        media_type="application/json",
        producer_stage_run_id=producer_stage_run_id,
    )


def read_verified_bytes(ref: ArtifactRef, root: Path) -> bytes:
    """Read ``ref`` and verify its SHA-256.

    Raises:
        ArtifactCorruptError: If the file is missing or its hash mismatches.
    """

    path = (root / ref.relative_path).resolve()
    if not str(path).startswith(str(root.resolve())):
        raise ArtifactCorruptError(
            f"artifact path escapes root: {ref.relative_path}",
            details={"ref": ref.model_dump()},
        )
    if not path.exists():
        raise ArtifactCorruptError(
            f"artifact file missing: {ref.relative_path}",
            details={"ref": ref.model_dump()},
        )
    data = path.read_bytes()
    actual = compute_sha256_bytes(data)
    if actual != ref.sha256:
        raise ArtifactCorruptError(
            f"artifact hash mismatch for {ref.relative_path}",
            details={"ref": ref.model_dump(), "actual_sha256": actual},
        )
    return data


def read_verified_json(ref: ArtifactRef, root: Path) -> dict[str, Any]:
    """Read ``ref`` and decode it as UTF-8 JSON, verifying its hash first."""

    data = read_verified_bytes(ref, root)
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactCorruptError(
            f"artifact is not valid UTF-8 JSON: {ref.relative_path}",
            details={"ref": ref.model_dump()},
        ) from exc


# ---------------------------------------------------------------------------
# Cache keys
# ---------------------------------------------------------------------------


def _canonical_json(payload: Mapping[str, Any]) -> str:
    """Serialise ``payload`` with sorted keys and no whitespace.

    Used as the input to the cache-key hash. Sorting keeps the key stable
    across input dict ordering.
    """

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def make_cache_key(payload: Mapping[str, Any]) -> str:
    """Return a stable SHA-256 cache key for ``payload``."""

    return compute_sha256_bytes(_canonical_json(payload).encode("utf-8"))