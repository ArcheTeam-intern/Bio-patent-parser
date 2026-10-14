"""Published result reader (plan T02, strict in B03).

One place that answers "what does job X currently publish?". The DB is the
source of truth for the current baseline (``publications`` table); the FS
mirror ``published/current.json`` points at an immutable per-publish
manifest whose hash is verified. Legacy jobs without a mirror fall back to
the fixed working paths — but ONLY when no published baseline exists at
all. A present-yet-corrupt mirror/manifest is ``RESULT_CORRUPT``: silently
reading working files could mix revisions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bio_pdf.errors import WorkflowError


class ResultCorruptError(WorkflowError):
    code = "RESULT_CORRUPT"


class ResultReader:
    """Read a job's published artifacts (hash-verified)."""

    #: Published manifest stage → logical file → reader-facing name.
    _LOGICAL = {
        "normalize": ("normalized.json", "normalized"),
        "extract": ("extract.json", "extract"),
        "validate": ("validation.json", "validation"),
        "export": ("result.json", "result"),
    }

    _LEGACY = {
        "normalized": ("normalized.json",),
        "extract": ("extract", "extract.json"),
        "validation": ("validate", "validation.json"),
        "result": ("export", "result.json"),
    }

    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = artifact_root

    def _job_dir(self, job_id: str) -> Path:
        return self.artifact_root / "jobs" / job_id

    def _read_verified(self, rel_path: str, sha256: str) -> dict[str, Any]:
        from bio_pdf.storage.artifacts import compute_sha256_bytes

        path = self.artifact_root / rel_path
        if not path.is_file():
            raise ResultCorruptError(
                f"published artifact missing: {rel_path}",
                details={"relative_path": rel_path},
            )
        data = path.read_bytes()
        if compute_sha256_bytes(data) != sha256:
            raise ResultCorruptError(
                f"published artifact hash mismatch: {rel_path}",
                details={"relative_path": rel_path},
            )
        try:
            return json.loads(data.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ResultCorruptError(
                f"published artifact is not valid JSON: {rel_path}",
                details={"relative_path": rel_path},
            ) from exc

    def read_published_manifest(self, job_id: str) -> dict[str, Any] | None:
        """Resolve the current baseline manifest.

        - FS mirror ``published/current.json`` → immutable per-publish
          manifest (hash-verified). Corrupt anything → RESULT_CORRUPT.
        - No mirror: legacy single ``result-manifest.json`` (older jobs),
          hash-verified against its own content is not possible for old
          layouts; its presence is trusted only when it parses. Corrupt →
          RESULT_CORRUPT.
        - Neither present → None (job never published).
        """

        published_dir = self._job_dir(job_id) / "published"
        mirror = published_dir / "current.json"
        if mirror.is_file():
            try:
                mirror_data = json.loads(mirror.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ResultCorruptError(
                    f"published pointer mirror is unreadable for job {job_id}",
                    details={"path": str(mirror)},
                ) from exc
            rel = mirror_data.get("manifest_rel")
            sha = mirror_data.get("manifest_sha256")
            if not rel or not sha:
                raise ResultCorruptError(
                    f"published pointer mirror lacks manifest_rel/sha for job {job_id}",
                    details={"mirror": mirror_data},
                )
            manifest = self._read_verified(rel, sha)
            manifest.setdefault("baseline_id", sha[:16])
            manifest["_manifest_rel"] = rel
            return manifest

        legacy = published_dir / "result-manifest.json"
        if legacy.is_file():
            try:
                manifest = json.loads(legacy.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ResultCorruptError(
                    f"legacy published manifest is unreadable for job {job_id}",
                    details={"path": str(legacy)},
                ) from exc
            manifest.setdefault("baseline_id", manifest.get("published_at"))
            return manifest
        return None

    def read_json(
        self, job_id: str, name: str, *, prefer_published: bool = True
    ) -> dict[str, Any]:
        """Read a logical result artifact for a job.

        ``name`` is one of ``normalized``/``extract``/``validation``/``result``.
        When a published baseline exists, it is authoritative: a missing or
        broken entry raises RESULT_CORRUPT instead of silently reading the
        working tree (plan B03).
        """

        if name not in self._LEGACY:
            raise WorkflowError(
                f"unknown result artifact name: {name}",
                details={"name": name},
            )

        if prefer_published:
            manifest = self.read_published_manifest(job_id)
            if manifest is not None:
                found_any_stage = False
                for stage_name, outputs in (manifest.get("stages") or {}).items():
                    logical_file, logical_name = self._LOGICAL.get(stage_name, ("", ""))
                    if logical_name != name:
                        continue
                    found_any_stage = True
                    entry = (outputs or {}).get(logical_file)
                    if not isinstance(entry, dict) or not entry.get("relative_path") or not entry.get("sha256"):
                        raise ResultCorruptError(
                            f"published manifest entry is incomplete for {name}",
                            details={"stage": stage_name, "entry": entry},
                        )
                    return self._read_verified(entry["relative_path"], entry["sha256"])
                if not found_any_stage:
                    raise ResultCorruptError(
                        f"published manifest has no entry for {name}",
                        details={"job_id": job_id, "name": name},
                    )

        parts = self._LEGACY[name]
        path = self._job_dir(job_id).joinpath(*parts)
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise WorkflowError(
                    f"legacy artifact unreadable: {path}",
                    details={"error": str(exc), "code": "RESULT_CORRUPT"},
                ) from exc

        raise WorkflowError(
            f"job has no {name} result",
            details={"job_id": job_id, "name": name},
        )


__all__ = ["ResultReader", "ResultCorruptError"]
