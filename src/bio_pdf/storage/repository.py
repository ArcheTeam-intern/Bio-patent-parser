"""SQLite repository for jobs, stages and review decisions.

The repository owns connection lifecycle, foreign-key setup, WAL pragmas
and idempotent schema migration. It is deliberately small: every public
method corresponds to one design-level operation.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bio_pdf.contracts import (
    AcceptanceDecision,
    ArtifactRef,
    Job,
    JobStatus,
    ReviewDecision,
    ReviewItem,
    Stage,
    StageStatus,
)
from bio_pdf.errors import DatabaseError


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS documents (
    document_id     TEXT PRIMARY KEY,
    sha256          TEXT NOT NULL,
    source_path     TEXT NOT NULL,
    bytes           INTEGER NOT NULL,
    metadata_json   TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    document_id     TEXT NOT NULL REFERENCES documents(document_id),
    config_hash     TEXT NOT NULL,
    status          TEXT NOT NULL,
    current_stage   TEXT,
    result_revision INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stage_runs (
    stage_run_id          TEXT PRIMARY KEY,
    job_id                TEXT NOT NULL REFERENCES jobs(job_id),
    stage_name            TEXT NOT NULL,
    attempt               INTEGER NOT NULL,
    status                TEXT NOT NULL,
    cache_key             TEXT,
    artifact_manifest_ref  TEXT,
    started_at            TEXT,
    ended_at                TEXT,
    error_json            TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS stage_runs_job_idx ON stage_runs(job_id);
CREATE INDEX IF NOT EXISTS stage_runs_cache_idx ON stage_runs(cache_key);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id            TEXT PRIMARY KEY,
    sha256                 TEXT NOT NULL,
    relative_path          TEXT NOT NULL,
    media_type             TEXT NOT NULL,
    producer_stage_run_id  TEXT,
    created_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_items (
    review_id        TEXT PRIMARY KEY,
    job_id           TEXT NOT NULL REFERENCES jobs(job_id),
    record_id        TEXT NOT NULL,
    record_revision  INTEGER NOT NULL,
    reason_codes_json TEXT NOT NULL DEFAULT '[]',
    status           TEXT NOT NULL DEFAULT 'open',
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    proposed_actions_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS review_items_job_idx ON review_items(job_id);

CREATE TABLE IF NOT EXISTS review_decisions (
    decision_id      TEXT PRIMARY KEY,
    review_id        TEXT NOT NULL REFERENCES review_items(review_id),
    actor            TEXT NOT NULL,
    action           TEXT NOT NULL,
    patch_json       TEXT NOT NULL DEFAULT '{}',
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    base_revision    INTEGER NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_configs (
    job_id        TEXT PRIMARY KEY REFERENCES jobs(job_id),
    config_json   TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS worker_leases (
    job_id        TEXT PRIMARY KEY REFERENCES jobs(job_id),
    worker_id     TEXT NOT NULL,
    lease_until   TEXT NOT NULL,
    heartbeat_at  TEXT,
    generation    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS record_versions (
    job_id        TEXT NOT NULL REFERENCES jobs(job_id),
    record_id     TEXT NOT NULL,
    revision      INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    record_json   TEXT NOT NULL,
    decision_id   TEXT,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (job_id, record_id, revision)
);

CREATE TABLE IF NOT EXISTS record_heads (
    job_id              TEXT NOT NULL REFERENCES jobs(job_id),
    record_id           TEXT NOT NULL,
    revision            INTEGER NOT NULL,
    kind                TEXT NOT NULL,
    validation_state    TEXT NOT NULL,
    acceptance_state    TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (job_id, record_id)
);

CREATE TABLE IF NOT EXISTS exports (
    export_id       TEXT PRIMARY KEY,
    job_id          TEXT NOT NULL REFERENCES jobs(job_id),
    format          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued',
    result_revision INTEGER NOT NULL DEFAULT 0,
    accepted_only   INTEGER NOT NULL DEFAULT 0,
    artifact_rel    TEXT,
    record_count    INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    created_at      TEXT NOT NULL,
    completed_at    TEXT
);
"""


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _connect(database: Path | str) -> sqlite3.Connection:
    """Open a SQLite connection with the recommended pragmas.

    Foreign keys are enabled per-connection. ``busy_timeout`` makes us wait
    instead of raising immediately when another writer holds the lock.
    ``check_same_thread=False`` lets the API's threadpool share the one
    process-local connection; WAL + busy_timeout serialise the writers.
    """

    conn = sqlite3.connect(database, isolation_level=None, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_repository(database: Path | str) -> sqlite3.Connection:
    """Open (or create) the repository database and apply migrations.

    The schema is idempotent (``CREATE TABLE IF NOT EXISTS``), so calling
    this multiple times is safe.
    """

    if isinstance(database, Path):
        database.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(database)
    try:
        conn.executescript(SCHEMA_SQL)
    except sqlite3.Error as exc:
        conn.close()
        raise DatabaseError(
            f"failed to initialise repository: {exc}",
            details={"database": str(database)},
        ) from exc
    return conn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_dumps(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _json_loads(text: str | None, *, default: Any) -> Any:
    import json

    if not text:
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class Repository:
    """Thin wrapper around a SQLite connection.

    The repository does not enforce business rules; the runner does. It
    just provides typed CRUD for the design-defined tables.
    """

    def __init__(self, database: Path | str) -> None:
        self._conn = init_repository(database)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    # ---- documents -----------------------------------------------------

    def upsert_document(
        self,
        *,
        document_id: str,
        sha256: str,
        source_path: str,
        bytes_count: int,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO documents (document_id, sha256, source_path, bytes, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
                sha256 = excluded.sha256,
                source_path = excluded.source_path,
                bytes = excluded.bytes,
                metadata_json = excluded.metadata_json
            """,
            (
                document_id,
                sha256,
                source_path,
                bytes_count,
                _json_dumps(metadata or {}),
                _utcnow_iso(),
            ),
        )

    # ---- jobs ----------------------------------------------------------

    def create_job(self, job: Job) -> None:
        self._conn.execute(
            """
            INSERT INTO jobs (job_id, document_id, config_hash, status, current_stage,
                              result_revision, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.job_id,
                job.document_id,
                job.config_hash,
                job.status,
                job.current_stage,
                job.result_revision,
                job.created_at.isoformat(),
                job.updated_at.isoformat(),
            ),
        )

    def get_job(self, job_id: str) -> Job | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        return Job(
            job_id=row["job_id"],
            document_id=row["document_id"],
            config_hash=row["config_hash"],
            status=row["status"],
            current_stage=row["current_stage"],
            result_revision=row["result_revision"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def update_job_status(
        self, job_id: str, status: JobStatus, *, current_stage: str | None = None
    ) -> None:
        self._conn.execute(
            """
            UPDATE jobs SET status = ?, current_stage = ?, updated_at = ?
            WHERE job_id = ?
            """,
            (status, current_stage, _utcnow_iso(), job_id),
        )

    # ---- stages --------------------------------------------------------

    def start_stage(self, stage: Stage) -> None:
        self._conn.execute(
            """
            INSERT INTO stage_runs (stage_run_id, job_id, stage_name, attempt, status,
                                    cache_key, artifact_manifest_ref, started_at,
                                    ended_at, error_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stage.stage_run_id,
                stage.job_id,
                stage.stage_name,
                stage.attempt,
                stage.status,
                stage.cache_key,
                stage.artifact_manifest_ref,
                stage.started_at.isoformat() if stage.started_at else None,
                stage.ended_at.isoformat() if stage.ended_at else None,
                _json_dumps(stage.error_json),
            ),
        )

    def complete_stage(
        self,
        stage_run_id: str,
        *,
        artifact_manifest_ref: str | None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE stage_runs SET status = ?, ended_at = ?, artifact_manifest_ref = ?
            WHERE stage_run_id = ?
            """,
            (
                "succeeded",
                _utcnow_iso(),
                artifact_manifest_ref,
                stage_run_id,
            ),
        )

    def fail_stage(self, stage_run_id: str, *, error_json: dict[str, Any]) -> None:
        self._conn.execute(
            """
            UPDATE stage_runs SET status = ?, ended_at = ?, error_json = ?
            WHERE stage_run_id = ?
            """,
            (
                "failed",
                _utcnow_iso(),
                _json_dumps(error_json),
                stage_run_id,
            ),
        )

    def find_successful_stage(self, job_id: str, stage_name: str, cache_key: str) -> Stage | None:
        """Return the most recent successful stage run matching ``cache_key``.

        Used by the runner to short-circuit a stage when its inputs and rules
        are unchanged. We do not return failed runs; resume must re-attempt
        those with a fresh ``attempt`` number.
        """

        row = self._conn.execute(
            """
            SELECT * FROM stage_runs
            WHERE job_id = ? AND stage_name = ? AND cache_key = ? AND status = 'succeeded'
            ORDER BY ended_at DESC, attempt DESC LIMIT 1
            """,
            (job_id, stage_name, cache_key),
        ).fetchone()
        if row is None:
            return None
        return self._stage_from_row(row)

    def list_stages(self, job_id: str) -> Iterable[Stage]:
        rows = self._conn.execute(
            "SELECT * FROM stage_runs WHERE job_id = ? ORDER BY attempt",
            (job_id,),
        ).fetchall()
        return [self._stage_from_row(row) for row in rows]

    def _stage_from_row(self, row: sqlite3.Row) -> Stage:
        started = datetime.fromisoformat(row["started_at"]) if row["started_at"] else None
        ended = datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None
        return Stage(
            stage_run_id=row["stage_run_id"],
            job_id=row["job_id"],
            stage_name=row["stage_name"],
            attempt=row["attempt"],
            status=row["status"],
            cache_key=row["cache_key"],
            artifact_manifest_ref=row["artifact_manifest_ref"],
            started_at=started,
            ended_at=ended,
            error_json=_json_loads(row["error_json"], default={}),
        )

    # ---- artifacts -----------------------------------------------------

    def register_artifact(self, ref: ArtifactRef) -> None:
        self._conn.execute(
            """
            INSERT INTO artifacts (artifact_id, sha256, relative_path, media_type,
                                   producer_stage_run_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_id) DO UPDATE SET
                    relative_path = excluded.relative_path,
                    media_type = excluded.media_type
            """,
            (
                ref.artifact_id,
                ref.sha256,
                ref.relative_path,
                ref.media_type,
                ref.producer_stage_run_id or None,
                _utcnow_iso(),
            ),
        )

    def get_artifact(self, artifact_id: str) -> ArtifactRef | None:
        row = self._conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        if row is None:
            return None
        return ArtifactRef(
            artifact_id=row["artifact_id"],
            sha256=row["sha256"],
            relative_path=row["relative_path"],
            media_type=row["media_type"],
            producer_stage_run_id=row["producer_stage_run_id"] or "",
        )

    # ---- worker leases ----------------------------------------------------

    def claim_next_queued_job(
        self, *, worker_id: str, lease_seconds: float = 600.0
    ) -> Job | None:
        """Atomically claim the oldest queued job with a fresh lease.

        The claim (SELECT → INSERT lease → UPDATE status) runs inside one
        transaction; SQLite serialises writers so two workers can never
        claim the same job. Expired leases from crashed workers are
        reclaimable: a queued job always wins, then a running job whose
        lease has lapsed is re-claimed with a bumped generation.
        """

        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = _utcnow_iso()
            row = conn.execute(
                """
                SELECT j.job_id FROM jobs j
                LEFT JOIN worker_leases w ON w.job_id = j.job_id
                WHERE j.status = 'queued'
                   OR (j.status = 'running' AND (w.lease_until IS NULL OR w.lease_until < ?))
                ORDER BY j.created_at
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            job_id = row["job_id"]

            generation = 1
            prev = conn.execute(
                "SELECT generation FROM worker_leases WHERE job_id = ?", (job_id,)
            ).fetchone()
            if prev is not None:
                generation = int(prev["generation"]) + 1

            from datetime import datetime, timedelta, timezone

            lease_until = (
                datetime.now(tz=timezone.utc) + timedelta(seconds=lease_seconds)
            ).isoformat()
            conn.execute(
                """
                INSERT INTO worker_leases (job_id, worker_id, lease_until, heartbeat_at, generation)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    worker_id = excluded.worker_id,
                    lease_until = excluded.lease_until,
                    heartbeat_at = excluded.heartbeat_at,
                    generation = excluded.generation
                """,
                (job_id, worker_id, lease_until, now, generation),
            )
            conn.execute(
                "UPDATE jobs SET status = 'running', current_stage = NULL, updated_at = ? WHERE job_id = ?",
                (now, job_id),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        job = self.get_job(job_id)
        assert job is not None
        return job

    def heartbeat(self, job_id: str, *, worker_id: str, lease_seconds: float = 600.0) -> None:
        from datetime import datetime, timedelta, timezone

        lease_until = (
            datetime.now(tz=timezone.utc) + timedelta(seconds=lease_seconds)
        ).isoformat()
        self._conn.execute(
            """
            UPDATE worker_leases
            SET heartbeat_at = ?, lease_until = ?
            WHERE job_id = ? AND worker_id = ?
            """,
            (_utcnow_iso(), lease_until, job_id, worker_id),
        )

    def release_lease(self, job_id: str, *, worker_id: str) -> None:
        self._conn.execute(
            "DELETE FROM worker_leases WHERE job_id = ? AND worker_id = ?",
            (job_id, worker_id),
        )

    # ---- exports ----------------------------------------------------------

    def create_export(
        self,
        *,
        export_id: str,
        job_id: str,
        format: str,
        result_revision: int,
        accepted_only: bool,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO exports (export_id, job_id, format, status, result_revision,
                                 accepted_only, created_at)
            VALUES (?, ?, ?, 'queued', ?, ?, ?)
            """,
            (export_id, job_id, format, result_revision, int(accepted_only), _utcnow_iso()),
        )

    def update_export(
        self,
        export_id: str,
        *,
        status: str,
        artifact_rel: str | None = None,
        record_count: int | None = None,
        error: str | None = None,
    ) -> None:
        # Only overwrite the columns the caller actually provides, so a
        # failure transition doesn't clobber completed fields with NULLs.
        sets = ["status = ?"]
        params: list[Any] = [status]
        if artifact_rel is not None:
            sets.append("artifact_rel = ?")
            params.append(artifact_rel)
        if record_count is not None:
            sets.append("record_count = ?")
            params.append(record_count)
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        if status in ("completed", "failed"):
            sets.append("completed_at = ?")
            params.append(_utcnow_iso())
        params.append(export_id)
        self._conn.execute(
            f"UPDATE exports SET {', '.join(sets)} WHERE export_id = ?",
            params,
        )

    def get_export(self, export_id: str) -> Any:
        row = self._conn.execute(
            "SELECT * FROM exports WHERE export_id = ?", (export_id,)
        ).fetchone()
        return row

    # ---- record versions --------------------------------------------------

    def get_record_head(self, job_id: str, record_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM record_heads WHERE job_id = ? AND record_id = ?",
            (job_id, record_id),
        ).fetchone()

    def get_record_version(
        self, job_id: str, record_id: str, revision: int
    ) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM record_versions WHERE job_id = ? AND record_id = ? AND revision = ?",
            (job_id, record_id, revision),
        ).fetchone()

    def list_record_heads(self, job_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM record_heads WHERE job_id = ? ORDER BY record_id",
            (job_id,),
        ).fetchall()

    # ---- job config ------------------------------------------------------

    def set_job_config(self, job_id: str, config: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO job_configs (job_id, config_json) VALUES (?, ?)
            ON CONFLICT(job_id) DO UPDATE SET config_json = excluded.config_json
            """,
            (job_id, _json_dumps(config)),
        )

    def get_job_config(self, job_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT config_json FROM job_configs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return {}
        return _json_loads(row["config_json"], default={})

    # ---- review --------------------------------------------------------

    def append_review_decision(self, decision: ReviewDecision) -> None:
        # Verify base_revision matches the live record before persisting.
        row = self._conn.execute(
            "SELECT record_revision FROM review_items WHERE review_id = ?",
            (decision.review_id,),
        ).fetchone()
        if row is None:
            raise DatabaseError(
                f"review item not found: {decision.review_id}",
                details={"decision_id": decision.d_id if hasattr(decision, "d_id") else None},
            )
        if row["record_revision"] != decision.base_revision:
            from bio_pdf.errors import ReviewRevisionConflictError

            raise ReviewRevisionConflictError(
                f"review base_revision {decision.base_revision} does not match live revision {row['record_revision']}",
                details={
                    "review_id": decision.review_id,
                    "live_revision": row["record_revision"],
                },
            )
        self._conn.execute(
            """
            INSERT INTO review_decisions (decision_id, review_id, actor, action,
                                          patch_json, evidence_ids_json,
                                          base_revision, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision.decision_id,
                decision.review_id,
                decision.actor,
                decision.action,
                _json_dumps(decision.patch_json),
                _json_dumps(decision.evidence_ids),
                decision.base_revision,
                decision.created_at.isoformat(),
            ),
        )

    def list_review_decisions(self, review_id: str) -> list[ReviewDecision]:
        rows = self._conn.execute(
            "SELECT * FROM review_decisions WHERE review_id = ? ORDER BY created_at",
            (review_id,),
        ).fetchall()
        out: list[ReviewDecision] = []
        for row in rows:
            out.append(
                ReviewDecision(
                    decision_id=row["decision_id"],
                    review_id=row["review_id"],
                    actor=row["actor"],
                    action=row["action"],
                    patch_json=_json_loads(row["patch_json"], default={}),
                    evidence_ids=_json_loads(row["evidence_ids_json"], default=[]),
                    base_revision=row["base_revision"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                )
            )
        return out

    def upsert_review_item(self, item: ReviewItem) -> None:
        self._conn.execute(
            """
            INSERT INTO review_items (review_id, job_id, record_id, record_revision,
                                       reason_codes_json, status, evidence_ids_json,
                                       proposed_actions_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(review_id) DO UPDATE SET
                job_id = excluded.job_id,
                record_id = excluded.record_id,
                record_revision = excluded.record_revision,
                reason_codes_json = excluded.reason_codes_json,
                status = excluded.status,
                evidence_ids_json = excluded.evidence_ids_json,
                proposed_actions_json = excluded.proposed_actions_json
            """,
            (
                item.review_id,
                item.job_id,
                item.record_id,
                item.record_revision,
                _json_dumps(item.reason_codes),
                item.status,
                _json_dumps(item.evidence_ids),
                _json_dumps(item.proposed_actions),
            ),
        )