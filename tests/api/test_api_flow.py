"""API integration tests: upload → job → worker → results → reviews.

Runs against a real temp data dir and the in-process worker path (no HTTP
server) using FastAPI's TestClient.
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
        "<213> Artificial",
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
def client(tmp_path: Path):
    settings = ApiSettings(
        data_dir=tmp_path / "data",
        samples_dir=tmp_path / "samples",
        max_upload_bytes=10 * 1024 * 1024,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c, tmp_path


def test_capabilities(client) -> None:
    c, _ = client
    res = c.get("/api/v1/capabilities")
    assert res.status_code == 200
    body = res.json()
    assert body["api_version"] == "v1"
    names = {e["name"] for e in body["engines"]}
    assert "native" in names and "docling_sdk" in names


def test_upload_and_create_job_then_worker_completes(client) -> None:
    c, tmp_path = client
    pdf = _sample_pdf(tmp_path / "paper.pdf")

    res = c.post(
        "/api/v1/documents",
        files={"file": ("paper.pdf", pdf.read_bytes(), "application/pdf")},
    )
    assert res.status_code == 201, res.text
    doc = res.json()
    assert doc["document_id"].startswith("sha256:")
    assert doc["page_count"] == 1

    res = c.post(
        "/api/v1/jobs",
        json={"document_id": doc["document_id"], "parser": "native"},
    )
    assert res.status_code == 202, res.text
    job = res.json()
    job_id = job["job_id"]
    assert job["status"] == "queued"

    # Run the worker synchronously until the queue drains.
    from bio_pdf.application.job_service import JobPaths, JobService
    from bio_pdf.workflow.queue_worker import run_worker

    run_worker(tmp_path / "data", once=True)

    res = c.get(f"/api/v1/jobs/{job_id}")
    assert res.status_code == 200
    job = res.json()
    assert job["status"] in {"succeeded", "succeeded_with_review"}, job
    stage_names = {s["stage_name"] for s in job["stages"] if s["status"] == "succeeded"}
    assert {"ingest", "native_parse", "normalize", "extract", "validate", "export"} <= stage_names

    # Records reflect the domain results.
    res = c.get(f"/api/v1/jobs/{job_id}/records")
    assert res.status_code == 200
    records = res.json()
    assert len(records["sequences"]) >= 1
    assert any(
        m["value"]["kind"] == "range" and m["value"]["lower"] == "1"
        for m in records["measurements"]
    ), records["measurements"]

    # Page projection serves normalized blocks.
    res = c.get(f"/api/v1/jobs/{job_id}/pages/1")
    assert res.status_code == 200
    assert res.json()["page_number"] == 1

    # Artifacts list includes the export and its content is downloadable.
    res = c.get(f"/api/v1/jobs/{job_id}/artifacts")
    rels = {a["relative_path"] for a in res.json()["artifacts"]}
    assert any(rel.endswith("--export--a1--") and rel.endswith("/result.json") is False or "/result.json" in rel for rel in rels)
    assert any("result.json" in rel for rel in rels)


def test_upload_rejects_html_disguised_as_pdf(client) -> None:
    c, tmp_path = client
    fake = tmp_path / "fake.pdf"
    fake.write_text("<!DOCTYPE html><html></html>", encoding="utf-8")
    res = c.post(
        "/api/v1/documents",
        files={"file": ("fake.pdf", fake.read_bytes(), "application/pdf")},
    )
    assert res.status_code == 415
    error = res.json()["error"]
    assert error["code"] == "INPUT_NOT_PDF"
    assert error["details"]["detected_type"] == "html"
    assert error["details"]["file_name"] == "fake.pdf"


def test_upload_rejects_empty_file_with_specific_reason(client) -> None:
    c, tmp_path = client
    res = c.post(
        "/api/v1/documents",
        files={"file": ("empty.pdf", b"", "application/pdf")},
    )
    assert res.status_code == 415
    error = res.json()["error"]
    assert error["code"] == "INPUT_NOT_PDF"
    assert error["details"]["detected_type"] == "empty"
    assert "为空" in error["message"]


def test_reupload_of_registered_pdf_keeps_source(client) -> None:
    c, tmp_path = client
    pdf = _sample_pdf(tmp_path / "paper.pdf")
    body = pdf.read_bytes()

    first = c.post("/api/v1/documents", files={"file": ("paper.pdf", body, "application/pdf")})
    assert first.status_code == 201, first.text
    document_id = first.json()["document_id"]

    # Same bytes again: content-addressed dedup must not delete the already
    # registered source file, and the response stays a success.
    second = c.post("/api/v1/documents", files={"file": ("paper.pdf", body, "application/pdf")})
    assert second.status_code == 201, second.text
    assert second.json()["document_id"] == document_id

    sha = document_id.removeprefix("sha256:")
    source = tmp_path / "data" / "sources" / f"{sha}.pdf"
    assert source.is_file()
    assert source.read_bytes() == body


def test_records_record_pages_consistent_with_kind_and_query(client) -> None:
    c, tmp_path = client
    from bio_pdf.workflow.queue_worker import run_worker

    pdf = _sample_pdf(tmp_path / "paper.pdf")
    doc = c.post(
        "/api/v1/documents",
        files={"file": ("paper.pdf", pdf.read_bytes(), "application/pdf")},
    ).json()
    job = c.post(
        "/api/v1/jobs",
        json={"document_id": doc["document_id"], "parser": "native"},
    ).json()
    run_worker(tmp_path / "data", once=True)
    job_id = job["job_id"]

    records = c.get(f"/api/v1/jobs/{job_id}/records").json()
    seq_ids = {s["sequence_id"] for s in records["sequences"]}
    meas_ids = {m["measurement_id"] for m in records["measurements"]}
    assert seq_ids and meas_ids, records
    # The page map covers exactly the returned records, one key each.
    assert set(records["record_pages"]) == seq_ids | meas_ids
    for pages in records["record_pages"].values():
        assert all(isinstance(p, int) and p >= 1 for p in pages)

    only_seq = c.get(f"/api/v1/jobs/{job_id}/records?kind=sequence").json()
    assert only_seq["measurements"] == []
    assert set(only_seq["record_pages"]) == {s["sequence_id"] for s in only_seq["sequences"]}

    # A needle filter narrows records and their page map to the same ids.
    needle = sorted(seq_ids)[0].lower()
    filtered = c.get(f"/api/v1/jobs/{job_id}/records?q={needle}").json()
    filtered_ids = {s["sequence_id"] for s in filtered["sequences"]} | {
        m["measurement_id"] for m in filtered["measurements"]
    }
    assert filtered_ids == {needle}
    assert set(filtered["record_pages"]) == filtered_ids


def test_document_source_supports_range_and_etag(client) -> None:
    c, tmp_path = client
    pdf = _sample_pdf(tmp_path / "paper.pdf")
    res = c.post(
        "/api/v1/documents",
        files={"file": ("paper.pdf", pdf.read_bytes(), "application/pdf")},
    )
    document_id = res.json()["document_id"]

    res = c.get(f"/api/v1/documents/{document_id}/source")
    assert res.status_code == 200
    etag = res.headers["etag"]
    assert res.headers["accept-ranges"] == "bytes"

    res = c.get(
        f"/api/v1/documents/{document_id}/source",
        headers={"Range": "bytes=0-3", "If-None-Match": etag.replace('"', "") + "x"},
    )
    assert res.status_code == 206
    assert res.content == b"%PDF"

    res = c.get(f"/api/v1/documents/{document_id}/source", headers={"If-None-Match": etag})
    assert res.status_code == 304


def test_unknown_api_route_returns_json_404(client) -> None:
    c, _ = client
    res = c.get("/api/v1/does-not-exist")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "NOT_FOUND"


def test_job_unknown_document_404(client) -> None:
    c, _ = client
    res = c.post(
        "/api/v1/jobs",
        json={"document_id": "sha256:" + "0" * 64, "parser": "native"},
    )
    assert res.status_code == 404


def test_review_decision_flow_with_409(client) -> None:
    c, tmp_path = client
    pdf = _sample_pdf(tmp_path / "paper.pdf")
    doc = c.post(
        "/api/v1/documents",
        files={"file": ("paper.pdf", pdf.read_bytes(), "application/pdf")},
    ).json()
    job = c.post("/api/v1/jobs", json={"document_id": doc["document_id"], "parser": "native"}).json()

    from bio_pdf.workflow.queue_worker import run_worker

    run_worker(tmp_path / "data", once=True)

    reviews = c.get(f"/api/v1/reviews?job_id={job['job_id']}").json()
    assert reviews["total"] >= 1
    item = reviews["reviews"][0]

    # Confirm works.
    res = c.post(
        f"/api/v1/reviews/{item['review_id']}/decisions",
        json={
            "action": "confirm",
            "base_revision": item["record_revision"],
            "actor": "tester",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["accepted"] is True
    assert body["record_revision"] == item["record_revision"] + 1

    # Replaying the same stale revision returns 409 with both values.
    res = c.post(
        f"/api/v1/reviews/{item['review_id']}/decisions",
        json={
            "action": "confirm",
            "base_revision": item["record_revision"],
            "actor": "other-window",
        },
    )
    assert res.status_code == 409
    err = res.json()["error"]
    assert err["code"] == "REVIEW_REVISION_CONFLICT"
    assert err["details"]["live_revision"] == item["record_revision"] + 1

    # Records reflect the accepted state.
    records = c.get(f"/api/v1/jobs/{job['job_id']}/records").json()
    accepted = [
        s for s in records["sequences"] if s["acceptance_state"] == "accepted"
    ] + [m for m in records["measurements"] if m["acceptance_state"] == "accepted"]
    assert len(accepted) >= 1


def test_export_flow_json_and_fasta_empty_fails(client) -> None:
    c, tmp_path = client
    pdf = _sample_pdf(tmp_path / "paper.pdf")
    doc = c.post(
        "/api/v1/documents",
        files={"file": ("paper.pdf", pdf.read_bytes(), "application/pdf")},
    ).json()
    job = c.post("/api/v1/jobs", json={"document_id": doc["document_id"], "parser": "native"}).json()

    from bio_pdf.workflow.queue_worker import run_worker

    run_worker(tmp_path / "data", once=True)

    # B08: default is the REVIEWED set (accepted_only=true). Nothing is
    # accepted yet → the default export contains zero records, so the
    # candidate download must be requested explicitly.
    res = c.post(
        f"/api/v1/jobs/{job['job_id']}/exports",
        json={"format": "json"},
    )
    assert res.status_code == 202, res.text
    assert res.json()["accepted_only"] is True

    res = c.post(
        f"/api/v1/jobs/{job['job_id']}/exports",
        json={"format": "json", "include_candidates": True},
    )
    assert res.status_code == 202, res.text
    assert res.json()["accepted_only"] is False
    export_id = res.json()["export_id"]

    import time

    for _ in range(50):
        status = c.get(f"/api/v1/exports/{export_id}").json()["status"]
        if status in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert status == "completed", c.get(f"/api/v1/exports/{export_id}").json()
    dl = c.get(f"/api/v1/exports/{export_id}/download")
    assert dl.status_code == 200
    body = dl.json()
    assert body["result_revision"] == 0
    assert len(body["sequences"]) >= 1

    # FASTA with nothing accepted fails honestly (no empty file).
    res = c.post(
        f"/api/v1/jobs/{job['job_id']}/exports",
        json={"format": "fasta"},
    )
    fasta_id = res.json()["export_id"]
    for _ in range(50):
        payload = c.get(f"/api/v1/exports/{fasta_id}").json()
        if payload["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert payload["status"] == "failed"
    assert "refusing" in (payload["error"] or "")
    dl = c.get(f"/api/v1/exports/{fasta_id}/download")
    assert dl.status_code == 409
