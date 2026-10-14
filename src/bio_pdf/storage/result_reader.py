"""Published result reader (plan T02).

One place that answers "what does job X currently publish?" — consumers
(API records/pages, ReviewService baselines, ExportService snapshots) read
through this, never by guessing fixed paths.

Resolution order:

1. ``jobs/<job>/published/result-manifest.json`` — the runner's published
   baseline; every file is hash-verified before use.
2. Legacy fixed paths (``jobs/<job>/validate/validation.json`` etc.) for
   pre-T02 jobs, without hash guarantees but still strict JSON.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bio_pdf.errors import WorkflowError


class ResultReader:
    """Read a job's published artifacts (hash-verified)."""

    #: Published manifest stage → logical file → reader-facing name.
    _LOGICAL = {
        "normalize": ("normalized.json", "normalized"),
        "extract": ("extract.json", "extract"),
        "validate": ("validation.json", "validation"),
        "export": ("result.json", "result"),
    }

    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = artifact_root

    def _job_dir(self, job_id: str) -> Path:
        return self.artifact_root / "jobs" / job_id

    def _read_verified(self, rel_path: str, sha256: str) -> dict[str, Any]:
        from bio_pdf.storage.artifacts import compute_sha256_bytes

        path = self.artifact_root / rel_path
        if not path.is_file():
            raise WorkflowError(
                f"published artifact missing: {rel_path}",
                details={"relative_path": rel_path},
            )
        data = path.read_bytes()
        if compute_sha256_bytes(data) != sha256:
            raise WorkflowError(
                f"published artifact hash mismatch: {rel_path}",
                details={"relative_path": rel_path},
            )
        try:
            return json.loads(data.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise WorkflowError(
                f"published artifact is not valid JSON: {rel_path}",
                details={"relative_path": rel_path},
            ) from exc

    def _read_legacy(self, *parts: str) -> dict[str, Any] | None:
        del parts
        return None

    def read_published_manifest(self, job_id: str) -> dict[str, Any] | None:
        path = self._job_dir(job_id) / "published" / "result-manifest.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def read_json(
        self, job_id: str, name: str, *, prefer_published: bool = True
    ) -> dict[str, Any]:
        """Read a logical result artifact for a job.

        ``name`` is one of ``normalized``/``extract``/``validation``/``result``.
        Published (hash-verified) wins when present; legacy fixed paths are
        the fallback so old jobs stay readable.
        """

        legacy_map = {
            "normalized": ("normalized.json",),
            "extract": ("extract", "extract.json"),
            "validation": ("validate", "validation.json"),
            "result": ("export", "result.json"),
        }
        if name not in legacy_map:
            raise WorkflowError(
                f"unknown result artifact name: {name}",
                details={"name": name},
            )

        if prefer_published:
            manifest = self.read_published_manifest(job_id)
            if manifest is not None:
                for stage_name, outputs in (manifest.get("stages") or {}).items():
                    logical_file, logical_name = self._LOGICAL.get(
                        stage_name, ("", "")
                    )
                    if logical_name != name:
                        continue
                    entry = outputs.get(logical_file)
                    if entry:
                        return self._read_verified(
                            entry["relative_path"], entry["sha256"]
                        )

        parts = legacy_map[name]
        path = self._job_dir(job_id).joinpath(*parts)
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise WorkflowError(
                    f"legacy artifact unreadable: {path}",
                    details={"error": str(exc)},
                ) from exc

        raise WorkflowError(
            f"job has no {name} result",
            details={"job_id": job_id, "name": name},
        )


__all__ = ["ResultReader"]
