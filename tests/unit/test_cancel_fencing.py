"""Cancellation fencing at the repository level.

``cancel_job`` deletes the lease row in the same transaction that marks
the job cancelled. Every fenced write the stale worker attempts afterwards
(``set_current_stage_fenced``, ``complete_stage_fenced``) must refuse, so
a cancelled job can never be resurrected by the worker that was told to
stop.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from bio_pdf.contracts import Job, Stage
from bio_pdf.storage.repository import Repository


def _mk_job(repo: Repository, job_id: str = "job-c1") -> None:
    repo.upsert_document(
        document_id="sha256:" + "a" * 64,
        sha256="a" * 64,
        source_path=str(Path("x.pdf")),
        bytes_count=1,
    )
    repo.create_job(
        Job(
            job_id=job_id,
            document_id="sha256:" + "a" * 64,
            config_hash="cfg",
        )
    )


def test_cancel_deletes_lease_and_fences_worker_writes(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "bio.sqlite")
    try:
        _mk_job(repo)
        claimed = repo.claim_next_queued_job(worker_id="worker-1", lease_seconds=900)
        assert claimed is not None and claimed.job.job_id == "job-c1"
        from bio_pdf.storage.repository import LeaseToken

        lease_token = LeaseToken.from_claim(claimed)

        # The user cancels mid-run.
        assert repo.cancel_job("job-c1") == "cancelled"
        assert repo.get_job("job-c1").status == "cancelled"

        # The stale worker tries to report progress and complete its stage.
        assert (
            repo.set_current_stage_fenced("job-c1", "extract", token=lease_token)
            is False
        )
        assert repo.get_job("job-c1").status == "cancelled"

        stage = Stage(
            stage_run_id="sr-1",
            job_id="job-c1",
            stage_name="extract",
            attempt=1,
            status="running",
        )
        repo.start_stage(stage)
        assert (
            repo.complete_stage_fenced("sr-1", token=lease_token, artifact_manifest_ref=None)
            is False
        )
        row = repo.connection.execute(
            "SELECT status FROM stage_runs WHERE stage_run_id = 'sr-1'"
        ).fetchone()
        assert row["status"] == "running"  # nothing was written

        # Heartbeat also reports the lost lease.
        assert (
            repo.heartbeat(
                "job-c1",
                worker_id="worker-1",
                generation=claimed.generation,
            )
            is False
        )
    finally:
        repo.close()


def test_progress_write_marks_running_stage(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "bio.sqlite")
    try:
        _mk_job(repo)
        claimed = repo.claim_next_queued_job(worker_id="worker-1", lease_seconds=900)
        from bio_pdf.storage.repository import LeaseToken

        lease_token = LeaseToken.from_claim(claimed)
        assert (
            repo.set_current_stage_fenced("job-c1", "layout_parse", token=lease_token)
            is True
        )
        job = repo.get_job("job-c1")
        assert job.status == "running"
        assert job.current_stage == "layout_parse"
    finally:
        repo.close()


def test_cancel_terminal_job_returns_none(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "bio.sqlite")
    try:
        _mk_job(repo)
        repo.update_job_status("job-c1", "failed")
        assert repo.cancel_job("job-c1") is None
        assert repo.get_job("job-c1").status == "failed"
    finally:
        repo.close()
