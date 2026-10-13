"""Runner safety tests for the review-report findings (issue 13).

Covers: missing required stages fail loudly, review_required aggregates into
the job status, and resume re-runs stages whose cached artifacts were
corrupted on disk.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from bio_pdf.contracts import ArtifactRef, Job, StageOutcome
from bio_pdf.errors import WorkflowError
from bio_pdf.storage.artifacts import atomic_write_bytes
from bio_pdf.storage.repository import Repository
from bio_pdf.workflow.runner import WorkflowRunner
from bio_pdf.workflow.stages import STAGE_ORDER, StageContext


class _CountingHandler:
    name = "native_parse"

    def __init__(self, *, artifact_rel: str | None = None, review: int = 0) -> None:
        self.calls = 0
        self.artifact_rel = artifact_rel
        self.review = review

    def cache_key(self, context: StageContext) -> str:
        return f"{self.name}:{context.document_id}"

    def run(self, context: StageContext) -> StageOutcome:
        self.calls += 1
        refs = []
        if self.artifact_rel is not None:
            out_dir = Path(context.output_dir) / "native"
            ref = atomic_write_bytes(out_dir, "out.bin", b"payload")
            refs.append(
                ArtifactRef(
                    artifact_id=ref.artifact_id,
                    sha256=ref.sha256,
                    relative_path=f"jobs/{context.job_id}/native/out.bin",
                    media_type="application/octet-stream",
                )
            )
        return StageOutcome(artifact_refs=refs, review_required=self.review)


class _ExportNoop:
    name = "export"

    def cache_key(self, context: StageContext) -> str:
        return f"export:{context.document_id}"

    def run(self, context: StageContext) -> StageOutcome:
        return StageOutcome(artifact_refs=[], review_required=0)


@pytest.fixture()
def repo(tmp_path: Path):
    r = Repository(tmp_path / "bio.sqlite")
    now = datetime.now(tz=timezone.utc)
    r.upsert_document(
        document_id="sha256:doc", sha256="a" * 64,
        source_path="/tmp/x.pdf", bytes_count=1,
    )
    r.create_job(Job(
        job_id="job-1", document_id="sha256:doc",
        config_hash="cfg", created_at=now, updated_at=now,
    ))
    yield r
    r.close()


def _job() -> Job:
    now = datetime.now(tz=timezone.utc)
    return Job(
        job_id="job-1", document_id="sha256:doc",
        config_hash="cfg", created_at=now, updated_at=now,
    )


def test_missing_required_stage_fails_loudly(repo: Repository, tmp_path: Path) -> None:
    runner = WorkflowRunner(repo, tmp_path, required_stages=STAGE_ORDER)
    runner.register(_CountingHandler())
    with pytest.raises(WorkflowError, match="required stages"):
        runner.run(_job(), source_path="/tmp/x.pdf", page_numbers=(1,))


def test_review_required_yields_succeeded_with_review(
    repo: Repository, tmp_path: Path
) -> None:
    runner = WorkflowRunner(repo, tmp_path)
    runner.register(_CountingHandler(review=3))
    runner.register(_ExportNoop())
    result = runner.run(_job(), source_path="/tmp/x.pdf", page_numbers=(1,))
    assert result.status == "succeeded_with_review"
    assert result.review_count == 3
    job = repo.get_job("job-1")
    assert job is not None
    assert job.status == "succeeded_with_review"


def test_resume_reruns_stage_with_corrupted_artifact(
    repo: Repository, tmp_path: Path
) -> None:
    handler = _CountingHandler(artifact_rel="native/out.bin")
    runner = WorkflowRunner(repo, tmp_path)
    runner.register(handler)
    runner.register(_ExportNoop())
    first = runner.run(_job(), source_path="/tmp/x.pdf", page_numbers=(1,))
    assert first.status == "succeeded"
    assert handler.calls == 1

    # Corrupt the cached artifact behind the runner's back.
    artifact_file = tmp_path / "jobs" / "job-1" / "native" / "out.bin"
    artifact_file.write_bytes(b"tampered")

    resumed = WorkflowRunner(repo, tmp_path)
    resumed.register(handler)
    resumed.register(_ExportNoop())
    result = resumed.run(
        _job(), source_path="/tmp/x.pdf", page_numbers=(1,), resume=True
    )
    assert result.status == "succeeded"
    assert handler.calls == 2  # the corrupted stage re-ran


def test_resume_trusts_intact_artifact(repo: Repository, tmp_path: Path) -> None:
    handler = _CountingHandler(artifact_rel="native/out.bin")
    runner = WorkflowRunner(repo, tmp_path)
    runner.register(handler)
    runner.register(_ExportNoop())
    runner.run(_job(), source_path="/tmp/x.pdf", page_numbers=(1,))
    assert handler.calls == 1

    resumed = WorkflowRunner(repo, tmp_path)
    resumed.register(handler)
    resumed.register(_ExportNoop())
    result = resumed.run(
        _job(), source_path="/tmp/x.pdf", page_numbers=(1,), resume=True
    )
    assert result.status == "succeeded"
    assert handler.calls == 1  # cache verified intact — not re-run
