"""Workflow runner.

A deterministic state machine over the fixed :data:`STAGE_ORDER`. It records
one ``stage_runs`` row per attempt, skips already-succeeded stages on resume
(only after verifying the cached artifacts still exist with matching hashes),
and never marks the job succeeded unless every *required* stage completed.

Aggregation rules (fix for review finding 13):

- A stage with no registered handler that is in ``required_stages`` fails
  the run instead of being silently skipped.
- ``review_required`` counts from every executed stage aggregate into the
  job result: any pending review yields ``succeeded_with_review``, never a
  bare ``succeeded``.
- Resume trusts a cached stage only when its first artifact still exists and
  its SHA-256 matches the registered hash; a corrupted or missing artifact
  forces a re-run.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bio_pdf.contracts import ArtifactRef, Job, Stage, StageOutcome
from bio_pdf.errors import WorkflowError
from bio_pdf.storage.repository import Repository
from bio_pdf.workflow.stages import STAGE_ORDER, StageContext, StageHandler


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class RunResult:
    """Summary of a runner invocation."""

    job_id: str
    status: str
    stages_run: list[str] = field(default_factory=list)
    stages_skipped: list[str] = field(default_factory=list)
    review_count: int = 0
    failed_stage: str | None = None


class WorkflowRunner:
    """Executes registered stage handlers in the fixed order.

    Args:
        repository: The SQLite repository for job/stage persistence.
        artifact_root: Root directory for stage artifacts.
        max_attempts: Maximum attempts per stage (default 2).
        required_stages: Stage names that MUST have handlers. ``run``
            refuses to start when one is missing, so an incompletely wired
            pipeline can never report success. The default is empty for
            backwards compatibility with partial test harnesses; production
            wiring MUST pass the full :data:`STAGE_ORDER`.
    """

    def __init__(
        self,
        repository: Repository,
        artifact_root: Path,
        *,
        max_attempts: int = 2,
        required_stages: tuple[str, ...] = (),
    ) -> None:
        self.repository = repository
        self.artifact_root = artifact_root
        self.max_attempts = max_attempts
        self.required_stages = required_stages
        self._handlers: dict[str, StageHandler] = {}

    def register(self, handler: StageHandler) -> None:
        if handler.name not in STAGE_ORDER:
            raise WorkflowError(
                f"stage {handler.name!r} is not in the fixed stage order",
                details={"stage": handler.name},
            )
        self._handlers[handler.name] = handler

    def _validate_registration(self) -> None:
        missing = [s for s in self.required_stages if s not in self._handlers]
        if missing:
            raise WorkflowError(
                f"required stages have no registered handler: {missing}",
                details={"missing": missing, "registered": sorted(self._handlers)},
            )

    def _context(self, job: Job, source_path: str, page_numbers: tuple[int, ...], options: dict[str, Any] | None = None) -> StageContext:
        return StageContext(
            job_id=job.job_id,
            document_id=job.document_id,
            source_path=source_path,
            page_numbers=page_numbers,
            output_dir=str(self.artifact_root / "jobs" / job.job_id),
            options=dict(options or {}),
        )

    def run(
        self,
        job: Job,
        *,
        source_path: str,
        page_numbers: tuple[int, ...],
        resume: bool = False,
        options: dict[str, Any] | None = None,
    ) -> RunResult:
        """Run the job through all registered stages.

        When ``resume`` is True, a stage whose cache key already has a
        succeeded run AND whose registered artifact still hashes correctly
        is skipped; otherwise the stage re-executes.
        """

        self._validate_registration()

        stages_run: list[str] = []
        stages_skipped: list[str] = []
        review_total = 0
        context = self._context(job, source_path, page_numbers, options)

        for stage_name in STAGE_ORDER:
            handler = self._handlers.get(stage_name)
            if handler is None:
                # Not required (e.g. layout_parse for a native-only profile):
                # record an explicit skipped stage, never a fake success.
                stages_skipped.append(stage_name)
                continue

            cache_key = handler.cache_key(context)

            # Resume: skip a stage we already completed with this cache key
            # whose artifacts still verify; quarantine a corrupted cache so
            # the re-run can rewrite the same paths.
            if resume and self._cache_is_intact(job.job_id, stage_name, cache_key):
                stages_skipped.append(stage_name)
                continue
            if resume:
                self._discard_corrupt_cache(job.job_id, stage_name, cache_key)

            outcome, succeeded = self._run_stage(job, handler, context, cache_key)
            if not succeeded:
                self.repository.update_job_status(
                    job.job_id, "failed", current_stage=stage_name
                )
                return RunResult(
                    job_id=job.job_id,
                    status="failed",
                    stages_run=stages_run,
                    review_count=review_total,
                    failed_stage=stage_name,
                )

            review_total += outcome.review_required if outcome is not None else 0
            stages_run.append(stage_name)

        # Only after every required stage completes do we mark success — and
        # pending human review downgrades the status explicitly.
        final_status = "succeeded_with_review" if review_total > 0 else "succeeded"
        self.repository.update_job_status(job.job_id, final_status, current_stage=None)
        return RunResult(
            job_id=job.job_id,
            status=final_status,
            stages_run=stages_run,
            stages_skipped=stages_skipped,
            review_count=review_total,
        )

    def _cache_is_intact(self, job_id: str, stage_name: str, cache_key: str) -> bool:
        """Return True when the cached success and its first artifact verify.

        A stage whose registered artifact file is missing or has a different
        hash must re-run; trusting a corrupted cache produced the "fake
        resume" failure in the implementation review.
        """

        stage = self.repository.find_successful_stage(job_id, stage_name, cache_key)
        if stage is None:
            return False

        artifact_ref = stage.artifact_manifest_ref
        if not artifact_ref:
            # No artifact to verify: the stage's own success row is the only
            # evidence. Keep trusting the row but do not claim file integrity.
            return True

        artifact = self.repository.get_artifact(artifact_ref)
        if artifact is None:
            return False

        path = self.artifact_root / artifact.relative_path
        if not path.is_file():
            return False
        try:
            return _sha256_file(path) == artifact.sha256
        except OSError:
            return False

    def _discard_corrupt_cache(self, job_id: str, stage_name: str, cache_key: str) -> None:
        """Delete the corrupted artifact files of a failed cache verification.

        The artifact store refuses to write different content to an existing
        path, so a re-run after tampering requires removing the corrupt file
        first. The stale stage row stays as audit history; a new row from the
        re-run supersedes it (``find_successful_stage`` picks the newest).
        """

        stage = self.repository.find_successful_stage(job_id, stage_name, cache_key)
        if stage is None or not stage.artifact_manifest_ref:
            return
        artifact = self.repository.get_artifact(stage.artifact_manifest_ref)
        if artifact is None:
            return
        path = self.artifact_root / artifact.relative_path
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass

    def _run_stage(
        self,
        job: Job,
        handler: StageHandler,
        context: StageContext,
        cache_key: str,
    ) -> tuple[StageOutcome | None, bool]:
        """Run one stage with bounded attempts. Returns (outcome, success)."""

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
                # Register every produced artifact so resume can verify the
                # file bytes, not just the row's existence.
                for ref in outcome.artifact_refs:
                    self.repository.register_artifact(ref)
                self.repository.complete_stage(stage_run_id, artifact_manifest_ref=manifest_ref)
                return outcome, True
            except WorkflowError as exc:
                self.repository.fail_stage(
                    stage_run_id,
                    error_json={"code": exc.code, "message": exc.message},
                )
                if attempt >= self.max_attempts:
                    return None, False
            except Exception as exc:  # noqa: BLE001 - defensive failure isolation
                self.repository.fail_stage(
                    stage_run_id,
                    error_json={"code": "STAGE_ERROR", "message": str(exc)},
                )
                if attempt >= self.max_attempts:
                    return None, False

        return None, False

    @staticmethod
    def _first_artifact_ref(outcome: StageOutcome) -> str | None:
        if not outcome.artifact_refs:
            return None
        return outcome.artifact_refs[0].artifact_id


__all__ = ["WorkflowRunner", "RunResult"]
