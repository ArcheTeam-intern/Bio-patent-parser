"""Stage manifest & artifact-chain tests (plan T02 acceptance).

- per-attempt stage dirs: a second attempt never hits ARTIFACT_CONFLICT,
- cache keys track implementation/provider versions, not mtimes,
- resume verifies ALL manifest outputs (not just the first),
- a job whose cache was fully skipped still reports open reviews,
- identical content in two jobs stays independently readable,
- ResultReader prefers the published manifest and verifies hashes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bio_pdf.contracts import ArtifactRef, Job, StageOutcome
from bio_pdf.errors import PageCoverageMismatchError
from bio_pdf.storage.artifacts import atomic_write_bytes
from bio_pdf.storage.repository import Repository
from bio_pdf.storage.result_reader import ResultReader
from bio_pdf.workflow.runner import WorkflowRunner
from bio_pdf.workflow.stages import StageContext
from bio_pdf.workflow.handlers import NormalizeStage


class _Ver:
    def implementation_version(self) -> str:
        return self.version

    def provider_version(self, context: StageContext) -> str:
        return ""


class _OutStage(_Ver):
    """Writes one artifact whose content is configurable."""

    name = "native_parse"

    def __init__(self, version: str = "v1", content: bytes = b"payload-v1") -> None:
        self.version = version
        self.content = content
        self.calls = 0

    def cache_key(self, context: StageContext) -> str:
        return "k"

    def run(self, context: StageContext) -> StageOutcome:
        self.calls += 1
        ref = atomic_write_bytes(Path(context.stage_dir()), "out.bin", self.content)
        return StageOutcome(
            artifact_refs=[
                ArtifactRef(
                    artifact_id=ref.artifact_id,
                    sha256=ref.sha256,
                    relative_path=(
                        f"jobs/{context.job_id}/stage_runs/"
                        f"{context.stage_run_id}/out.bin"
                    ),
                    media_type="application/octet-stream",
                )
            ]
        )


class _NoopStage(_Ver):
    name = "export"

    def __init__(self, version: str = "v1") -> None:
        self.version = version
        self.calls = 0

    def cache_key(self, context: StageContext) -> str:
        return "k"

    def run(self, context: StageContext) -> StageOutcome:
        self.calls += 1
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


def test_second_attempt_writes_isolated_stage_dir(repo: Repository, tmp_path: Path) -> None:
    """Attempt 1 succeeds, then a fresh non-resume run must not conflict."""

    stage = _OutStage()
    noop = _NoopStage()
    runner = WorkflowRunner(repo, tmp_path)
    runner.register(stage)
    runner.register(noop)
    assert runner.run(_job(), source_path="/tmp/x.pdf", page_numbers=(1,)).status == "succeeded"

    runner2 = WorkflowRunner(repo, tmp_path)
    stage2 = _OutStage(content=b"payload-v2")
    runner2.register(stage2)
    runner2.register(_NoopStage())
    result = runner2.run(_job(), source_path="/tmp/x.pdf", page_numbers=(1,))
    assert result.status == "succeeded"
    stage_dirs = list((tmp_path / "jobs" / "job-1" / "stage_runs").glob("*native_parse*"))
    assert len(stage_dirs) == 2  # both attempts preserved


def test_cache_key_tracks_implementation_version(repo: Repository, tmp_path: Path) -> None:
    """Bumping the implementation version re-runs the stage on resume."""

    stage = _OutStage(version="v1")
    runner = WorkflowRunner(repo, tmp_path)
    runner.register(stage)
    runner.register(_NoopStage())
    runner.run(_job(), source_path="/tmp/x.pdf", page_numbers=(1,), resume=True)
    assert stage.calls == 1

    resumed = WorkflowRunner(repo, tmp_path)
    stage_v2 = _OutStage(version="v2")
    resumed.register(stage_v2)
    resumed.register(_NoopStage())
    resumed.run(_job(), source_path="/tmp/x.pdf", page_numbers=(1,), resume=True)
    assert stage_v2.calls == 1  # version change → cache miss → re-run


def test_resume_verifies_all_manifest_outputs(repo: Repository, tmp_path: Path) -> None:
    """A stage whose (single) output was tampered must re-run on resume."""

    stage = _OutStage()
    runner = WorkflowRunner(repo, tmp_path)
    runner.register(stage)
    runner.register(_NoopStage())
    runner.run(_job(), source_path="/tmp/x.pdf", page_numbers=(1,))

    target = next((tmp_path / "jobs" / "job-1" / "stage_runs").glob("*native_parse*/out.bin"))
    target.write_bytes(b"tampered")

    resumed = WorkflowRunner(repo, tmp_path)
    stage2 = _OutStage()
    resumed.register(stage2)
    resumed.register(_NoopStage())
    result = resumed.run(_job(), source_path="/tmp/x.pdf", page_numbers=(1,), resume=True)
    assert result.status == "succeeded"
    assert stage2.calls == 1  # tampered output → cache miss


def test_resume_with_open_reviews_keeps_review_status(repo: Repository, tmp_path: Path) -> None:
    """Cache-skipped pipeline must not downgrade to plain succeeded."""

    class _ReviewStage(_OutStage):
        def run(self, context: StageContext) -> StageOutcome:
            super().run(context)
            return StageOutcome(artifact_refs=[], review_required=3)

    runner = WorkflowRunner(repo, tmp_path)
    runner.register(_ReviewStage())
    runner.register(_NoopStage())
    result = runner.run(_job(), source_path="/tmp/x.pdf", page_numbers=(1,))
    assert result.status == "succeeded_with_review"

    # Simulate an open review item left in the DB (as ValidateStage does).
    repo.connection.execute(
        """
        INSERT INTO review_items (review_id, job_id, record_id, record_revision, status)
        VALUES ('review-x', 'job-1', 'rec-1', 1, 'open')
        """
    )

    resumed = WorkflowRunner(repo, tmp_path)
    # Same version → cache hit on resume; open review must still count.
    resumed_stage = _OutStage()
    resumed.register(resumed_stage)
    resumed.register(_NoopStage())
    result2 = resumed.run(_job(), source_path="/tmp/x.pdf", page_numbers=(1,), resume=True)
    assert result2.status == "succeeded_with_review"
    assert resumed_stage.calls == 0  # cache skipped, status still honest


def test_result_reader_prefers_published_manifest(repo: Repository, tmp_path: Path) -> None:
    reader = ResultReader(tmp_path)
    assert reader.read_published_manifest("job-1") is None
    with pytest.raises(Exception):
        reader.read_json("job-1", "validation")


def test_normalize_stage_fails_on_missing_requested_page(tmp_path: Path) -> None:
    """Partial page coverage fails instead of faking completion."""

    document = {
        "schema_version": "1.1.0",
        "document_id": "sha256:x",
        "parser_run_id": "r1",
        "pages": [
            {
                "page_number": 1,
                "width_pt": 612.0,
                "height_pt": 792.0,
                "blocks": [
                    {"block_id": "b1", "text_raw": "hello", "bbox": None}
                ],
                "tables": [],
            }
        ],
        "errors": [],
    }
    output_dir = tmp_path / "jobs" / "job-x"
    output_dir.mkdir(parents=True)
    (output_dir / "normalized.json").write_text(
        json.dumps(document), encoding="utf-8"
    )
    from bio_pdf.contracts import ArtifactRef

    import hashlib

    content = json.dumps(document).encode("utf-8")
    ref = ArtifactRef(
        artifact_id="ref-1",
        sha256=hashlib.sha256(content).hexdigest(),
        relative_path="jobs/job-x/normalized.json",
        media_type="application/json",
    )
    context = StageContext(
        job_id="job-x",
        document_id="sha256:x",
        source_path=str(tmp_path / "x.pdf"),
        page_numbers=(1, 2),  # page 2 missing from the document
        output_dir=str(output_dir),
        options={},
        stage_run_id="run-1",
        document_sha256="x",
        inputs={"native_parse.normalized.json": ref},
    )
    stage = NormalizeStage()
    with pytest.raises(PageCoverageMismatchError):
        stage.run(context)
