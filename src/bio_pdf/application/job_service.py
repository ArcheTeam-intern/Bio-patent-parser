"""Application service shared by the CLI and the HTTP API.

The service owns the end-to-end job lifecycle: register the document,
create the job row, build a fully-wired runner and run/resume it. Both
surfaces (Typer CLI, FastAPI) call these functions so the domain pipeline
is wired exactly once (review finding 2).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bio_pdf.contracts import Job, ReviewItem
from bio_pdf.config import WorkflowConfig, load_workflow_config
from bio_pdf.errors import JobRunningError, PageRangeInvalidError, WorkflowError
from bio_pdf.ingest.inspector import inspect_pdf
from bio_pdf.storage.artifacts import make_cache_key
from bio_pdf.storage.repository import LeaseToken, Repository
from bio_pdf.workflow.handlers import build_production_handlers
from bio_pdf.workflow.runner import RunResult, WorkflowRunner
from bio_pdf.workflow.stages import STAGE_ORDER

#: Bump when the stage wiring or domain rules change so old cache entries
#: cannot be mixed with new semantics.
PIPELINE_VERSION = "v2"


def effective_workflow_config(persisted: dict[str, Any]) -> WorkflowConfig:
    """Read the WorkflowConfig from a persisted job config row.

    New rows carry ``effective_config``; legacy rows (v1 shape with
    top-level engine/pages/…) are mapped so old jobs stay runnable without
    rewriting their history.
    """

    raw = persisted.get("effective_config")
    if isinstance(raw, dict):
        raw = dict(raw)
        # Legacy engine alias: stored configs written by the pre-B09 API
        # carry "docling"; the canonical name is "docling_sdk".
        if raw.get("parser") == "docling":
            raw["parser"] = "docling_sdk"
        return WorkflowConfig.model_validate(raw)
    # Legacy v1 shape.
    hint = persisted.get("document_type_hint")
    engine = str(persisted.get("engine") or "native")
    if engine == "docling":
        engine = "docling_sdk"
    return WorkflowConfig.model_validate(
        {
            "parser": engine,
            "pages": persisted.get("pages"),
            "document_type_hint": hint if hint in ("paper", "patent") else "auto",
        }
    )


def _require_engine_available(config: WorkflowConfig) -> None:
    """Reject unavailable engines at job creation (plan B04)."""

    import os

    from bio_pdf.errors import EngineUnavailableError

    parser = config.parser
    if parser in ("native",):
        return
    if parser in ("docling", "docling_sdk"):
        from bio_pdf.parsers.docling_adapter import docling_is_available

        if not docling_is_available():
            raise EngineUnavailableError(
                "docling SDK is not installed; engine unavailable",
                details={"parser": parser},
            )
        return
    if parser == "docling_serve":
        base_url = os.environ.get("BIO_PDF_DOCLING_SERVE_URL", "")
        if not base_url:
            raise EngineUnavailableError(
                "docling_serve requires BIO_PDF_DOCLING_SERVE_URL to be configured",
                details={"parser": parser, "code": "ENGINE_UNAVAILABLE"},
            )
        return
    if parser == "mineru_http":
        if not os.environ.get("MINERU_TOKEN"):
            raise EngineUnavailableError(
                "mineru_http requires MINERU_TOKEN to be configured",
                details={"parser": parser, "code": "ENGINE_UNAVAILABLE"},
            )
        return
    raise EngineUnavailableError(
        f"unknown parser: {parser}", details={"parser": parser}
    )


def validate_page_selection(
    page_numbers: tuple[int, ...] | list[int] | None, page_count: int
) -> tuple[int, ...]:
    """Validate an explicit page selection against the real page count.

    Empty/None → all pages. Out-of-range pages and duplicates are
    rejected at creation time (422 at the API layer) — silently dropping
    duplicates produced page sets that differed from the request.
    """

    if not page_numbers:
        return tuple(range(1, page_count + 1))
    pages = tuple(int(p) for p in page_numbers)
    out_of_range = [p for p in pages if p < 1 or p > page_count]
    if out_of_range:
        raise PageRangeInvalidError(
            f"page numbers out of range {out_of_range} (document has {page_count})",
            details={"pages": out_of_range, "page_count": page_count},
        )
    duplicates = sorted({p for p in pages if pages.count(p) > 1})
    if duplicates:
        raise PageRangeInvalidError(
            f"duplicate pages in request: {duplicates}",
            details={"duplicates": duplicates},
        )
    return pages


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
        ocr_policy: str = "auto",
        workflow_config: WorkflowConfig | None = None,
        config_path: Path | str | None = None,
    ) -> tuple[Job, int]:
        """Register the document and enqueue a job. Returns (job, page_count).

        The effective :class:`WorkflowConfig` is loaded (base TOML + API
        overrides), completed with the concrete page set and persisted with
        its hash — this is what stages read and what makes a rerun
        reproducible. ``page_numbers`` may be empty for "all pages"; pages
        outside 1..page_count or duplicates fail at creation time.
        """

        inspection = inspect_pdf(source_path)
        self._repository.upsert_document(
            document_id=inspection.document_id,
            sha256=inspection.sha256,
            source_path=str(source_path.resolve()),
            bytes_count=inspection.bytes,
            metadata={"document_type_hint": document_type_hint},
        )

        if workflow_config is None:
            workflow_config = load_workflow_config(config_path)
        cfg = workflow_config.model_copy(update={"parser": engine})
        if ocr_policy != "auto":
            cfg = cfg.model_copy(update={"ocr_policy": ocr_policy})  # type: ignore[arg-type]
        if document_type_hint:
            cfg = cfg.model_copy(update={"document_type_hint": document_type_hint})  # type: ignore[arg-type]
        if targets:
            # B04: the API/CLI targets argument must reach the effective
            # config — it previously only changed the config hash.
            cfg = cfg.model_copy(update={"targets": tuple(targets)})

        # Engine availability is checked BEFORE enqueue (B04): an
        # unconfigured remote service or a missing SDK is not a silent
        # substitution candidate.
        _require_engine_available(cfg)

        page_count = inspection.page_count
        pages = validate_page_selection(page_numbers, page_count)
        cfg = cfg.model_copy(update={"pages": list(pages)})

        effective = {
            "pipeline": PIPELINE_VERSION,
            "effective_config": cfg.model_dump(mode="json"),
        }
        config_hash = make_cache_key(effective)
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
        self._repository.set_job_config(job.job_id, effective)
        return job, inspection.page_count

    def run_job(
        self,
        job_id: str,
        *,
        resume: bool = False,
        lease_guard: Callable[[], bool] | None = None,
        lease_token: "LeaseToken | None" = None,
    ) -> RunResult:
        """Run (or resume) a job through the full production pipeline.

        ``lease_guard`` (plan T03) is checked before every stage; the
        ``lease_token`` (plan B03) fences the stage-completion and publish
        transactions themselves, so no write slips through between checks.
        """

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

        config = self.job_config(job_id)
        effective = effective_workflow_config(config)
        runner = self._build_runner(
            effective, lease_guard=lease_guard, lease_token=lease_token
        )
        self._repository.update_job_status(job_id, "running", current_stage=None)
        result = runner.run(
            job,
            source_path=str(row["source_path"]),
            page_numbers=tuple(effective.pages or ()),
            resume=resume,
            options=self.job_options(job, config),
        )
        return result

    def requeue_resume(self, job_id: str) -> None:
        """Persist the resume intent and requeue the job (plan T03).

        A running job with a VALID lease refuses (JOB_RUNNING) — resetting
        it to queued would let a second worker double-claim. An expired
        lease (crashed worker) may be requeued; the reclaim bumps the
        generation and the worker resumes from verified cache.
        """

        job = self._repository.get_job(job_id)
        if job is None:
            raise WorkflowError(f"job not found: {job_id}", details={"job_id": job_id})
        if job.status == "running":
            row = self._repository.connection.execute(
                "SELECT lease_until FROM worker_leases WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            lease_until = row["lease_until"] if row else None
            if lease_until and lease_until > datetime.now(tz=timezone.utc).isoformat():
                raise JobRunningError(
                    "job is running and its lease is still valid",
                    details={"job_id": job_id, "lease_until": lease_until},
                )
        config = self.job_config(job_id)
        config["execution_mode"] = "resume"
        self._repository.set_job_config(job_id, config)
        self._repository.update_job_status(job_id, "queued")

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
        """Options dict handed to every stage handler.

        Stages read their slice via effective_options_for_stage; the full
        effective config travels under ``workflow_config``.
        """

        cfg = config if config is not None else self.job_config(job.job_id)
        effective = effective_workflow_config(cfg)
        return {
            "engine": effective.parser,
            "targets": tuple(effective.targets),
            "document_type_hint": effective.document_type_hint,
            "workflow_config": effective.model_dump(mode="json"),
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

    def _build_runner(
        self,
        effective: WorkflowConfig | None = None,
        *,
        lease_guard: Callable[[], bool] | None = None,
        lease_token: "LeaseToken | None" = None,
    ) -> WorkflowRunner:
        runner = WorkflowRunner(
            self._repository,
            self.paths.artifact_root,
            required_stages=STAGE_ORDER,
            workflow_config=effective,
            lease_guard=lease_guard,
            lease_token=lease_token,
        )
        for handler in build_production_handlers():
            runner.register(handler)
        return runner


__all__ = ["JobService", "JobPaths", "PIPELINE_VERSION"]
