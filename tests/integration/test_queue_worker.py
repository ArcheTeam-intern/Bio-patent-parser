"""Persistent queue worker tests: claim atomicity and lease recovery."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from bio_pdf.application.job_service import JobPaths, JobService
from bio_pdf.workflow.queue_worker import run_worker


@pytest.fixture()
def service(tmp_path: Path):
    svc = JobService(JobPaths.under(tmp_path / "data"))
    yield svc, tmp_path
    svc.close()


def _make_pdf(tmp_path: Path) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    pdf = tmp_path / "in.pdf"
    c = canvas.Canvas(str(pdf), pagesize=A4)
    t = c.beginText(50, 800)
    for line in ["<210> 1", "<211> 2", "<212> PRT", "<400> 1", "Ala Gly"]:
        t.textLine(line)
    c.drawText(t)
    c.save()
    return pdf


def test_worker_runs_queued_job(service) -> None:
    svc, tmp_path = service
    pdf = _make_pdf(tmp_path)
    job, _ = svc.create_job(pdf)
    assert job.status == "queued"

    run_worker(tmp_path / "data", once=True)

    done = svc.get_job(job.job_id)
    assert done is not None
    assert done.status in {"succeeded", "succeeded_with_review"}


def test_expired_lease_is_reclaimable(service) -> None:
    svc, tmp_path = service
    pdf = _make_pdf(tmp_path)
    job, _ = svc.create_job(pdf)

    # Simulate a crashed worker: claim, then force the lease into the past.
    claimed = svc.repository.claim_next_queued_job(worker_id="crashed", lease_seconds=900)
    assert claimed is not None and claimed.job_id == job.job_id
    svc.repository.connection.execute(
        "UPDATE worker_leases SET lease_until = ? WHERE job_id = ?",
        ("2000-01-01T00:00:00+00:00", job.job_id),
    )

    # A new worker claims the expired-lease job and completes it.
    run_worker(tmp_path / "data", once=True)
    done = svc.get_job(job.job_id)
    assert done is not None
    assert done.status in {"succeeded", "succeeded_with_review"}


def test_fresh_claim_beats_expired_lease_ordering(service) -> None:
    svc, tmp_path = service
    pdf = _make_pdf(tmp_path)
    first, _ = svc.create_job(pdf)
    second, _ = svc.create_job(_make_pdf(tmp_path))

    claimed = svc.repository.claim_next_queued_job(worker_id="w1", lease_seconds=900)
    assert claimed is not None and claimed.job_id == first.job_id
    # An active lease is not stealable.
    assert svc.repository.claim_next_queued_job(worker_id="w2", lease_seconds=900).job_id == second.job_id
