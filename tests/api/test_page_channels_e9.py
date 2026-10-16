"""E9/P05: page channels endpoint reads the RAW per-engine artifacts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bio_pdf.api.app import create_app
from bio_pdf.api.settings import ApiSettings


def _sample_pdf(path: Path) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    c.drawString(50, 800, "<210> 1")
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


def test_channels_endpoint_separates_every_engine_channel(client) -> None:
    c, tmp_path = client
    pdf = _sample_pdf(tmp_path / "ch.pdf")
    doc = c.post(
        "/api/v1/documents", files={"file": ("ch.pdf", pdf.read_bytes(), "application/pdf")}
    ).json()
    job = c.post(
        "/api/v1/jobs",
        json={"document_id": doc["document_id"], "parser": "docling"},
    ).json()
    job_id = job["job_id"]

    runs = tmp_path / "data" / "jobs" / job_id / "stage_runs"
    # native parse stage (glyph layer)
    native = runs / "j--native_parse--a1--x"
    native.mkdir(parents=True)
    (native / "normalized.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "document_id": doc["document_id"],
                "parser_run_id": "native",
                "pages": [
                    {
                        "page_number": 1,
                        "width_pt": 595.0,
                        "height_pt": 842.0,
                        "route": "native",
                        "blocks": [
                            {
                                "block_id": "n1",
                                "kind": "text",
                                "semantic_role": "unknown",
                                "text_raw": "NATIVE LINE",
                                "bbox": [50.0, 700.0, 200.0, 710.0],
                            }
                        ],
                    }
                ],
                "errors": [],
            }
        ),
        encoding="utf-8",
    )
    # layout parse stage split into profile runs
    layout = runs / "j--layout_parse--a1--y" / "run-0-layout-native"
    layout.mkdir(parents=True)
    (layout / "normalized.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "document_id": doc["document_id"],
                "parser_run_id": "docling",
                "pages": [
                    {
                        "page_number": 1,
                        "width_pt": 595.0,
                        "height_pt": 842.0,
                        "route": "layout_native",
                        "blocks": [
                            {
                                "block_id": "d1",
                                "kind": "text",
                                "semantic_role": "unknown",
                                "text_raw": "DOCLING LINE",
                                "bbox": [50.0, 700.0, 200.0, 710.0],
                            }
                        ],
                    }
                ],
                "errors": [],
            }
        ),
        encoding="utf-8",
    )
    # extract region-OCR sidecar (E9 persistence)
    extract = runs / "j--extract--a1--z"
    extract.mkdir(parents=True)
    (extract / "region-ocr.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "engine": "rapidocr_region",
                "dpi": 300,
                "policy": "st25-residue-fallback",
                "pages": {
                    "1": [{"x0": 60.0, "y0": 471.0, "x1": 531.0, "y1": 481.0, "text": "OCR RESIDUES"}]
                },
            }
        ),
        encoding="utf-8",
    )

    body = c.get(f"/api/v1/jobs/{job_id}/pages/1/channels").json()
    engines = {ch["engine"] for ch in body["channels"]}
    assert {"native", "layout", "rapidocr_region"} <= engines, body["channels"]
    by_engine = {ch["engine"]: ch for ch in body["channels"]}
    assert by_engine["native"]["lines"][0]["text"] == "NATIVE LINE"
    assert by_engine["layout"]["lines"][0]["text"] == "DOCLING LINE"
    assert by_engine["rapidocr_region"]["lines"][0]["text"] == "OCR RESIDUES"
    # Artifact refs point at the real files the reader can open.
    assert by_engine["rapidocr_region"]["artifact_rel"].endswith("region-ocr.json")

    # A page nothing produced → honest empty.
    empty = c.get(f"/api/v1/jobs/{job_id}/pages/99/channels").json()
    assert empty["channels"] == []
