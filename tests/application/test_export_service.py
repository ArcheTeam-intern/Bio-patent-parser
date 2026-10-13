"""Export service tests (design §14 acceptance)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bio_pdf.application.export_service import ExportService
from bio_pdf.application.job_service import JobPaths, JobService
from bio_pdf.errors import WorkflowError
from bio_pdf.workflow.queue_worker import run_worker


@pytest.fixture()
def job_service(tmp_path: Path):
    svc = JobService(JobPaths.under(tmp_path / "data"))
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    pdf = tmp_path / "in.pdf"
    c = canvas.Canvas(str(pdf), pagesize=A4)
    t = c.beginText(50, 800)
    for line in [
        "<210> 1",
        "<211> 4",
        "<212> PRT",
        "<213> Artificial",
        "<400> 1",
        "Ala Gly Leu Lys",
        "",
        "Example 1: The IC50 was 1-10 nM.",
    ]:
        t.textLine(line)
    c.drawText(t)
    c.save()

    job, _ = svc.create_job(pdf)
    run_worker(tmp_path / "data", once=True)
    yield svc, job.job_id
    svc.close()


def test_json_export_reflects_snapshot(job_service) -> None:
    svc, job_id = job_service
    export = ExportService(svc.repository, svc.paths.artifact_root)
    rel, count = export.build(job_id, "json")
    path = svc.paths.artifact_root / rel
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["result_revision"] == 0
    assert count >= 2
    assert data["counts"]["sequences"] >= 1


def test_csv_keeps_range_columns(job_service) -> None:
    svc, job_id = job_service
    export = ExportService(svc.repository, svc.paths.artifact_root)
    rel, _count = export.build(job_id, "csv")
    path = svc.paths.artifact_root / rel
    content = path.read_text(encoding="utf-8")
    # Range bounds survive as separate columns, never collapsed.
    assert '"1"' in content and '"10"' in content
    assert '"range"' in content


def test_fasta_excludes_unaccepted_sequences(job_service) -> None:
    svc, job_id = job_service
    export = ExportService(svc.repository, svc.paths.artifact_root)

    # Nothing accepted yet → empty FASTA must fail, not fake success.
    with pytest.raises(WorkflowError, match="refusing"):
        export.build(job_id, "fasta")

    # Confirm the sequence via the review service, then FASTA includes it.
    from bio_pdf.application.review_service import ReviewService

    items = svc.review_items(job_id)
    seq_item = next(i for i in items if ":seq:" in i.record_id)
    review = ReviewService(svc.repository, svc.paths.artifact_root)
    review.apply_decision(
        review_id=seq_item.review_id,
        action="confirm",
        base_revision=seq_item.record_revision,
        actor="tester",
    )
    rel, count = export.build(job_id, "fasta")
    content = (svc.paths.artifact_root / rel).read_text(encoding="utf-8")
    assert content.startswith(">1")
    assert "AGLK" in content
    assert count == 1


def test_export_requires_results(tmp_path: Path) -> None:
    svc = JobService(JobPaths.under(tmp_path / "data2"))
    export = ExportService(svc.repository, svc.paths.artifact_root)
    # A job id that exists but has no results is impossible here (no job);
    # snapshot must raise for the unknown job either way.
    with pytest.raises(WorkflowError):
        export.snapshot("job-missing")
    svc.close()
