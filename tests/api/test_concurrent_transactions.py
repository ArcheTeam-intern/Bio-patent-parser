"""Concurrent transaction & lease fencing tests (plan T03 acceptance).

- two workers claiming → one success,
- heartbeat with stale generation reports lease lost,
- resume on a running leased job → JOB_RUNNING,
- re-derived review plans never reopen resolved items,
- concurrent review transactions on separate connections stay isolated.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from bio_pdf.contracts import Job
from bio_pdf.application.job_service import JobPaths, JobService
from bio_pdf.errors import JobRunningError, WorkflowError
from bio_pdf.review.plan import build_review_plan
from bio_pdf.storage.repository import Repository


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


def _job(job_id: str = "job-1"):
    now = datetime.now(tz=timezone.utc)
    return Job(
        job_id=job_id, document_id="sha256:doc",
        config_hash="cfg", created_at=now, updated_at=now,
    )


def test_two_workers_only_one_claims(repo) -> None:
    first = repo.claim_next_queued_job(worker_id="w1", lease_seconds=600)
    second = repo.claim_next_queued_job(worker_id="w2", lease_seconds=600)
    assert first is not None
    assert first.generation == 1
    assert second is None  # the running lease is valid — nothing left to claim


def test_expired_lease_reclaim_bumps_generation(repo) -> None:
    first = repo.claim_next_queued_job(worker_id="w1", lease_seconds=600)
    assert first is not None
    # Force the lease into the past (crashed worker).
    repo.connection.execute(
        "UPDATE worker_leases SET lease_until = '2000-01-01T00:00:00+00:00' WHERE job_id = ?",
        ("job-1",),
    )
    second = repo.claim_next_queued_job(worker_id="w2", lease_seconds=600)
    assert second is not None
    assert second.generation == 2  # monotonic — never reused


def test_heartbeat_with_stale_generation_reports_lost(repo) -> None:
    claimed = repo.claim_next_queued_job(worker_id="w1", lease_seconds=600)
    assert claimed is not None
    # Simulate reclaim by w2.
    repo.connection.execute(
        "UPDATE worker_leases SET worker_id='w2', generation=generation+1 WHERE job_id='job-1'"
    )
    ok = repo.heartbeat(
        "job-1", worker_id="w1", generation=claimed.generation, lease_seconds=600
    )
    assert ok is False  # fenced out
    ok2 = repo.heartbeat(
        "job-1", worker_id="w2", generation=claimed.generation + 1, lease_seconds=600
    )
    assert ok2 is True


def test_lease_is_valid_fencing(repo) -> None:
    claimed = repo.claim_next_queued_job(worker_id="w1", lease_seconds=600)
    assert claimed is not None
    assert repo.lease_is_valid("job-1", worker_id="w1", generation=1)
    assert not repo.lease_is_valid("job-1", worker_id="w1", generation=2)
    assert not repo.lease_is_valid("job-1", worker_id="w2", generation=1)


def test_release_keeps_generation_counter(repo) -> None:
    claimed = repo.claim_next_queued_job(worker_id="w1", lease_seconds=600)
    assert claimed is not None
    repo.release_lease("job-1", worker_id="w1", generation=claimed.generation)
    again = repo.claim_next_queued_job(worker_id="w2", lease_seconds=600)
    assert again is not None
    assert again.generation == 2  # counter survives deletion


def test_resume_on_running_leased_job_raises(tmp_path: Path) -> None:
    svc = JobService(JobPaths.under(tmp_path / "data"))
    try:
        claimed = svc.repository.claim_next_queued_job(worker_id="w", lease_seconds=600)
        assert claimed is None  # no jobs yet
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        pdf = tmp_path / "in.pdf"
        c = canvas.Canvas(str(pdf), pagesize=A4)
        c.drawString(50, 800, "hello")
        c.save()
        job, _ = svc.create_job(pdf)
        claimed = svc.repository.claim_next_queued_job(worker_id="w", lease_seconds=600)
        assert claimed is not None and claimed.job.job_id == job.job_id
        with pytest.raises(JobRunningError):
            svc.requeue_resume(job.job_id)
    finally:
        svc.close()


def test_review_plan_ids_stable_and_never_reopen_resolved(tmp_path: Path) -> None:
    r = Repository(tmp_path / "r.sqlite")
    from bio_pdf.contracts import Job

    now = datetime.now(tz=timezone.utc)
    r.upsert_document(
        document_id="sha256:doc", sha256="a" * 64,
        source_path="/tmp/x.pdf", bytes_count=1,
    )
    r.create_job(Job(
        job_id="job-1", document_id="sha256:doc",
        config_hash="cfg", created_at=now, updated_at=now,
    ))
    records = [
        {
            "record_id": "rec-1",
            "job_id": "job-1",
            "validation_state": "conflict",
            "reason_codes": ["X"],
        }
    ]
    items = build_review_plan(records, budget=0)
    assert len(items) == 1
    r.upsert_review_item(items[0])
    r.connection.execute(
        "UPDATE review_items SET status='resolved' WHERE review_id=?", (items[0].review_id,)
    )
    # Re-derive the same baseline plan: must NOT reopen the resolved item.
    items_again = build_review_plan(records, budget=0)
    assert items_again[0].review_id == items[0].review_id
    r.upsert_review_item(items_again[0])
    row = r.connection.execute(
        "SELECT status FROM review_items WHERE review_id=?", (items[0].review_id,)
    ).fetchone()
    assert row["status"] == "resolved"
    r.close()


def test_concurrent_review_transactions_isolated(tmp_path: Path) -> None:
    """Two connections, two decisions: one rolls back, the other commits."""

    svc = JobService(JobPaths.under(tmp_path / "data"))
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        pdf = tmp_path / "in.pdf"
        c = canvas.Canvas(str(pdf), pagesize=A4)
        t = c.beginText(50, 800)
        for line in [
            "<210> 1", "<211> 2", "<212> PRT", "<400> 1", "Ala Gly",
            "",
            "The IC50 was 1-10 nM.",
            "The measured MIC was >128 ug/mL against E. coli.",
        ]:
            t.textLine(line)
        c.drawText(t)
        c.save()
        job, _ = svc.create_job(pdf)

        from bio_pdf.workflow.queue_worker import run_worker

        run_worker(tmp_path / "data", once=True)
        items = svc.review_items(job.job_id)
        assert len(items) >= 2
        first, second = items[0], items[1]

        from bio_pdf.application.review_service import ReviewService

        class FailingReview(ReviewService):
            """Fails mid-transaction to exercise the rollback path."""

            def _write_version(self, *args, **kwargs):  # noqa: ANN002, ANN003
                raise RuntimeError("forced mid-transaction failure")

        def decide(item, *, fail: bool) -> str:
            # Each "request" opens its OWN service/connection (as the API now does).
            own = JobService(JobPaths.under(tmp_path / "data"))
            try:
                review_cls = FailingReview if fail else ReviewService
                review = review_cls(own.repository, own.paths.artifact_root)
                outcome = review.apply_decision(
                    review_id=item.review_id,
                    action="confirm",
                    base_revision=item.record_revision,
                    actor="parallel",
                )
                return outcome.record_id
            finally:
                own.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(decide, first, fail=True)
            f2 = pool.submit(decide, second, fail=False)
            with pytest.raises(RuntimeError):
                f1.result()
            ok_id = f2.result()

        # The failed decision left no trace; the successful one persisted.
        decisions_first = svc.repository.list_review_decisions(first.review_id)
        decisions_second = svc.repository.list_review_decisions(second.review_id)
        assert decisions_first == []
        assert len(decisions_second) == 1
        assert ok_id == second.record_id
    finally:
        svc.close()


def test_fenced_run_does_not_publish_or_write_status(tmp_path: Path) -> None:
    """A worker that loses its lease mid-run must not publish results."""

    from bio_pdf.workflow.runner import WorkflowRunner

    svc = JobService(JobPaths.under(tmp_path / "data"))
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        pdf = tmp_path / "in.pdf"
        c = canvas.Canvas(str(pdf), pagesize=A4)
        c.drawString(50, 800, "hello")
        c.save()
        job, _ = svc.create_job(pdf)

        def guard() -> bool:
            # Loses the lease on the second check (after ingest runs).
            calls["n"] += 1
            return calls["n"] < 2

        calls = {"n": 0}
        runner = svc._build_runner(None, lease_guard=guard)
        result = runner.run(
            job,
            source_path=str(pdf),
            page_numbers=(1,),
            options=svc.job_options(svc.repository.get_job(job.job_id)),
        )
        assert result.status == "fenced"
        # No published manifest, no succeeded status.
        published = svc.paths.artifact_root / "jobs" / job.job_id / "published" / "result-manifest.json"
        assert not published.is_file()
        reloaded = svc.repository.get_job(job.job_id)
        assert reloaded.status != "succeeded"
    finally:
        svc.close()
