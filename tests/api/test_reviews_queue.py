"""Review queue listing: true totals, pagination, and per-record lookup.

The queue can hold hundreds of items (every cell of a reconstructed
susceptibility matrix is one). A truncated first page must never present
itself as the whole queue, and the workbench panel must find the review
item of ANY record — not only records that happen to be on page one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bio_pdf.api.app import create_app
from bio_pdf.api.settings import ApiSettings


def _sample_pdf(path: Path) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    t = c.beginText(50, 800)
    for line in [
        "<210> 1",
        "<211> 4",
        "<212> PRT",
        "<400> 1",
        "Ala Gly Leu Lys",
        "",
        "Example 1: The IC50 was 1-10 nM.",
    ]:
        t.textLine(line)
    c.drawText(t)
    c.save()
    return path


@pytest.fixture()
def completed_job(tmp_path: Path):
    settings = ApiSettings(
        data_dir=tmp_path / "data",
        samples_dir=tmp_path / "samples",
        max_upload_bytes=10 * 1024 * 1024,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        pdf = _sample_pdf(tmp_path / "paper.pdf")
        doc = c.post(
            "/api/v1/documents",
            files={"file": ("paper.pdf", pdf.read_bytes(), "application/pdf")},
        ).json()
        job = c.post(
            "/api/v1/jobs",
            json={"document_id": doc["document_id"], "parser": "native"},
        ).json()
        from bio_pdf.workflow.queue_worker import run_worker

        run_worker(tmp_path / "data", once=True)
        yield c, job["job_id"]


def test_total_is_the_true_count_not_the_page_size(completed_job) -> None:
    c, job_id = completed_job
    full = c.get(f"/api/v1/reviews?job_id={job_id}").json()
    assert full["total"] >= 2, full
    assert full["total"] == len(full["reviews"])

    # A page of one still reports the whole queue's size.
    page = c.get(f"/api/v1/reviews?job_id={job_id}&limit=1").json()
    assert page["total"] == full["total"]
    assert len(page["reviews"]) == 1

    # Offsets page through without repeats.
    second = c.get(f"/api/v1/reviews?job_id={job_id}&limit=1&offset=1").json()
    assert second["reviews"][0]["review_id"] != page["reviews"][0]["review_id"]


def test_record_id_filter_finds_the_review_item_directly(completed_job) -> None:
    c, job_id = completed_job
    full = c.get(f"/api/v1/reviews?job_id={job_id}").json()
    # Pick a record that is NOT the first entry of the queue.
    target = full["reviews"][-1]["record_id"]

    res = c.get(f"/api/v1/reviews?job_id={job_id}&record_id={target}").json()
    assert res["total"] == 1
    assert len(res["reviews"]) == 1
    assert res["reviews"][0]["record_id"] == target

    # Unknown record: honest empty result, not an error.
    empty = c.get(f"/api/v1/reviews?job_id={job_id}&record_id=no-such-record").json()
    assert empty["total"] == 0
    assert empty["reviews"] == []
