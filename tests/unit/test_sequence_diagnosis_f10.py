"""F10: layered sequence diagnosis + the pending character-level queue."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bio_pdf.application.job_service import JobPaths, JobService


def _declaration_only_pdf(path: Path) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    t = c.beginText(50, 800)
    for line in ["<210> 1", "<211> 4", "<212> PRT", "<400> 1"]:
        t.textLine(line)
    c.drawText(t)
    c.save()
    return path


def _run_job_with_ocr(tmp_path: Path, ocr_lines):
    from bio_pdf.extract.sequences import region_ocr
    from bio_pdf.workflow.queue_worker import run_worker

    data_dir = tmp_path / "data"
    svc = JobService(JobPaths.under(data_dir))
    pdf = _declaration_only_pdf(tmp_path / "seq.pdf")
    job, _ = svc.create_job(pdf, engine="native")

    import bio_pdf.extract.sequences.region_ocr as ro

    original = ro.ocr_residue_pages
    ro.ocr_residue_pages = lambda pdf_path, pages: {  # type: ignore[assignment]
        int(p): ocr_lines for p in pages
    }
    try:
        run_worker(data_dir, once=True)
    finally:
        ro.ocr_residue_pages = original  # type: ignore[assignment]
    done = svc.get_job(job.job_id)
    assert done is not None and done.status in {"succeeded", "succeeded_with_review"}
    svc.close()
    return data_dir, job.job_id


def test_diagnosis_explains_ocr_present_but_missing(tmp_path: Path) -> None:
    """The recheck's core demand: a missing declaration whose page DID get
    OCR must show the OCR text and say the miss is NOT a budget issue."""

    # OCR ran on page 1 and produced residue-like text. The residue parser
    # still rejects it (the line is placed inside the <400> region but the
    # text contains no parseable residues), so status stays missing.
    data_dir, job_id = _run_job_with_ocr(
        tmp_path, [(40.0, 700.0, 200.0, 710.0, "XXXX")]
    )

    from bio_pdf.evaluation.sequence_diagnosis import diagnose_sequences

    payload = diagnose_sequences(data_dir, job_id)
    assert payload["entries"], payload
    entry = payload["entries"][0]
    assert entry["seq_id_no"] == "1"
    assert entry["region_ocr"]["entered"] is True
    assert entry["region_ocr"]["line_count"] == 1
    assert "XXXX" in entry["region_ocr"]["text"]
    # Ragged "XXXX" cannot become residues → the diagnosis must say the OCR
    # text exists and point at the fusion/parser, not at the budget.
    if entry["parse_outcome"]["status"] in ("missing", "declared", "partial"):
        joined = " ".join(entry["diagnosis"])
        assert "OCR text EXISTS" in joined or "OCR ran" in joined
        assert "budget" not in joined.lower() or "NOT a budget issue" in joined

    # And the fused feed layer shows what the merge kept per source.
    assert "rapidocr_region" in entry["fused_feed"] or "native" in entry["fused_feed"]


def test_diagnosis_reports_never_ocrd_pages_as_selection_not_quality(tmp_path: Path) -> None:
    data_dir, job_id = _run_job_with_ocr(tmp_path, [])  # OCR returned nothing

    from bio_pdf.evaluation.sequence_diagnosis import diagnose_sequences

    payload = diagnose_sequences(data_dir, job_id)
    entry = payload["entries"][0]
    joined = " ".join(entry["diagnosis"])
    assert "never entered region OCR" in joined or "OCR ran but returned nothing" in joined


def test_annotation_queue_is_a_draft_v2_manifest(tmp_path: Path) -> None:
    data_dir, job_id = _run_job_with_ocr(
        tmp_path, [(40.0, 700.0, 200.0, 710.0, "Ala Gly Leu Lys")]
    )

    from bio_pdf.evaluation.sequence_diagnosis import build_annotation_queue

    draft = build_annotation_queue(data_dir, job_id, document_pages=[1])
    assert draft["schema_version"] == "v2"
    assert draft["draft"] is True
    assert draft["scopes"][0]["scope_verified"] is False
    assert draft["annotations"], draft
    item = draft["annotations"][0]
    assert item["kind"] == "sequence"
    assert item["human_verified"] is False and item["status"] == "draft"
    assert item["seq_id_no"] == "1"
    assert "declared length 4" in (item["note"] or "")

    # The draft loads through the REAL gold loader and evaluates as
    # not_evaluated — a draft never counts as gold.
    path = tmp_path / "gold-draft.json"
    path.write_text(json.dumps(draft), encoding="utf-8")
    from bio_pdf.evaluation.corpus import evaluation_status, load_corpus_scope

    scopes, annotations = load_corpus_scope(path)
    assert annotations and all(a.status == "draft" for a in annotations)
    assert evaluation_status(scopes, annotations) == "not_evaluated"
