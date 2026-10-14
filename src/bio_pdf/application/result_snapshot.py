"""Fixed result snapshots (plan B08).

A snapshot is captured ONCE inside a single read transaction: the job's
``result_revision``, the published baseline reference, every record head
and its immutable version. The snapshot is written to an immutable file;
export builds consume ONLY that file — a review landing after the capture
can never mix revisions into an export (the review's export race).

The records API uses :meth:`SnapshotService.read_current` so the revision
it reports matches the content it returns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bio_pdf.errors import WorkflowError
from bio_pdf.storage.artifacts import atomic_write_json, compute_sha256_bytes
from bio_pdf.storage.repository import Repository
from bio_pdf.storage.result_reader import ResultReader


@dataclass
class Snapshot:
    snapshot_id: str
    rel_path: str
    sha256: str
    payload: dict[str, Any]


class SnapshotService:
    """Capture and read fixed result snapshots."""

    def __init__(self, repository: Repository, artifact_root: Path) -> None:
        self.repository = repository
        self.artifact_root = artifact_root

    def read_current(self, job_id: str) -> dict[str, Any]:
        """Read revision + records inside one transaction (for the records API).

        Returns ``{result_revision, sequences, measurements, baseline_id}``
        where the revision corresponds to the returned content.
        """

        conn = self.repository.connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            data = self._capture_locked(job_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return data

    def capture(self, job_id: str, *, export_id: str) -> Snapshot:
        """Capture a snapshot and write it under the export's own directory."""

        payload = self.read_current(job_id)
        payload["captured_at"] = datetime.now(tz=timezone.utc).isoformat()
        payload["job_id"] = job_id
        payload["export_id"] = export_id

        out_dir = self.artifact_root / "jobs" / job_id / "exports" / export_id
        ref = atomic_write_json(out_dir, "snapshot.json", payload)
        rel = (Path("jobs") / job_id / "exports" / export_id / "snapshot.json").as_posix()
        sha = compute_sha256_bytes((self.artifact_root / rel).read_bytes())
        return Snapshot(
            snapshot_id=f"snap-{sha[:16]}", rel_path=rel, sha256=sha, payload=payload
        )

    def read_snapshot(self, rel_path: str) -> dict[str, Any]:
        """Load a snapshot file (hash is re-verified by callers via baseline)."""

        path = self.artifact_root / rel_path
        if not path.is_file():
            raise WorkflowError(
                f"snapshot file missing: {rel_path}",
                details={"relative_path": rel_path, "code": "RESULT_CORRUPT"},
            )
        return json.loads(path.read_text(encoding="utf-8"))

    # ---- internals ---------------------------------------------------------

    def _capture_locked(self, job_id: str) -> dict[str, Any]:
        """Caller holds a transaction: read revision + baseline + records."""

        job = self.repository.get_job(job_id)
        if job is None:
            raise WorkflowError(f"job not found: {job_id}", details={"job_id": job_id})

        reader = ResultReader(self.artifact_root)
        data = reader.read_json(job_id, "validation")
        sequences = data.get("sequences", [])
        measurements = data.get("measurements", [])
        heads = {
            row["record_id"]: row
            for row in self.repository.list_record_heads(job_id)
        }
        publication = self.repository.get_publication(job_id)

        def _overlay(records: list[dict[str, Any]], id_field: str) -> list[dict[str, Any]]:
            out = []
            for record in records:
                revision = 1
                head = heads.get(record[id_field])
                if head is not None:
                    revision = int(head["revision"])
                    version = self.repository.get_record_version(
                        job_id, record[id_field], revision
                    )
                    if version is not None:
                        record = json.loads(version["record_json"])
                record = dict(record)
                # B07: the UI never guesses the head version.
                record["_revision"] = revision
                failed = [
                    c.get("check_id")
                    for c in record.get("checks", [])
                    if c.get("outcome") == "fail"
                ]
                unresolved = any(
                    r.get("resolved") == "false" for r in record.get("residues", [])
                )
                blockers = []
                if failed:
                    blockers.append(f"failed checks: {failed}")
                if unresolved:
                    blockers.append("sequence contains unresolved residues")
                record["blockers"] = blockers
                record["allowed_actions"] = (
                    ["correct", "reject"] if blockers else ["confirm", "correct", "reject"]
                )
                out.append(record)
            return out

        return {
            "result_revision": job.result_revision,
            "baseline_id": publication["baseline_id"] if publication else None,
            "document_id": job.document_id,
            "sequences": _overlay(sequences, "sequence_id"),
            "measurements": _overlay(measurements, "measurement_id"),
        }


__all__ = ["SnapshotService", "Snapshot"]
