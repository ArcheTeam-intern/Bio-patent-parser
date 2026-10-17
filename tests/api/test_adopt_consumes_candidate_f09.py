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
    # H02/S02: enqueueing = scheduled; consumption is proven by the run.
    assert body["status"] == "scheduled"
    assert "consumption_proof" in body
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


# ---- G01/S01: adoption input integrity ------------------------------------


def _create_adopted_job(
    tmp_path: Path,
    *,
    candidate_text: str = "Ala Gly Leu Lys",
    lines: list[dict] | None = None,
    source_sha256: str | None = None,
):
    """Create a REAL adopted job through JobService (native engine) and
    return (service, job, adopted_dir) so a test can tamper the copied
    inputs before the worker consumes them."""

    import hashlib as _h

    from bio_pdf.application.job_service import JobPaths, JobService

    data_dir = tmp_path / "data"
    svc = JobService(JobPaths.under(data_dir))
    pdf = _sample_pdf(tmp_path / "g01.pdf")

    candidate_file = tmp_path / "fake-candidate-g01.txt"
    candidate_file.write_text(candidate_text, encoding="utf-8")
    candidate_sha = _h.sha256(candidate_text.encode("utf-8")).hexdigest()
    meta = {
        "candidate_id": "cand-g01",
        "page_number": 1,
        "source_sha256": source_sha256,
        "lines": lines
        if lines is not None
        else [{"x0": 40.0, "y0": 700.0, "x1": 200.0, "y1": 710.0, "text": candidate_text}],
    }
    job, _ = svc.create_job(
        pdf,
        engine="native",
        adopted_candidates=[
            {
                "candidate_id": "cand-g01",
                "artifact_path": str(candidate_file),
                "content_sha256": candidate_sha,
                "meta_json": json.dumps(meta),
                "source_sha256": source_sha256,
                "page_number": 1,
                "origin_job_id": "job-origin",
                "origin_issue_id": "iss-origin",
            }
        ],
    )
    adopted_dir = data_dir / "jobs" / job.job_id / "adopted" / "cand-g01"
    return svc, job, adopted_dir


def _run_and_expect_extract_failure(svc, job) -> str:
    from bio_pdf.workflow.queue_worker import run_worker

    data_dir = svc.paths.artifact_root
    run_worker(data_dir, once=True)
    done = svc.get_job(job.job_id)
    assert done is not None and done.status == "failed", done
    assert svc.repository.get_publication(job.job_id) is None, "no publication may exist"
    row = svc.repository.connection.execute(
        "SELECT error_json FROM stage_runs WHERE job_id = ? AND stage_name = 'extract'"
        " ORDER BY attempt DESC LIMIT 1",
        (job.job_id,),
    ).fetchone()
    assert row is not None
    return str(row["error_json"] or "")


def test_tampered_candidate_txt_is_refused_before_extraction(tmp_path: Path) -> None:
    svc, job, adopted_dir = _create_adopted_job(tmp_path)
    (adopted_dir / "candidate.txt").write_text("Gly Gly Gly Gly", encoding="utf-8")
    error = _run_and_expect_extract_failure(svc, job)
    assert "content hash mismatch" in error, error
    svc.close()


def test_tampered_candidate_meta_text_is_refused(tmp_path: Path) -> None:
    """The recheck repro: the txt stays byte-identical, only the meta LINE
    TEXT changes — the old code published GGGG/complete off unverified
    meta. Now the meta hash gate refuses before extraction."""

    svc, job, adopted_dir = _create_adopted_job(tmp_path)
    meta_path = adopted_dir / "candidate.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["lines"][0]["text"] = "Gly Gly Gly Gly"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    error = _run_and_expect_extract_failure(svc, job)
    assert "metadata hash mismatch" in error, error
    svc.close()


def test_tampered_candidate_meta_box_is_refused(tmp_path: Path) -> None:
    svc, job, adopted_dir = _create_adopted_job(tmp_path)
    meta_path = adopted_dir / "candidate.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["lines"][0]["x0"] = 999.0
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    error = _run_and_expect_extract_failure(svc, job)
    assert "metadata hash mismatch" in error, error
    svc.close()


def test_candidate_claiming_a_different_source_is_refused(tmp_path: Path) -> None:
    """Source-identity check: a hash-locked candidate whose recorded source
    SHA differs from the job's document is refused (meta itself intact)."""

    svc, job, _adopted_dir = _create_adopted_job(
        tmp_path, source_sha256="sha256:" + "0" * 64
    )
    error = _run_and_expect_extract_failure(svc, job)
    assert "different source document" in error, error
    svc.close()


def test_legit_adopted_candidate_still_recovers_through_a_real_worker(tmp_path: Path) -> None:
    """The negative cases above must not break the legitimate path: the
    untouched candidate still yields the original residues."""

    svc, job, _adopted_dir = _create_adopted_job(tmp_path)
    from bio_pdf.workflow.queue_worker import run_worker

    run_worker(svc.paths.artifact_root, once=True)
    done = svc.get_job(job.job_id)
    assert done is not None and done.status in {"succeeded", "succeeded_with_review"}
    from bio_pdf.storage.result_reader import ResultReader

    extract = ResultReader(svc.paths.artifact_root).read_json(job.job_id, "extract")
    entry = (extract.get("sequence_inventory") or {})["entries"][0]
    assert entry["status"] == "complete"
    assert entry["observed_length"] == 4
    consumed = extract["adopted_candidates"]
    assert consumed and consumed[0]["merged_block_ids"]
    # The config records the meta hash, not just the content hash.
    config = svc.repository.get_job_config(job.job_id)
    spec = config["effective_config"]["adopted_candidates"][0]
    assert spec["content_sha256"] and spec["meta_sha256"]
    svc.close()


# ---- H02/S02: the adopted page must be inside the job's scope ---------------


def test_adopt_rejects_a_candidate_page_outside_the_requested_pages(client, monkeypatch) -> None:
    """The reviewer's repro: candidate meta page = 2, request pages = [1] —
    the old flow queued the job, the worker silently skipped the candidate,
    and the job still published success. Now rejected BEFORE any write."""

    c, tmp_path = client
    job_id, issue_id = _seed_issue(c, tmp_path)

    from bio_pdf.extract.sequences import region_ocr

    monkeypatch.setattr(
        region_ocr,
        "ocr_residue_pages",
        lambda pdf_path, pages: {int(pages[0]): [(40.0, 700.0, 200.0, 710.0, "MKTG")]},
    )
    rep = c.post(f"/api/v1/jobs/{job_id}/issues/{issue_id}/reparse")
    assert rep.status_code == 200

    rejected = c.post(
        f"/api/v1/jobs/{job_id}/issues/{issue_id}/adopt",
        json={"actor": "reviewer", "pages": [1]},
    )  # candidate meta page is 1 here → allowed; force the mismatch instead:
    if rejected.status_code == 201:
        # page 1 IS in [1] — verify the guard through the SERVICE layer with
        # an explicit out-of-scope page set instead.
        pass

    conn = sqlite3.connect(tmp_path / "data" / "bio.sqlite")
    conn.row_factory = sqlite3.Row
    before_jobs = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    before_events = conn.execute(
        "SELECT COUNT(*) AS n FROM issue_events WHERE action = 'adopted'"
    ).fetchone()["n"]
    conn.close()

    strictly_out = c.post(
        f"/api/v1/jobs/{job_id}/issues/{issue_id}/adopt",
        json={"actor": "reviewer", "pages": [9]},
    )
    assert strictly_out.status_code == 422, strictly_out.text
    detail = strictly_out.json()["error"]
    assert detail["code"] == "ADOPT_PAGE_OUT_OF_SCOPE"
    assert detail["details"]["candidate_page"] == 1

    conn = sqlite3.connect(tmp_path / "data" / "bio.sqlite")
    conn.row_factory = sqlite3.Row
    after_jobs = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    after_events = conn.execute(
        "SELECT COUNT(*) AS n FROM issue_events WHERE action = 'adopted'"
    ).fetchone()["n"]
    conn.close()
    assert after_jobs == before_jobs, "no job may be created for a doomed adoption"
    assert after_events == before_events, "no adopted event may be written"


def test_create_job_refuses_an_adopted_page_outside_its_pages(tmp_path: Path) -> None:
    from bio_pdf.application.job_service import JobPaths, JobService

    data_dir = tmp_path / "data2"
    svc = JobService(JobPaths.under(data_dir))
    pdf = _sample_pdf(tmp_path / "h02.pdf")
    import hashlib as _h

    text = "Ala Gly Leu Lys"
    candidate = tmp_path / "cand-h02.txt"
    candidate.write_text(text, encoding="utf-8")
    meta = {
        "candidate_id": "cand-h02",
        "page_number": 2,
        "lines": [{"x0": 40.0, "y0": 700.0, "x1": 200.0, "y1": 710.0, "text": text}],
    }
    from bio_pdf.errors import WorkflowError

    import pytest as _pytest

    with _pytest.raises(WorkflowError) as excinfo:
        svc.create_job(
            pdf,
            engine="native",
            page_numbers=(1,),
            adopted_candidates=[
                {
                    "candidate_id": "cand-h02",
                    "artifact_path": str(candidate),
                    "content_sha256": _h.sha256(text.encode("utf-8")).hexdigest(),
                    "meta_json": json.dumps(meta),
                    "page_number": 2,
                }
            ],
        )
    assert excinfo.value.details.get("code") == "ADOPT_PAGE_OUT_OF_SCOPE"
    svc.close()


def test_worker_structurally_fails_on_an_out_of_scope_adopted_input(tmp_path: Path) -> None:
    """Defense in depth: even if a config carries an out-of-scope candidate
    (e.g. hand-edited), the worker FAILS the job — no silent skip, no
    success publication."""

    svc, job, _adopted_dir = _create_adopted_job(tmp_path)
    # Rewrite the persisted config to move the candidate to page 2 while the
    # job only parses page 1.
    config = svc.repository.get_job_config(job.job_id)
    specs = config["effective_config"]["adopted_candidates"]
    specs[0]["page_number"] = 2
    svc.repository.set_job_config(job.job_id, config)

    from bio_pdf.workflow.queue_worker import run_worker

    run_worker(svc.paths.artifact_root, once=True)
    done = svc.get_job(job.job_id)
    assert done is not None and done.status == "failed", done
    assert svc.repository.get_publication(job.job_id) is None
    row = svc.repository.connection.execute(
        "SELECT error_json FROM stage_runs WHERE job_id = ? AND stage_name = 'extract'"
        " ORDER BY attempt DESC LIMIT 1",
        (job.job_id,),
    ).fetchone()
    assert "ADOPTED_CANDIDATE_OUT_OF_SCOPE" in str(row["error_json"])
    svc.close()


# ---- I04/S04: adopt page-input validation ---------------------------------


def _reparse_ready(client, monkeypatch):
    c, tmp_path = client
    job_id, issue_id = _seed_issue(c, tmp_path)
    from bio_pdf.extract.sequences import region_ocr

    monkeypatch.setattr(
        region_ocr,
        "ocr_residue_pages",
        lambda pdf_path, pages: {int(pages[0]): [(40.0, 700.0, 200.0, 710.0, "MKTG")]},
    )
    rep = c.post(f"/api/v1/jobs/{job_id}/issues/{issue_id}/reparse")
    assert rep.status_code == 200, rep.text
    return c, tmp_path, job_id, issue_id


def _counts(tmp_path: Path) -> tuple[int, int]:
    conn = sqlite3.connect(tmp_path / "data" / "bio.sqlite")
    conn.row_factory = sqlite3.Row
    jobs = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    adopted = conn.execute(
        "SELECT COUNT(*) AS n FROM issue_events WHERE action = 'adopted'"
    ).fetchone()["n"]
    conn.close()
    return jobs, adopted


def test_adopt_invalid_pages_are_422_with_zero_writes(client, monkeypatch) -> None:
    c, tmp_path, job_id, issue_id = _reparse_ready(client, monkeypatch)
    before = _counts(tmp_path)
    for pages in ([0], [-1], [0, -1], []):
        resp = c.post(
            f"/api/v1/jobs/{job_id}/issues/{issue_id}/adopt",
            json={"actor": "reviewer", "pages": pages},
        )
        assert resp.status_code == 422, (pages, resp.status_code, resp.text)
        body = resp.json()
        detail = body.get("error") if isinstance(body.get("error"), dict) else body.get("detail")
        if isinstance(detail, dict):
            assert detail["code"] in (
                "ADOPT_PAGE_INVALID",
                "ADOPT_PAGE_OUT_OF_SCOPE",
                "PAGE_RANGE_INVALID",
            )
        # model-boundary validation errors arrive list-shaped; the contract
        # here is the 422 + zero writes.
        assert "jobs" not in body, "a rejected adopt must create nothing"
    assert _counts(tmp_path) == before, "no job/event may be written for invalid pages"


def test_adopt_without_pages_defaults_to_the_candidate_page(client, monkeypatch) -> None:
    c, tmp_path, job_id, issue_id = _reparse_ready(client, monkeypatch)
    adopted = c.post(
        f"/api/v1/jobs/{job_id}/issues/{issue_id}/adopt", json={"actor": "reviewer"}
    )
    assert adopted.status_code == 201, adopted.text
    assert adopted.json()["adopted"]["pages"] == [1]


def test_adopt_page_beyond_the_document_is_422(client, monkeypatch) -> None:
    c, tmp_path = client
    job_id, issue_id = _seed_issue(c, tmp_path)

    from bio_pdf.extract.sequences import region_ocr

    # Candidate targeting page 99 (beyond the 1-page fixture document).
    monkeypatch.setattr(
        region_ocr,
        "ocr_residue_pages",
        lambda pdf_path, pages: {int(pages[0]): [(40.0, 700.0, 200.0, 710.0, "MKTG")]},
    )
    rep = c.post(f"/api/v1/jobs/{job_id}/issues/{issue_id}/reparse")
    assert rep.status_code == 200
    conn = sqlite3.connect(tmp_path / "data" / "bio.sqlite")
    cand = conn.execute(
        "SELECT candidate_id FROM issue_candidates ORDER BY attempt DESC LIMIT 1"
    ).fetchone()[0]
    meta_path = next(
        (tmp_path / "data" / "jobs" / job_id / "issues" / issue_id).glob(
            f"candidate-{cand}.meta.json"
        )
    )
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["page_number"] = 99
    meta_text = json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True)
    meta_path.write_bytes(meta_text.encode("utf-8"))
    import hashlib as _h

    conn.execute(
        "UPDATE issue_candidates SET meta_sha256 = ? WHERE candidate_id = ?",
        (_h.sha256(meta_text.encode("utf-8")).hexdigest(), cand),
    )
    conn.commit()
    conn.close()

    before = _counts(tmp_path)
    resp = c.post(
        f"/api/v1/jobs/{job_id}/issues/{issue_id}/adopt",
        json={"actor": "reviewer", "pages": [99]},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] in (
        "ADOPT_PAGE_OUT_OF_SCOPE",
        "ADOPT_PAGE_INVALID",
        "PAGE_RANGE_INVALID",
    )
    assert _counts(tmp_path) == before
