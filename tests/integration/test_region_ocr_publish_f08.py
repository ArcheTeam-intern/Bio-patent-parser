"""F08/P04+P05: region-OCR sidecar publication, table-cell channels, history.

The recheck found: (a) `region-ocr.json` was written to disk but never part
of the published manifest (no hash audit), (b) the channels endpoint ignored
tables (`page.blocks` only, so a table-only page had NO channel), and
(c) the endpoint walked raw stage_runs dirs, mixing current and historical
attempts. These tests lock the fixed behaviour end to end.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bio_pdf.api.app import create_app
from bio_pdf.api.settings import ApiSettings
from bio_pdf.application.job_service import JobPaths, JobService


def _declaration_only_pdf(tmp_path: Path) -> Path:
    """A declaration with a <400> region but NO residues in the text layer —
    exactly the case the region-OCR fallback exists for."""

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    pdf = tmp_path / "seq.pdf"
    c = canvas.Canvas(str(pdf), pagesize=A4)
    t = c.beginText(50, 800)
    for line in ["<210> 1", "<211> 4", "<212> PRT", "<400> 1"]:
        t.textLine(line)
    c.drawText(t)
    c.save()
    return pdf


def test_region_ocr_sidecar_is_published_and_hash_verified(tmp_path, monkeypatch) -> None:
    from bio_pdf.extract.sequences import region_ocr

    monkeypatch.setattr(
        region_ocr,
        "ocr_residue_pages",
        lambda pdf_path, pages: {
            int(p): [(40.0, 700.0, 200.0, 710.0, "Ala Gly Leu Lys")] for p in pages
        },
    )

    data_dir = tmp_path / "data"
    svc = JobService(JobPaths.under(data_dir))
    pdf = _declaration_only_pdf(tmp_path)
    job, _ = svc.create_job(pdf)

    from bio_pdf.workflow.queue_worker import run_worker

    run_worker(data_dir, once=True)
    done = svc.get_job(job.job_id)
    assert done is not None and done.status in {"succeeded", "succeeded_with_review"}
    svc.close()

    # F08/P05: the sidecar IS in the published manifest (hash included).
    from bio_pdf.storage.result_reader import ResultReader

    manifest = ResultReader(data_dir).read_published_manifest(job.job_id)
    extract_outputs = manifest["stages"]["extract"]
    sidecar_entry = extract_outputs["region-ocr.json"]
    assert sidecar_entry["sha256"], "the sidecar must be hash-registered at publish"

    settings = ApiSettings(data_dir=data_dir, samples_dir=tmp_path / "samples")
    app = create_app(settings)
    with TestClient(app) as c:
        body = c.get(f"/api/v1/jobs/{job.job_id}/pages/1/channels").json()
        assert body["manifest_verified"] is True
        ocr_channel = next(ch for ch in body["channels"] if ch["engine"] == "rapidocr_region")
        assert ocr_channel["lines"][0]["text"] == "Ala Gly Leu Lys"

        # F08/P05: corrupting the published sidecar is RESULT_CORRUPT — never
        # silently skipped or degraded to an empty channel list.
        sidecar_path = data_dir / sidecar_entry["relative_path"]
        sidecar_path.write_text('{"pages": {}}', encoding="utf-8")
        r = c.get(f"/api/v1/jobs/{job.job_id}/pages/1/channels")
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "RESULT_CORRUPT"


def test_failed_attempts_are_history_not_current_channels(tmp_path, monkeypatch) -> None:
    from bio_pdf.extract.sequences import region_ocr

    monkeypatch.setattr(
        region_ocr,
        "ocr_residue_pages",
        lambda pdf_path, pages: {int(p): [(40.0, 700.0, 200.0, 710.0, "Ala Gly Leu Lys")] for p in pages},
    )

    data_dir = tmp_path / "data"
    svc = JobService(JobPaths.under(data_dir))
    pdf = _declaration_only_pdf(tmp_path)
    job, _ = svc.create_job(pdf)

    from bio_pdf.workflow.queue_worker import run_worker

    run_worker(data_dir, once=True)
    svc.close()

    # An older/failed layout attempt that is NOT part of the publication.
    old_attempt = data_dir / "jobs" / job.job_id / "stage_runs" / "run-old--layout_parse--crashed"
    old_attempt.mkdir(parents=True)
    (old_attempt / "normalized.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "document_id": job.document_id,
                "parser_run_id": "docling",
                "pages": [
                    {
                        "page_number": 1,
                        "width_pt": 595.0,
                        "height_pt": 842.0,
                        "route": "layout_native",
                        "blocks": [
                            {
                                "block_id": "old1",
                                "kind": "text",
                                "semantic_role": "unknown",
                                "text_raw": "STALE ATTEMPT TEXT",
                                "bbox": [10.0, 10.0, 100.0, 20.0],
                            }
                        ],
                    }
                ],
                "errors": [],
            }
        ),
        encoding="utf-8",
    )

    settings = ApiSettings(data_dir=data_dir, samples_dir=tmp_path / "samples")
    app = create_app(settings)
    with TestClient(app) as c:
        body = c.get(f"/api/v1/jobs/{job.job_id}/pages/1/channels").json()
        texts = [
            line["text"] for ch in body["channels"] for line in ch["lines"]
        ]
        assert "STALE ATTEMPT TEXT" not in texts, "a failed attempt must not masquerade as current"
        history = body["history"]
        stale = next(h for h in history if h["run"].endswith("--layout_parse--crashed"))
        assert stale["status"] in ("failed_or_incomplete", "completed")
        assert stale["run"] not in {
            ch.get("run") for ch in body["channels"] if ch["engine"] == "layout"
        }


def test_table_only_page_still_has_a_channel_with_cells(tmp_path) -> None:
    """F08/P04 recheck repro: a layout page with NO text blocks but a table
    with cells must still appear as a channel carrying the cells — the
    activity tables are exactly what a reviewer compares by hand."""

    data_dir = tmp_path / "data"
    svc = JobService(JobPaths.under(data_dir))
    pdf = _declaration_only_pdf(tmp_path)
    job, _ = svc.create_job(pdf)
    job_id = job.job_id
    svc.close()

    runs = data_dir / "jobs" / job_id / "stage_runs"
    layout = runs / "j--layout_parse--a1--t"
    layout.mkdir(parents=True)
    (layout / "normalized.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "document_id": job.document_id,
                "parser_run_id": "docling",
                "pages": [
                    {
                        "page_number": 1,
                        "width_pt": 595.0,
                        "height_pt": 842.0,
                        "route": "layout_native",
                        "blocks": [],
                        "tables": [
                            {
                                "table_id": "7:t0",
                                "page_number": 1,
                                "row_count": 2,
                                "column_count": 2,
                                "caption": "MIC table",
                                "bbox": [40.0, 100.0, 500.0, 200.0],
                                "cells": [
                                    {"cell_id": "7:t0:r0c0", "row_index": 0, "column_index": 0,
                                     "rowspan": 1, "colspan": 1, "text_raw": "Compound",
                                     "header_role": "column", "bbox": [40.0, 100.0, 200.0, 110.0]},
                                    {"cell_id": "7:t0:r1c1", "row_index": 1, "column_index": 1,
                                     "rowspan": 1, "colspan": 1, "text_raw": "MIC 1 uM",
                                     "header_role": "unknown", "bbox": [40.0, 120.0, 200.0, 130.0]},
                                ],
                            }
                        ],
                    }
                ],
                "errors": [],
            }
        ),
        encoding="utf-8",
    )

    settings = ApiSettings(data_dir=data_dir, samples_dir=tmp_path / "samples")
    app = create_app(settings)
    with TestClient(app) as c:
        body = c.get(f"/api/v1/jobs/{job_id}/pages/1/channels").json()
        layout_channel = next(ch for ch in body["channels"] if ch["engine"] == "layout")
        assert layout_channel["lines"] == []
        tables = layout_channel["tables"]
        assert len(tables) == 1
        assert tables[0]["table_id"] == "7:t0"
        cells = {c["text"] for c in tables[0]["cells"]}
        assert "MIC 1 uM" in cells and "Compound" in cells
        assert tables[0]["cells"][0]["header_role"] == "column"
