"""B08: fixed-snapshot exports and honest source Range handling.

Counterexamples from the review:

- export row recorded revision r1 but the background re-read the CURRENT
  records — a review landing in between mixed revisions;
- ``bytes=-100`` computed ``end=100`` (a 100-byte window at the START)
  instead of the last 100 bytes;
- ``bytes=1000-`` on a 1000-byte file must be 416;
- a failed inspection on GET removed an existing source file.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bio_pdf.api.app import create_app
from bio_pdf.api.settings import ApiSettings


def _client(tmp_path: Path):
    settings = ApiSettings(
        data_dir=tmp_path / "data",
        samples_dir=tmp_path / "samples",
        max_upload_bytes=10 * 1024 * 1024,
    )
    return TestClient(create_app(settings))


def _pdf_with_range(tmp_path: Path) -> Path:
    """A real one-page PDF (valid signature + parseable)."""

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    pdf = tmp_path / "ranged.pdf"
    c = canvas.Canvas(str(pdf), pagesize=A4)
    t = c.beginText(50, 800)
    for line in ["<210> 1", "<211> 4", "<212> PRT", "<400> 1", "Ala Gly Leu Lys"]:
        t.textLine(line)
    c.drawText(t)
    c.save()
    return pdf


@pytest.fixture()
def client(tmp_path: Path):
    with _client(tmp_path) as c:
        yield c, tmp_path


def _make_job(client, tmp_path: Path) -> str:
    pdf = _pdf_with_range(tmp_path)
    doc = client.post(
        "/api/v1/documents",
        files={"file": ("ranged.pdf", pdf.read_bytes(), "application/pdf")},
    ).json()
    job = client.post(
        "/api/v1/jobs", json={"document_id": doc["document_id"], "parser": "native"}
    ).json()
    from bio_pdf.workflow.queue_worker import run_worker

    run_worker(tmp_path / "data", once=True)
    return job["job_id"]


# ---- Range semantics ------------------------------------------------------

def test_suffix_range_returns_the_last_bytes(client) -> None:
    c, tmp_path = client
    pdf = _pdf_with_range(tmp_path)
    doc = c.post(
        "/api/v1/documents",
        files={"file": ("ranged.pdf", pdf.read_bytes(), "application/pdf")},
    ).json()
    total = len(pdf.read_bytes())
    res = c.get(
        f"/api/v1/documents/{doc['document_id']}/source", headers={"Range": "bytes=-100"}
    )
    assert res.status_code == 206
    assert len(res.content) == 100
    # The LAST 100 bytes (previously: the first 100).
    assert res.content == pdf.read_bytes()[total - 100 : total]
    assert res.headers["content-range"] == f"bytes {total - 100}-{total - 1}/{total}"


def test_open_ended_range_and_out_of_bounds(client) -> None:
    c, tmp_path = client
    pdf = _pdf_with_range(tmp_path)
    data = pdf.read_bytes()
    total = len(data)
    doc = c.post(
        "/api/v1/documents",
        files={"file": ("ranged.pdf", data, "application/pdf")},
    ).json()

    res = c.get(
        f"/api/v1/documents/{doc['document_id']}/source",
        headers={"Range": f"bytes={total - 50}-"},
    )
    assert res.status_code == 206
    assert len(res.content) == 50

    res = c.get(
        f"/api/v1/documents/{doc['document_id']}/source",
        headers={"Range": f"bytes={total}-"},
    )
    assert res.status_code == 416

    res = c.get(
        f"/api/v1/documents/{doc['document_id']}/source", headers={"Range": "bytes=100-199"}
    )
    assert res.status_code == 206
    assert len(res.content) == 100


def test_failed_inspection_does_not_delete_existing_source(client) -> None:
    """A GET whose inspect fails must not unlink the registered source."""

    c, tmp_path = client
    pdf = _pdf_with_range(tmp_path)
    doc = c.post(
        "/api/v1/documents",
        files={"file": ("ranged.pdf", pdf.read_bytes(), "application/pdf")},
    ).json()
    row = c.app.state  # noqa: F841 - just ensure app state exists
    # Corrupt the source on disk (simulating damage after registration).
    import sqlite3

    conn = sqlite3.connect(tmp_path / "data" / "bio.sqlite")
    source_path = Path(
        conn.execute(
            "SELECT source_path FROM documents WHERE document_id = ?",
            (doc["document_id"],),
        ).fetchone()[0]
    )
    conn.close()
    source_path.write_bytes(b"not a pdf anymore")

    res = c.get(f"/api/v1/documents/{doc['document_id']}")
    # Either a structured error or a successful re-inspect; either way the
    # file must still exist afterwards.
    assert res.status_code in (409, 422, 200, 415) or res.status_code >= 400
    assert source_path.exists(), "GET deleted the registered source file"


# ---- fixed-snapshot export race -------------------------------------------

def test_export_snapshot_is_fixed_against_concurrent_review(client) -> None:
    """POST fixes r1; a review lands (r2); the export stays r1."""

    c, tmp_path = client
    job_id = _make_job(c, tmp_path)

    reviews = c.get(f"/api/v1/reviews?job_id={job_id}").json()["reviews"]
    assert reviews, "fixture needs at least one review item"

    # 1) POST the export (captures the snapshot synchronously).
    res = c.post(f"/api/v1/jobs/{job_id}/exports", json={"format": "json"})
    assert res.status_code == 202, res.text
    export_id = res.json()["export_id"]

    # 2) A review decision lands BEFORE the background build runs.
    item = reviews[0]
    decision = c.post(
        f"/api/v1/reviews/{item['review_id']}/decisions",
        json={"action": "confirm", "base_revision": item["record_revision"], "actor": "race"},
    )
    assert decision.status_code == 200, decision.text

    # 3) Poll the export; it must complete against the r1 snapshot.
    import time as _time

    status = None
    for _ in range(100):
        payload = c.get(f"/api/v1/exports/{export_id}").json()
        status = payload["status"]
        if status in ("completed", "failed"):
            break
        _time.sleep(0.05)
    assert status == "completed", payload

    dl = c.get(f"/api/v1/exports/{export_id}/download")
    body = dl.json()
    assert body["result_revision"] == 0  # r1 snapshot (pre-review revision)

    # The CURRENT records reflect the review (r2) — export did not drift.
    records = c.get(f"/api/v1/jobs/{job_id}/records").json()
    assert any(
        r.get("acceptance_state") == "accepted"
        for r in records["sequences"] + records["measurements"]
    )
    assert records["revision"] == 1


def test_same_revision_two_filters_do_not_collide(client) -> None:
    c, tmp_path = client
    job_id = _make_job(c, tmp_path)

    ids = []
    for accepted_only in (True, False):
        res = c.post(
            f"/api/v1/jobs/{job_id}/exports",
            json={"format": "json", "accepted_only": accepted_only},
        )
        ids.append(res.json()["export_id"])
    assert ids[0] != ids[1]

    import time as _time

    for export_id in ids:
        for _ in range(100):
            payload = c.get(f"/api/v1/exports/{export_id}").json()
            if payload["status"] in ("completed", "failed"):
                break
            _time.sleep(0.05)
        assert payload["status"] == "completed", payload
        dl = c.get(f"/api/v1/exports/{export_id}/download")
        assert dl.status_code == 200

    files = list((tmp_path / "data" / "jobs" / job_id / "exports").rglob("*.json"))
    paths = {str(p) for p in files}
    assert len(paths) == len(files), "export files must live under export_id dirs"


def test_snapshot_content_is_frozen_across_a_real_review(tmp_path: Path) -> None:
    """Service-level race with a barrier: capture → review(r2) → build.

    This is the deterministic core of the race: the build consumes ONLY
    the captured snapshot, so the review that lands in between cannot
    change the export contents, while read_current reflects r2.
    """

    from bio_pdf.application.job_service import JobPaths, JobService
    from bio_pdf.application.result_snapshot import SnapshotService
    from bio_pdf.application.review_service import ReviewService

    svc = JobService(JobPaths.under(tmp_path / "race"))
    svc.job_repo_path = tmp_path / "race" / "bio.sqlite"  # type: ignore[attr-defined]
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        pdf = tmp_path / "in.pdf"
        c = canvas.Canvas(str(pdf), pagesize=A4)
        t = c.beginText(50, 800)
        for line in [
            "<210> 1", "<211> 4", "<212> PRT", "<400> 1", "Ala Gly Leu Lys",
        ]:
            t.textLine(line)
        c.drawText(t)
        c.save()
        job, _ = svc.create_job(pdf)
        from bio_pdf.workflow.queue_worker import run_worker

        run_worker(tmp_path / "race", once=True)

        # 1) capture the snapshot (barrier point A).
        snapshots = SnapshotService(svc.repository, svc.paths.artifact_root)
        snap = snapshots.capture(job.job_id, export_id="exp-race")

        # 2) a review lands (r2).
        items = svc.review_items(job.job_id)
        assert items
        review = ReviewService(svc.repository, svc.paths.artifact_root)
        review.apply_decision(
            review_id=items[0].review_id,
            action="confirm",
            base_revision=items[0].record_revision,
            actor="race",
        )

        # 3) the build consumes the FIXED snapshot → still r1, unaccepted.
        from bio_pdf.application.export_service import ExportService

        exports = ExportService(svc.repository, svc.paths.artifact_root)
        rel, _count = exports.build_from_snapshot(
            job_id=job.job_id,
            export_id="exp-race",
            snapshot=snap.payload,
            fmt="json",
            accepted_only=False,
        )
        import json as _json

        body = _json.loads((svc.paths.artifact_root / rel).read_text(encoding="utf-8"))
        assert body["result_revision"] == 0
        assert all(
            r.get("acceptance_state") != "accepted"
            for r in body["sequences"] + body["measurements"]
        )
        assert body["source_counts"]["sequences"] >= 1

        # 4) the CURRENT records reflect r2.
        current = snapshots.read_current(job.job_id)
        assert current["result_revision"] == 1
        assert any(
            r.get("acceptance_state") == "accepted"
            for r in current["sequences"] + current["measurements"]
        )
    finally:
        svc.close()
