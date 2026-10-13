"""Workflow runner.

A deterministic state machine over the fixed :data:`STAGE_ORDER`. It records
one ``stage_runs`` row per attempt, skips already-succeeded stages on resume
(by cache key), and never marks the job succeeded until the ``export`` stage
has actually completed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bio_pdf.contracts import Job, Stage, StageOutcome
from bio_pdf.errors import WorkflowError
from bio_pdf.storage.repository import Repository
from bio_pdf.workflow.stages import STAGE_ORDER, StageContext, StageHandler


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass
class RunResult:
    """Summary of a runner invocation."""

    job_id: str
    status: str
    stages_run: list[str] = field(default_factory=list)
    review_count: int = 0
    failed_stage: str | None = None


class WorkflowRunner:
    """Executes registered stage handlers in the fixed order.

    Args:
        repository: The SQLite repository for job/stage persistence.
        artifact_root: Root directory for stage artifacts.
        max_attempts: Maximum attempts per stage (default 2).
    """

    def __init__(
        self,
        repository: Repository,
        artifact_root: Path,
        *,
        max_attempts: int = 2,
    ) -> None:
        self.repository = repository
        self.artifact_root = artifact_root
        self.max_attempts = max_attempts
        self._handlers: dict[str, StageHandler] = {}

    def register(self, handler: StageHandler) -> None:
        if handler.name not in STAGE_ORDER:
            raise WorkflowError(
                f"stage {handler.name!r} is not in the fixed stage order",
                details={"stage": handler.name},
            )
        self._handlers[handler.name] = handler

    def _context(self, job: Job, source_path: str, page_numbers: tuple[int, ...]) -> StageContext:
        return StageContext(
            job_id=job.job_id,
            document_id=job.document_id,
            source_path=source_path,
            page_numbers=page_numbers,
            output_dir=str(self.artifact_root / "jobs" / job.job_id),
            options={},
        )

    def run(
        self,
        job: Job,
        *,
        source_path: str,
        page_numbers: tuple[int, ...],
        resume: bool = False,
    ) -> RunResult:
        """Run the job through all registered stages.

        When ``resume`` is True, a stage whose cache key already has a
        succeeded run is skipped (its artifacts are trusted, not re-derived).
        """

        stages_run: list[str] = []
        context = self._context(job, source_path, page_numbers)

        for stage_name in STAGE_ORDER:
            handler = self._handlers.get(stage_name)
            if handler is None:
                continue

            cache_key = handler.cache_key(context)

            # Resume: skip a stage we already completed with this cache key.
            if resume and self.repository.find_successful_stage(
                job.job_id, stage_name, cache_key
            ) is not None:
                continue

            succeeded = self._run_stage(job, handler, context, cache_key)
            if not succeeded:
                self.repository.update_job_status(
                    job.job_id, "failed", current_stage=stage_name
                )
                return RunResult(
                    job_id=job.job_id,
                    status="failed",
                    stages_run=stages_run,
                    failed_stage=stage_name,
                )

            stages_run.append(stage_name)

        # Only after every stage (incl. export) completes do we mark success.
        self.repository.update_job_status(job.job_id, "succeeded", current_stage=None)
        return RunResult(
            job_id=job.job_id,
            status="succeeded",
            stages_run=stages_run,
        )

    def _run_stage(
        self,
        job: Job,
        handler: StageHandler,
        context: StageContext,
        cache_key: str,
    ) -> bool:
        """Run one stage with bounded attempts. Returns True on success."""

        for attempt in range(1, self.max_attempts + 1):
            stage_run_id = f"{job.job_id}:{handler.name}:{attempt}:{uuid.uuid4().hex[:8]}"
            stage = Stage(
                stage_run_id=stage_run_id,
                job_id=job.job_id,
                stage_name=handler.name,
                attempt=attempt,
                status="running",
                cache_key=cache_key,
                started_at=_utcnow(),
            )
            self.repository.start_stage(stage)

            try:
                outcome = handler.run(context)
                manifest_ref = self._first_artifact_ref(outcome)
                self.repository.complete_stage(stage_run_id, artifact_manifest_ref=manifest_ref)
                return True
            except WorkflowError as exc:
                self.repository.fail_stage(
                    stage_run_id,
                    error_json={"code": exc.code, "message": exc.message},
                )
                if attempt >= self.max_attempts:
                    return False
            except Exception as exc:  # noqa: BLE001 - defensive failure isolation
                self.repository.fail_stage(
                    stage_run_id,
                    error_json={"code": "STAGE_ERROR", "message": str(exc)},
                )
                if attempt >= self.max_attempts:
                    return False

        return False

    @staticmethod
    def _first_artifact_ref(outcome: StageOutcome) -> str | None:
        if not outcome.artifact_refs:
            return None
        return outcome.artifact_refs[0].artifact_id


__all__ = ["WorkflowRunner", "RunResult"]