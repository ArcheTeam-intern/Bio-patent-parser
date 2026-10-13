"""Application service shared by the CLI and the HTTP API.

The service owns the end-to-end job lifecycle: register the document,
create the job row, build a fully-wired runner and run/resume it. Both
surfaces (Typer CLI, FastAPI) call these functions so the domain pipeline
is wired exactly once (review finding 2).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bio_pdf.contracts import Job, ReviewItem
from bio_pdf.errors import WorkflowError
from bio_pdf.ingest.inspector import inspect_pdf
from bio_pdf.storage.artifacts import make_cache_key
from bio_pdf.storage.repository import Repository
from bio_pdf.workflow.handlers import build_production_handlers
from bio_pdf.workflow.runner import RunResult, WorkflowRunner
from bio_pdf.workflow.stages import STAGE_ORDER

#: Bump when the stage wiring or domain rules change so old cache entries
#: cannot be mixed with new semantics.
PIPELINE_VERSION = "v1"


@dataclass
class JobPaths:
    """Filesystem layout for one application instance."""

    database: Path
    artifact_root: Path

    @classmethod
    def under(cls, data_dir: Path) -> "JobPaths":
        # artifact_root is the data dir itself: job artifacts live under
        # <data>/jobs/<job_id>/... matching the ArtifactRef relative paths.
        return cls(
            database=data_dir / "bio.sqlite",
            artifact_root=data_dir,
        )


class JobService:
    """Create, run, resume and inspect domain jobs."""

    def __init__(self, paths: JobPaths) -> None:
        self.paths = paths
        self.paths.database.parent.mkdir(parents=True, exist_ok=True)
        self.paths.artifact_root.mkdir(parents=True, exist_ok=True)
        self._repository = Repository(self.paths.database)

    def close(self) -> None:
        self._repository.close()

    @property
    def repository(self) -> Repository:
        return self._repository

    # ---- commands --------------------------------------------------------

    def create_job(
        self,
        source_path: Path,
        *,
        engine: str = "native",
        page_numbers: tuple[int, ...] = (),
        targets: tuple[str, ...] = ("sequences", "activities"),
        document_type_hint: str | None = None,
    ) -> tuple[Job, int]:
        """Register the document and enqueue a job. Returns (job, page_count).

        ``page_numbers`` may be empty for "all pages". The inspection runs
        up front so a non-PDF input fails at creation time, not in the
        worker.
        """

        inspection = inspect_pdf(source_path)
        self._repository.upsert_document(
            document_id=inspection.document_id,
            sha256=inspection.sha256,
            source_path=str(source_path.resolve()),
            bytes_count=inspection.bytes,
            metadata={"document_type_hint": document_type_hint},
        )

        pages = tuple(page_numbers) or tuple(range(1, inspection.page_count + 1))
        config = {
            "pipeline": PIPELINE_VERSION,
            "engine": engine,
            "pages": list(pages),
            "targets": list(targets),
            "document_type_hint": document_type_hint,
        }
        config_hash = make_cache_key(config)
        now = datetime.now(tz=timezone.utc)
        job = Job(
            job_id=f"job-{uuid.uuid4().hex[:12]}",
            document_id=inspection.document_id,
            config_hash=config_hash,
            status="queued",
            created_at=now,
            updated_at=now,
        )
        self._repository.create_job(job)
        self._repository.set_job_config(job.job_id, config)
        return job, inspection.page_count

    def run_job(self, job_id: str, *, resume: bool = False) -> RunResult:
        """Run (or resume) a job through the full production pipeline."""

        job = self._repository.get_job(job_id)
        if job is None:
            raise WorkflowError(f"job not found: {job_id}", details={"job_id": job_id})
        row = self._repository.connection.execute(
            "SELECT source_path FROM documents WHERE document_id = ?",
            (job.document_id,),
        ).fetchone()
        if row is None:
            raise WorkflowError(
                f"document row missing for {job.document_id}",
                details={"document_id": job.document_id},
            )

        runner = self._build_runner()
        config = self.job_config(job_id)
        self._repository.update_job_status(job_id, "running", current_stage=None)
        result = runner.run(
            job,
            source_path=str(row["source_path"]),
            page_numbers=tuple(config.get("pages") or ()),
            resume=resume,
            options=self.job_options(job, config),
        )
        return result

    # ---- queries ---------------------------------------------------------

    def get_job(self, job_id: str) -> Job | None:
        return self._repository.get_job(job_id)

    def list_jobs(self) -> list[Job]:
        rows = self._repository.connection.execute(
            "SELECT job_id FROM jobs ORDER BY created_at DESC"
        ).fetchall()
        return [job for row in rows if (job := self._repository.get_job(row["job_id"]))]

    def job_config(self, job_id: str) -> dict[str, Any]:
        """The persisted creation config (engine, pages, targets, hint)."""

        return self._repository.get_job_config(job_id)

    def job_options(self, job: Job, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """Options dict handed to every stage handler."""

        cfg = config if config is not None else self.job_config(job.job_id)
        return {
            "engine": str(cfg.get("engine") or "native"),
            "targets": tuple(cfg.get("targets") or ("sequences", "activities")),
            "document_type_hint": cfg.get("document_type_hint"),
            "repository_path": str(self.paths.database),
        }

    def review_items(self, job_id: str) -> list[ReviewItem]:
        rows = self._repository.connection.execute(
            "SELECT * FROM review_items WHERE job_id = ? AND status = 'open'",
            (job_id,),
        ).fetchall()
        import json

        return [
            ReviewItem(
                review_id=row["review_id"],
                job_id=row["job_id"],
                record_id=row["record_id"],
                record_revision=row["record_revision"],
                reason_codes=json.loads(row["reason_codes_json"] or "[]"),
                status=row["status"],
                evidence_ids=json.loads(row["evidence_ids_json"] or "[]"),
                proposed_actions=json.loads(row["proposed_actions_json"] or "[]"),
            )
            for row in rows
        ]

    def open_review_count(self, job_id: str) -> int:
        row = self._repository.connection.execute(
            "SELECT COUNT(*) AS n FROM review_items WHERE job_id = ? AND status = 'open'",
            (job_id,),
        ).fetchone()
        return int(row["n"]) if row is not None else 0

    # ---- internals -------------------------------------------------------

    def _build_runner(self) -> WorkflowRunner:
        runner = WorkflowRunner(
            self._repository,
            self.paths.artifact_root,
            required_stages=STAGE_ORDER,
        )
        for handler in build_production_handlers():
            runner.register(handler)
        return runner


__all__ = ["JobService", "JobPaths", "PIPELINE_VERSION"]
