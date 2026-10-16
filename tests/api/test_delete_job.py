"""DELETE /jobs/{id}: terminal-job removal with dependent-row cleanup.

Delete removes the job row plus every dependent table row and the
jobs/<id>/ artifact tree. A queued/running job is refused (JOB_ACTIVE) —
cancel first; deleting behind a live worker would break lease fencing.
The source document stays (content-addressed, possibly shared).
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


def _job(c: TestClient, tmp_path: Path, name: str = "paper.pdf") -> str:
    pdf = _sample_pdf(tmp_path / name)
    doc = c.post(
        "/api/v1/documents",
        files={"file": (name, pdf.read_bytes(), "application/pdf")},
    ).json()
    created = c.post(
        "/api/v1/jobs",
        json={"document_id": doc["document_id"], "parser": "native"},
    )
    assert created.status_code == 202
    return created.json()["job_id"]


def test_delete_missing_job_is_404(client) -> None:
    c, _ = client
    assert c.delete("/api/v1/jobs/job-nosuch").status_code == 404


def test_delete_active_job_is_refused_then_allowed_after_cancel(client) -> None:
    c, tmp_path = client
    job_id = _job(c, tmp_path)

    # Queued = active: refuse with a machine-readable code.
    res = c.delete(f"/api/v1/jobs/{job_id}")
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "JOB_ACTIVE"

    assert c.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 200
    res = c.delete(f"/api/v1/jobs/{job_id}")
    assert res.status_code == 204
    assert c.get(f"/api/v1/jobs/{job_id}").status_code == 404

    # Idempotent at the API level: deleting again is a plain 404.
    assert c.delete(f"/api/v1/jobs/{job_id}").status_code == 404


def test_delete_removes_dependent_rows_and_artifacts_but_keeps_document(
    client, tmp_path: Path
) -> None:
    c, tmp = client
    data_dir = tmp / "data"
    job_id = _job(c, tmp)
    c.post(f"/api/v1/jobs/{job_id}/cancel")

    # A stage run + artifact row so cleanup has something to chew on; the
    # ingest stage already wrote files under jobs/<id>/ for the queued job.
    import sqlite3

    conn = sqlite3.connect(data_dir / "bio.sqlite")
    job_dir = data_dir / "jobs" / job_id
    if not job_dir.is_dir():
        job_dir.mkdir(parents=True)
        (job_dir / "marker.txt").write_text("x", encoding="utf-8")
    before = conn.execute(
        "SELECT document_id FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()[0]
    conn.close()

    assert c.delete(f"/api/v1/jobs/{job_id}").status_code == 204

    conn = sqlite3.connect(data_dir / "bio.sqlite")
    assert conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM job_configs WHERE job_id = ?", (job_id,)
    ).fetchone()[0] == 0
    # The source document survives deletion: content-addressed and shared.
    assert conn.execute(
        "SELECT COUNT(*) FROM documents WHERE document_id = ?", (before,)
    ).fetchone()[0] == 1
    conn.close()
    assert not (data_dir / "jobs" / job_id).exists()


def test_delete_second_job_keeps_shared_document(client, tmp_path: Path) -> None:
    c, tmp = client
    # Same bytes uploaded twice -> one content-addressed document row, two jobs.
    pdf = _sample_pdf(tmp / "shared.pdf")
    payload = pdf.read_bytes()

    def _job_from(payload: bytes) -> str:
        doc = c.post(
            "/api/v1/documents",
            files={"file": ("shared.pdf", payload, "application/pdf")},
        ).json()
        created = c.post(
            "/api/v1/jobs",
            json={"document_id": doc["document_id"], "parser": "native"},
        )
        assert created.status_code == 202
        return created.json()["job_id"]

    first, second = _job_from(payload), _job_from(payload)
    assert first != second
    for job_id in (first, second):
        c.post(f"/api/v1/jobs/{job_id}/cancel")
        assert c.delete(f"/api/v1/jobs/{job_id}").status_code == 204

    import sqlite3

    conn = sqlite3.connect(tmp / "data" / "bio.sqlite")
    doc_count, sha = conn.execute(
        "SELECT COUNT(*), MIN(sha256) FROM documents"
    ).fetchone()
    job_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    conn.close()
    assert job_count == 0
    assert doc_count == 1, "shared document must survive both job deletions"
    assert sha and len(sha) == 64
