"""Workflow runner resume tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from bio_pdf.contracts import Job, StageOutcome
from bio_pdf.errors import WorkflowError
from bio_pdf.storage.repository import Repository
from bio_pdf.workflow.runner import WorkflowRunner
from bio_pdf.workflow.stages import StageContext


class _NativeParseHandler:
    name = "native_parse"

    def __init__(self) -> None:
        self.calls = 0

    def cache_key(self, context: StageContext) -> str:
        return f"native:{context.document_id}"

    def run(self, context: StageContext) -> StageOutcome:
        self.calls += 1
        return StageOutcome(artifact_refs=[], review_required=0, warnings=[])


class _LayoutParseHandler:
    name = "layout_parse"

    def __init__(self, *, fail: bool) -> None:
        self.fail = fail
        self.calls = 0

    def cache_key(self, context: StageContext) -> str:
        return f"layout:{context.document_id}"

    def run(self, context: StageContext) -> StageOutcome:
        self.calls += 1
        if self.fail:
            raise WorkflowError("layout failed", details={"code": "ENGINE_TIMEOUT"})
        return StageOutcome(artifact_refs=[], review_required=0, warnings=[])


class _ExportHandler:
    name = "export"

    def __init__(self) -> None:
        self.calls = 0

    def cache_key(self, context: StageContext) -> str:
        return f"export:{context.document_id}"

    def run(self, context: StageContext) -> StageOutcome:
        self.calls += 1
        return StageOutcome(artifact_refs=[], review_required=0, warnings=[])


@pytest.fixture()
def repo(tmp_path: Path) -> Repository:
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


def test_resume_skips_succeeded_native_and_retries_layout(
    repo: Repository, tmp_path: Path
) -> None:
    native = _NativeParseHandler()
    layout = _LayoutParseHandler(fail=True)
    export = _ExportHandler()

    runner = WorkflowRunner(repo, tmp_path)
    runner.register(native)
    runner.register(layout)
    runner.register(export)

    # First run: native succeeds, layout fails, export never runs.
    result = runner.run(_job(), source_path="/tmp/x.pdf", page_numbers=(1,))
    assert result.status == "failed"
    assert result.failed_stage == "layout_parse"
    assert native.calls == 1
    assert layout.calls == 2  # max_attempts=2
    assert export.calls == 0

    # "Fix" layout so it now succeeds.
    layout2 = _LayoutParseHandler(fail=False)
    runner2 = WorkflowRunner(repo, tmp_path)
    runner2.register(native)
    runner2.register(layout2)
    runner2.register(export)

    # Resume: native must NOT re-run (already succeeded, cache key matches).
    result2 = runner2.run(
        _job(), source_path="/tmp/x.pdf", page_numbers=(1,), resume=True
    )
    assert result2.status == "succeeded"
    assert native.calls == 1  # unchanged — native was skipped on resume
    assert layout2.calls == 1  # layout re-attempted exactly once
    assert export.calls == 1  # export ran to completion


def test_non_resume_run_reruns_everything(repo: Repository, tmp_path: Path) -> None:
    native = _NativeParseHandler()
    layout = _LayoutParseHandler(fail=False)
    export = _ExportHandler()

    runner = WorkflowRunner(repo, tmp_path)
    runner.register(native)
    runner.register(layout)
    runner.register(export)
    result = runner.run(_job(), source_path="/tmp/x.pdf", page_numbers=(1,))
    assert result.status == "succeeded"
    assert native.calls == 1

    # Without resume, a fresh runner re-runs stages.
    runner2 = WorkflowRunner(repo, tmp_path)
    runner2.register(native)
    runner2.register(layout)
    runner2.register(export)
    result2 = runner2.run(_job(), source_path="/tmp/x.pdf", page_numbers=(1,))
    assert result2.status == "succeeded"
    assert native.calls == 2  # re-ran


def test_failure_marks_job_failed_in_repo(repo: Repository, tmp_path: Path) -> None:
    native = _NativeParseHandler()
    layout = _LayoutParseHandler(fail=True)

    runner = WorkflowRunner(repo, tmp_path)
    runner.register(native)
    runner.register(layout)
    runner.run(_job(), source_path="/tmp/x.pdf", page_numbers=(1,))

    job = repo.get_job("job-1")
    assert job is not None
    assert job.status == "failed"
    assert job.current_stage == "layout_parse"