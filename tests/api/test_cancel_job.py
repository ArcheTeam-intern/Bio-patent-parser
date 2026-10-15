"""POST /jobs/{id}/cancel and resume-after-cancel (fenced cancellation).

Cancel = one transaction that marks the job cancelled and deletes the
lease. The running worker's next stage gate / conditional commit then
refuses (fencing) — a cancelled parse can never publish or flip the
status back to running.
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
    for line in ["<210> 1", "<211> 4", "<212> PRT", "<400> 1", "Ala Gly Leu Lys"]:
        t.textLine(line)
    c.drawText(t)
    c.save()
    return path


@pytest.fixture()
def client(tmp_path: Path):
    settings = ApiSettings(
        data_dir=tmp_path / "data",
        samples_dir=tmp_path / "samples",
        max_upload_bytes=10 * 1024 * 1024,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c, tmp_path


def _queued_job(c: TestClient, tmp_path: Path) -> str:
    pdf = _sample_pdf(tmp_path / "paper.pdf")
    doc = c.post(
        "/api/v1/documents",
        files={"file": ("paper.pdf", pdf.read_bytes(), "application/pdf")},
    ).json()
    created = c.post(
        "/api/v1/jobs",
        json={"document_id": doc["document_id"], "parser": "native"},
    )
    assert created.status_code == 202
    return created.json()["job_id"]


def test_cancel_queued_job_and_refuse_double_cancel(client) -> None:
    c, tmp_path = client
    job_id = _queued_job(c, tmp_path)

    res = c.post(f"/api/v1/jobs/{job_id}/cancel")
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "cancelled"

    # Terminal state: a second cancel is a conflict, not a silent no-op.
    res = c.post(f"/api/v1/jobs/{job_id}/cancel")
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "JOB_NOT_CANCELLABLE"


def test_cancelled_job_can_resume(client) -> None:
    c, tmp_path = client
    job_id = _queued_job(c, tmp_path)
    assert c.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 200

    res = c.post(f"/api/v1/jobs/{job_id}/resume")
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["status"] == "queued"
    assert body["execution_mode"] == "resume"


def test_cancel_refuses_terminal_jobs(client) -> None:
    c, tmp_path = client
    job_id = _queued_job(c, tmp_path)

    # Drain the queue synchronously to a terminal state.
    from bio_pdf.workflow.queue_worker import run_worker

    run_worker(tmp_path / "data", once=True)
    job = c.get(f"/api/v1/jobs/{job_id}").json()
    assert job["status"] in {"succeeded", "succeeded_with_review"}

    res = c.post(f"/api/v1/jobs/{job_id}/cancel")
    assert res.status_code == 409
    assert res.json()["error"]["details"]["status"] == job["status"]
