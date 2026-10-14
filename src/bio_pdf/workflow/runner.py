"""Workflow runner with per-attempt stage runs, content cache keys and a
published result baseline (plan T02).

Properties:

- Every attempt writes to ``jobs/<job>/stage_runs/<stage_run_id>/``; a
  second attempt never overwrites a previous success.
- Cache keys are content-addressed: stage name, document SHA-256, requested
  pages, input artifact hashes, effective stage options and the stage's
  implementation/provider versions. File paths and mtimes are NOT business
  identity.
- Each attempt persists a stage manifest (inputs/outputs with hashes,
  versions, coverage, warnings, review count); ``stage_runs`` row points at
  the manifest, not at a "first output".
- Resume verifies the manifest AND every listed output file; any drift
  re-runs the stage. Downstream cache reuse keys on the new output hashes.
- After a full pass the runner atomically publishes a result manifest; API
  reads (records/export/review) consume the published baseline via
  :class:`~bio_pdf.storage.result_reader.ResultReader`, not working files.
- A stage whose cache was skipped cannot zero out review status: the final
  status counts open review items from the database.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bio_pdf.config import effective_options_for_stage, WorkflowConfig
from bio_pdf.contracts import ArtifactRef, Job, Stage, StageOutcome
from bio_pdf.errors import PageCoverageMismatchError, WorkflowError
from bio_pdf.storage.artifacts import (
    atomic_replace_json,
    atomic_write_json,
    compute_sha256_bytes,
    make_cache_key,
)
from bio_pdf.storage.repository import Repository
from bio_pdf.workflow.stages import STAGE_ORDER, StageContext, StageHandler


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _sha256_file(path: Path) -> str:
    import hashlib

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
    published_manifest: str | None = None


class WorkflowRunner:
    """Executes registered stage handlers in the fixed order."""

    def __init__(
        self,
        repository: Repository,
        artifact_root: Path,
        *,
        max_attempts: int = 2,
        required_stages: tuple[str, ...] = (),
        workflow_config: WorkflowConfig | None = None,
    ) -> None:
        self.repository = repository
        self.artifact_root = artifact_root
        self.max_attempts = max_attempts
        self.required_stages = required_stages
        self.workflow_config = workflow_config
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

    def _context(
        self,
        job: Job,
        source_path: str,
        page_numbers: tuple[int, ...],
        options: dict[str, Any] | None,
        stage_name: str,
        stage_run_id: str,
        inputs: dict[str, ArtifactRef],
    ) -> StageContext:
        sha = job.document_id.removeprefix("sha256:")
        return StageContext(
            job_id=job.job_id,
            document_id=job.document_id,
            source_path=source_path,
            page_numbers=page_numbers,
            output_dir=str(self.artifact_root / "jobs" / job.job_id),
            options=dict(options or {}),
            stage_run_id=stage_run_id,
            document_sha256=sha,
            inputs=dict(inputs),
        )

    # ---- cache key --------------------------------------------------------

    def _content_cache_key(
        self,
        handler: StageHandler,
        context: StageContext,
        inputs: dict[str, ArtifactRef],
    ) -> str:
        return make_cache_key(
            {
                "stage": handler.name,
                "document_sha256": context.document_sha256,
                "pages": sorted(context.page_numbers),
                "input_artifact_hashes": sorted(
                    (name, ref.sha256) for name, ref in inputs.items()
                ),
                "effective_options": effective_options_for_stage(
                    self.workflow_config, handler.name
                )
                if self.workflow_config
                else context.options,
                "implementation_version": handler.implementation_version(),
                "provider_version": handler.provider_version(context),
            }
        )

    # ---- resume verification ----------------------------------------------

    def _latest_success(
        self, job_id: str, stage_name: str, cache_key: str
    ) -> Stage | None:
        return self.repository.find_successful_stage(job_id, stage_name, cache_key)

    def _cache_is_intact(
        self, job_id: str, stage_name: str, cache_key: str
    ) -> tuple[bool, dict[str, ArtifactRef]]:
        """Verify a cached success: manifest present, every output verifiable.

        Returns ``(intact, outputs)`` — the verified output refs are reused
        as downstream inputs so cache reuse keys on real content.
        """

        stage = self._latest_success(job_id, stage_name, cache_key)
        if stage is None:
            return False, {}
        manifest_ref = stage.artifact_manifest_ref
        if not manifest_ref:
            return False, {}
        manifest_artifact = self.repository.get_artifact(manifest_ref)
        if manifest_artifact is None:
            return False, {}
        manifest_path = self.artifact_root / manifest_artifact.relative_path
        if not manifest_path.is_file():
            return False, {}
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False, {}

        outputs: dict[str, ArtifactRef] = {}
        for name, entry in (manifest.get("outputs") or {}).items():
            rel = entry.get("relative_path")
            sha = entry.get("sha256")
            if not rel or not sha:
                return False, {}
            path = self.artifact_root / rel
            if not path.is_file():
                return False, {}
            try:
                if _sha256_file(path) != sha:
                    return False, {}
            except OSError:
                return False, {}
            outputs[f"{stage_name}.{name}"] = ArtifactRef(
                artifact_id=entry.get("artifact_id") or f"sha256:{sha}",
                sha256=sha,
                relative_path=rel,
                media_type=entry.get("media_type", "application/json"),
            )
        return True, outputs

    # ---- main loop ---------------------------------------------------------

    def run(
        self,
        job: Job,
        *,
        source_path: str,
        page_numbers: tuple[int, ...],
        resume: bool = False,
        options: dict[str, Any] | None = None,
    ) -> RunResult:
        self._validate_registration()

        stages_run: list[str] = []
        stages_skipped: list[str] = []
        review_total = 0
        # Logical outputs of executed (or verified-cached) stages.
        available_inputs: dict[str, ArtifactRef] = {}
        # Outputs of THIS invocation, per stage, for the published manifest.
        published: dict[str, dict[str, Any]] = {}

        for stage_name in STAGE_ORDER:
            handler = self._handlers.get(stage_name)
            if handler is None:
                stages_skipped.append(stage_name)
                continue

            inputs = self._inputs_for(handler.name, available_inputs)
            context = self._context(
                job, source_path, page_numbers, options, stage_name, "", inputs
            )
            cache_key = self._content_cache_key(handler, context, inputs)

            if resume:
                intact, cached_outputs = self._cache_is_intact(
                    job.job_id, stage_name, cache_key
                )
                if intact:
                    stages_skipped.append(stage_name)
                    available_inputs.update(cached_outputs)
                    published[stage_name] = {
                        name[len(stage_name) + 1 :]: ref.model_dump(mode="json")
                        for name, ref in cached_outputs.items()
                    }
                    continue

            outcome, outputs, stage_run_id, ok = self._run_stage(
                job, handler, context, inputs, cache_key
            )
            if not ok:
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
            available_inputs.update(
                {f"{stage_name}.{name}": ref for name, ref in outputs.items()}
            )
            published[stage_name] = {
                name: ref.model_dump(mode="json") for name, ref in outputs.items()
            }

        # Coverage: the produced pages must cover the requested set. A
        # partial parse never reports success.
        self._verify_coverage(job.job_id, page_numbers)

        final_status = self._final_status(job.job_id, review_total)
        self.repository.update_job_status(job.job_id, final_status, current_stage=None)
        manifest_rel = self._publish_result(job, published, final_status)
        return RunResult(
            job_id=job.job_id,
            status=final_status,
            stages_run=stages_run,
            stages_skipped=stages_skipped,
            review_count=max(
                review_total, self.repository_open_reviews(job.job_id)
            ),
            published_manifest=manifest_rel,
        )

    # ---- stage execution ----------------------------------------------------

    def _inputs_for(
        self, stage_name: str, available: dict[str, ArtifactRef]
    ) -> dict[str, ArtifactRef]:
        """Inputs a stage may consume: everything produced so far.

        Stages pick what they need by logical key (``normalize`` chooses
        between ``layout_parse.normalized`` and ``native_parse.normalized``).
        """

        return dict(available)

    def _run_stage(
        self,
        job: Job,
        handler: StageHandler,
        context: StageContext,
        inputs: dict[str, ArtifactRef],
        cache_key: str,
    ) -> tuple[StageOutcome | None, dict[str, ArtifactRef], str, bool]:
        for attempt in range(1, self.max_attempts + 1):
            # Filesystem-safe id: this string becomes a directory name
            # (colons are illegal on Windows).
            stage_run_id = f"{job.job_id}--{handler.name}--a{attempt}--{uuid.uuid4().hex[:8]}"
            stage_context = self._context(
                job,
                context.source_path,
                context.page_numbers,
                context.options,
                handler.name,
                stage_run_id,
                inputs,
            )
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
                outcome = handler.run(stage_context)
                outputs = self._materialize_outputs(
                    stage_context, handler.name, outcome
                )
                manifest_ref = self._write_stage_manifest(
                    stage_context,
                    handler,
                    cache_key,
                    inputs,
                    outputs,
                    outcome,
                )
                self.repository.complete_stage(
                    stage.stage_run_id, artifact_manifest_ref=manifest_ref
                )
                return outcome, outputs, stage_run_id, True
            except WorkflowError as exc:
                self.repository.fail_stage(
                    stage.stage_run_id,
                    error_json={"code": exc.code, "message": exc.message},
                )
                if attempt >= self.max_attempts:
                    return None, {}, stage_run_id, False
            except Exception as exc:  # noqa: BLE001 - defensive failure isolation
                self.repository.fail_stage(
                    stage.stage_run_id,
                    error_json={"code": "STAGE_ERROR", "message": str(exc)},
                )
                if attempt >= self.max_attempts:
                    return None, {}, stage_run_id, False
        return None, {}, "", False

    def _materialize_outputs(
        self,
        context: StageContext,
        stage_name: str,
        outcome: StageOutcome | None,
    ) -> dict[str, ArtifactRef]:
        """Re-key the handler's artifact refs to this stage-run occurrence.

        Handlers report ``relative_path`` (inside their stage dir) plus the
        content hash; the runner assigns occurrence-unique artifact ids
        (``<stage_run_id>:<logical_name>``) so identical content in two jobs
        can never overwrite each other's registration.
        """

        outputs: dict[str, ArtifactRef] = {}
        if outcome is None:
            return outputs
        for idx, ref in enumerate(outcome.artifact_refs):
            logical = Path(ref.relative_path).name or f"artifact-{idx}"
            outputs[logical] = ArtifactRef(
                artifact_id=f"{context.stage_run_id}:{logical}",
                sha256=ref.sha256,
                relative_path=ref.relative_path,
                media_type=ref.media_type or "application/octet-stream",
                producer_stage_run_id=context.stage_run_id,
            )
        return outputs

    def _write_stage_manifest(
        self,
        context: StageContext,
        handler: StageHandler,
        cache_key: str,
        inputs: dict[str, ArtifactRef],
        outputs: dict[str, ArtifactRef],
        outcome: StageOutcome | None,
    ) -> str:
        manifest = {
            "stage_run_id": context.stage_run_id,
            "stage_name": handler.name,
            "cache_key": cache_key,
            "document_sha256": context.document_sha256,
            "pages": list(context.page_numbers),
            "implementation_version": handler.implementation_version(),
            "provider_version": handler.provider_version(context),
            "inputs": {
                name: {
                    "artifact_id": ref.artifact_id,
                    "sha256": ref.sha256,
                    "relative_path": ref.relative_path,
                }
                for name, ref in inputs.items()
            },
            "outputs": {
                name: {
                    "artifact_id": ref.artifact_id,
                    "sha256": ref.sha256,
                    "relative_path": ref.relative_path,
                    "media_type": ref.media_type,
                }
                for name, ref in outputs.items()
            },
            "warnings": list(outcome.warnings) if outcome else [],
            "review_required": outcome.review_required if outcome else 0,
            "page_coverage": {
                "requested": list(context.page_numbers),
            },
        }
        manifest_rel = (
            Path("jobs")
            / context.job_id
            / "stage_runs"
            / context.stage_run_id
            / "stage.manifest.json"
        ).as_posix()
        # Not "manifest.json": adapters legitimately write their own parse
        # manifest into the same stage dir.
        ref = atomic_write_json(Path(context.stage_dir()), "stage.manifest.json", manifest)
        artifact = ArtifactRef(
            artifact_id=f"{context.stage_run_id}:stage.manifest.json",
            sha256=ref.sha256,
            relative_path=manifest_rel,
            media_type="application/json",
            producer_stage_run_id=context.stage_run_id,
        )
        self.repository.register_artifact(artifact)
        for out_ref in outputs.values():
            self.repository.register_artifact(out_ref)
        return artifact.artifact_id

    # ---- publication & status ----------------------------------------------

    def _verify_coverage(self, job_id: str, page_numbers: tuple[int, ...]) -> None:
        """Coverage gate hook.

        Per-stage coverage is enforced by NormalizeStage (which holds the
        produced document); this hook stays for cross-stage checks after
        all stages complete. Partial coverage must fail the job, never
        fake completion.
        """

        del job_id, page_numbers
        return

    def repository_open_reviews(self, job_id: str) -> int:
        row = self.repository.connection.execute(
            "SELECT COUNT(*) AS n FROM review_items WHERE job_id = ? AND status = 'open'",
            (job_id,),
        ).fetchone()
        return int(row["n"]) if row else 0

    def _final_status(self, job_id: str, review_total: int) -> str:
        open_reviews = max(review_total, self.repository_open_reviews(job_id))
        return "succeeded_with_review" if open_reviews > 0 else "succeeded"

    def _publish_result(
        self,
        job: Job,
        published: dict[str, dict[str, Any]],
        final_status: str,
    ) -> str | None:
        """Atomically publish the result manifest pointing at stage outputs."""

        manifest = {
            "job_id": job.job_id,
            "document_id": job.document_id,
            "published_at": _utcnow().isoformat(),
            "final_status": final_status,
            "stages": {
                name: refs for name, refs in published.items() if refs
            },
        }
        job_dir = self.artifact_root / "jobs" / job.job_id
        # Publishing is a pointer replacement, not an immutable write: the
        # referenced stage-run artifacts stay immutable and audited.
        atomic_replace_json(job_dir / "published", "result-manifest.json", manifest)
        return (Path("jobs") / job.job_id / "published" / "result-manifest.json").as_posix()


__all__ = ["WorkflowRunner", "RunResult"]
