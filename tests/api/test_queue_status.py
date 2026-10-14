"""GET /api/v1/queue/status (live-ops).

The endpoint exists so the UI can explain a job stuck at 等待解析: a
queued job with no worker polling is not going to start on its own.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bio_pdf.api.app import create_app
from bio_pdf.api.settings import ApiSettings
from bio_pdf.application.job_service import JobPaths
from bio_pdf.storage.repository import Repository
from bio_pdf.workflow.queue_worker import PRESENCE_STALE_AFTER


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


def _repo(tmp_path: Path) -> Repository:
    return Repository(JobPaths.under(tmp_path / "data").database)


def test_queue_status_reports_offline_with_queued_job(client) -> None:
    c, tmp_path = client
    pdf = _sample_pdf(tmp_path / "paper.pdf")

    upload = c.post(
        "/api/v1/documents",
        files={"file": ("paper.pdf", pdf.read_bytes(), "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    created = c.post(
        "/api/v1/jobs",
        json={"document_id": upload.json()["document_id"], "parser": "native"},
    )
    assert created.status_code == 202, created.text

    res = c.get("/api/v1/queue/status")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["api_version"] == "v1"
    assert body["queued_jobs"] == 1
    assert body["online_worker_count"] == 0
    assert body["workers"] == []
    assert body["stale_after_seconds"] == PRESENCE_STALE_AFTER
    assert Path(body["data_dir"]) == (tmp_path / "data").resolve()


def test_queue_status_online_for_fresh_heartbeat_then_stale(client) -> None:
    c, tmp_path = client
    repo = _repo(tmp_path)
    try:
        repo.record_worker_presence("worker-test", pid=99, data_dir=str(tmp_path))
    finally:
        repo.close()

    body = c.get("/api/v1/queue/status").json()
    assert body["online_worker_count"] == 1
    assert body["workers"][0]["worker_id"] == "worker-test"
    assert body["workers"][0]["online"] is True

    # Age the heartbeat past the staleness threshold: the worker is now
    # reported offline even though the row still exists (crashed worker).
    repo = _repo(tmp_path)
    try:
        stale = (
            datetime.now(tz=timezone.utc)
            - timedelta(seconds=PRESENCE_STALE_AFTER + 60)
        ).isoformat()
        repo.connection.execute(
            "UPDATE worker_presence SET last_seen_at = ? WHERE worker_id = ?",
            (stale, "worker-test"),
        )
    finally:
        repo.close()

    body = c.get("/api/v1/queue/status").json()
    assert body["online_worker_count"] == 0
    assert body["workers"][0]["online"] is False
    assert body["workers"][0]["seconds_since_heartbeat"] > PRESENCE_STALE_AFTER
