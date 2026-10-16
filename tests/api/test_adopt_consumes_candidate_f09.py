"""F09: adoption really CONSUMES the candidate — and one outward baseline.

The recheck: "采用候选" only created a new job over the same PDF; the
candidate content never entered it, so adopting A vs B changed nothing.
And the outward baseline_id had three different values (DB/validation,
ResultReader's manifest-sha fallback, published_at). Both are locked here.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bio_pdf.api.app import create_app
from bio_pdf.api.settings import ApiSettings
from bio_pdf.application.job_service import JobPaths, JobService


def _sample_pdf(path: Path) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    t = c.beginText(50, 800)
    for line in ["<210> 1", "<211> 4", "<212> PRT", "<400> 1"]:
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


def _seed_issue(c: TestClient, tmp_path: Path) -> tuple[str, str]:
    pdf = _sample_pdf(tmp_path / "i.pdf")
    doc = c.post(
        "/api/v1/documents", files={"file": ("i.pdf", pdf.read_bytes(), "application/pdf")}
    ).json()
    job = c.post(
        "/api/v1/jobs", json={"document_id": doc["document_id"], "parser": "native"}
    ).json()
    job_id = job["job_id"]
    conn = sqlite3.connect(tmp_path / "data" / "bio.sqlite")
    conn.execute(
        "INSERT INTO parse_issues (issue_id, job_id, scope, reason_code, source_json, detail, state, created_at)"
        " VALUES ('iss-f09', ?, 'sequence', 'SEQ_INVENTORY_MISSING', '[\"st25:1\"]', 'SEQ ID 1 (page 1): region reached, no residues', 'open', '2026-10-16T00:00:00+00:00')",
        (job_id,),
    )
    conn.commit()
    conn.close()
    return job_id, "iss-f09"


def test_adopt_records_candidate_as_new_job_input(client, monkeypatch) -> None:
    """The adopted candidate's identity + content are part of the NEW job's
    persisted inputs (and its config hash) — not just a new queued job."""

    c, tmp_path = client
    job_id, issue_id = _seed_issue(c, tmp_path)

    from bio_pdf.extract.sequences import region_ocr

    monkeypatch.setattr(
        region_ocr,
        "ocr_residue_pages",
        lambda pdf_path, pages: {
            int(pages[0]): [(40.0, 700.0, 200.0, 710.0, "MKTG ADOPTED")]
        },
    )
    rep = c.post(f"/api/v1/jobs/{job_id}/issues/{issue_id}/reparse")
    assert rep.status_code == 200, rep.text
    candidate = rep.json()["candidate"]

    adopted = c.post(f"/api/v1/jobs/{job_id}/issues/{issue_id}/adopt", json={"actor": "reviewer"})
    assert adopted.status_code == 201, adopted.text
    body = adopted.json()["adopted"]
    assert body["consumed_by_new_job"] is True
    assert body["candidate_id"] == candidate["candidate_id"]
    new_job_id = body["new_job_id"]

    # 1. The new job's PERSISTED effective config carries the candidate.
    conn = sqlite3.connect(tmp_path / "data" / "bio.sqlite")
    conn.row_factory = sqlite3.Row
    config_row = conn.execute(
        "SELECT config_json FROM job_configs WHERE job_id = ?", (new_job_id,)
    ).fetchone()
    conn.close()
    config = json.loads(config_row["config_json"])
    specs = config["effective_config"]["adopted_candidates"]
    assert len(specs) == 1
    assert specs[0]["candidate_id"] == candidate["candidate_id"]
    assert specs[0]["content_sha256"] == candidate["content_sha256"]
    assert specs[0]["origin_job_id"] == job_id
    assert specs[0]["origin_issue_id"] == issue_id
    assert specs[0]["page_number"] == 1

    # 2. The candidate bytes were hash-verified and copied into the job.
    copied = (
        tmp_path / "data" / "jobs" / new_job_id / "adopted" / candidate["candidate_id"] / "candidate.txt"
    )
    assert copied.is_file()
    import hashlib as _h

    assert _h.sha256(copied.read_bytes()).hexdigest() == candidate["content_sha256"]

    # 3. The event note names the candidate identity, not just a job.
    events = c.get(f"/api/v1/jobs/{job_id}/issues/{issue_id}").json()["events"]
    adopted_event = next(e for e in events if e["action"] == "adopted")
    assert candidate["candidate_id"] in (adopted_event["note"] or "")
    assert new_job_id in (adopted_event["note"] or "")


def test_adopted_content_is_actually_consumed_by_the_new_extraction(tmp_path, monkeypatch) -> None:
    """End-to-end: with region OCR UNAVAILABLE (raising), the residues can
    only come from the adopted candidate — the new extract must recover
    them, publish the sidecar with the adopted section, and keep the
    candidate identity on the merged feed blocks."""

    from bio_pdf.extract.sequences import region_ocr

    def _ocr_down(pdf_path, pages):  # noqa: ANN001
        raise RuntimeError("region OCR unavailable in this environment")

    monkeypatch.setattr(region_ocr, "ocr_residue_pages", _ocr_down)

    data_dir = tmp_path / "data"
    svc = JobService(JobPaths.under(data_dir))
    pdf = _sample_pdf(tmp_path / "consume.pdf")

    # A candidate as the reparse endpoint would have produced it.
    candidate_text = "Ala Gly Leu Lys"
    import hashlib as _h

    candidate_sha = _h.sha256(candidate_text.encode("utf-8")).hexdigest()
    (tmp_path / "fake-candidate.txt").write_text(candidate_text, encoding="utf-8")

    job, _ = svc.create_job(
        pdf,
        engine="native",
        adopted_candidates=[
            {
                "candidate_id": "cand-f09test0001",
                "artifact_path": str(tmp_path / "fake-candidate.txt"),
                "content_sha256": candidate_sha,
                "meta_json": json.dumps(
                    {
                        "candidate_id": "cand-f09test0001",
                        "page_number": 1,
                        "lines": [
                            {"x0": 40.0, "y0": 700.0, "x1": 200.0, "y1": 710.0, "text": candidate_text}
                        ],
                    }
                ),
                "page_number": 1,
                "origin_job_id": "job-origin",
                "origin_issue_id": "iss-origin",
            }
        ],
    )

    from bio_pdf.workflow.queue_worker import run_worker

    run_worker(data_dir, once=True)
    done = svc.get_job(job.job_id)
    assert done is not None and done.status in {"succeeded", "succeeded_with_review"}
    svc.close()

    from bio_pdf.storage.result_reader import ResultReader

    reader = ResultReader(data_dir)
    extract = reader.read_json(job.job_id, "extract")
    consumed = extract["adopted_candidates"]
    assert consumed and consumed[0]["candidate_id"] == "cand-f09test0001"
    assert consumed[0]["merged_block_ids"], "the feed blocks must carry the candidate identity"

    # Residues recovered DESPITE region OCR being down → they came from the
    # adopted candidate, and the inventory says complete.
    inventory = extract["sequence_inventory"]
    entry = inventory["entries"][0]
    assert entry["status"] == "complete", entry
    assert entry["observed_length"] == 4

    # The sidecar (published, hash-registered) separates adopted content.
    manifest = reader.read_published_manifest(job.job_id)
    sidecar_entry = manifest["stages"]["extract"]["region-ocr.json"]
    sidecar = json.loads((data_dir / sidecar_entry["relative_path"]).read_text(encoding="utf-8"))
    assert sidecar["adopted_candidates"]["1"][0]["text"] == candidate_text
    assert sidecar["pages"] == {}

    # The channels endpoint shows it as its own channel.
    settings = ApiSettings(data_dir=data_dir, samples_dir=tmp_path / "samples")
    app = create_app(settings)
    with TestClient(app) as c:
        channels = c.get(f"/api/v1/jobs/{job.job_id}/pages/1/channels").json()["channels"]
        adopted_channel = next(ch for ch in channels if ch["engine"] == "adopted_candidate")
        assert adopted_channel["lines"][0]["text"] == candidate_text


def test_outward_baseline_id_is_one_value(client, monkeypatch) -> None:
    """F09: DB publication, published manifest and the evidence endpoint must
    report the SAME baseline_id (previously: validation digest vs manifest
    sha vs published_at)."""

    c, tmp_path = client
    pdf = _sample_pdf(tmp_path / "base.pdf")
    doc = c.post(
        "/api/v1/documents", files={"file": ("base.pdf", pdf.read_bytes(), "application/pdf")}
    ).json()
    job = c.post(
        "/api/v1/jobs", json={"document_id": doc["document_id"], "parser": "native"}
    ).json()
    job_id = job["job_id"]

    from bio_pdf.workflow.queue_worker import run_worker

    run_worker(tmp_path / "data", once=True)

    conn = sqlite3.connect(tmp_path / "data" / "bio.sqlite")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT baseline_id FROM publications WHERE job_id = ?", (job_id,)
    ).fetchone()
    conn.close()
    db_baseline = row["baseline_id"]

    from bio_pdf.storage.result_reader import ResultReader

    manifest = ResultReader(tmp_path / "data").read_published_manifest(job_id)
    manifest_baseline = manifest.get("baseline_id")
    assert manifest_baseline == db_baseline, (manifest_baseline, db_baseline)
    assert manifest["published_at"]  # separate field, kept
    assert manifest["_manifest_rel"]

    extract = ResultReader(tmp_path / "data").read_json(job_id, "extract")
    spans = extract.get("evidence_spans") or []
    if spans:
        evidence = c.get(
            f"/api/v1/jobs/{job_id}/evidence/{spans[0]['evidence_id']}"
        ).json()
        assert evidence["baseline_id"] == db_baseline
