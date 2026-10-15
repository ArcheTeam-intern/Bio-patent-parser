"""End-to-end: a heat-matrix style susceptibility table.

Builds a real 1-page PDF (drugs as rows, isolate numbers as columns,
R/S/I glyph cells, caption + CLSI legend), runs the full pipeline with
the native engine, and pins the behaviour the user asked for:

- normalize reconstructs the matrix the parser missed (source flag,
  labels, legend captured as footnote);
- extract turns each call into an antimicrobial_susceptibility
  measurement with entity=drug / target=isolate (axis flipped);
- the figure endpoint exports the matrix region as a standalone PNG.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bio_pdf.api.app import create_app
from bio_pdf.api.settings import ApiSettings

DRUGS = ["Tobramycin", "Meropenem", "Colistin", "Gentamicin"]
ISOLATES = ["0001", "0002", "0003", "0004"]
CALLS = [
    ["R", "S", "R", "I"],
    ["S", "S", "R", "R"],
    ["R", "I", "S", "R"],
    ["S", "R", "R", "S"],
]
LEGEND = (
    "S (green), sensitive; R (orange), resistant; I (yellow), "
    "intermediate; empty cells were not tested."
)


def _matrix_pdf(path: Path) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica", 10)
    c.drawString(60, 700, "TABLE 1 | Antibiotic sensitivity data")
    col_x = [200, 225, 250, 275]
    row_y = [640, 610, 580, 550]
    for r, drug in enumerate(DRUGS):
        c.setFont("Helvetica", 10)
        c.drawString(60, row_y[r], drug)
    c.setFont("Helvetica", 9)
    for r in range(4):
        for j in range(4):
            c.drawString(col_x[j], row_y[r], CALLS[r][j])
    c.setFont("Helvetica", 8)
    for j, iso in enumerate(ISOLATES):
        c.drawString(col_x[j] - 8, 510, iso)
    c.setFont("Helvetica", 8)
    c.drawString(60, 480, LEGEND)
    c.showPage()
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


def test_matrix_pipeline_and_figure_export(client) -> None:
    c, tmp_path = client
    pdf = _matrix_pdf(tmp_path / "matrix.pdf")
    doc = c.post(
        "/api/v1/documents",
        files={"file": ("matrix.pdf", pdf.read_bytes(), "application/pdf")},
    )
    assert doc.status_code == 201, doc.text
    job = c.post(
        "/api/v1/jobs",
        json={"document_id": doc.json()["document_id"], "parser": "native"},
    )
    assert job.status_code == 202, job.text
    job_id = job.json()["job_id"]

    from bio_pdf.workflow.queue_worker import run_worker

    run_worker(tmp_path / "data", once=True)
    detail = c.get(f"/api/v1/jobs/{job_id}").json()
    assert detail["status"] in {"succeeded", "succeeded_with_review"}, detail

    # The matrix table exists, marked as geometry-reconstructed.
    page = c.get(f"/api/v1/jobs/{job_id}/pages/1").json()
    grids = [t for t in page["tables"] if t.get("source") == "geometry_reconstructed"]
    assert len(grids) == 1, page["tables"]
    grid = grids[0]
    data = [cell for cell in grid["cells"] if cell["header_role"] == "data"]
    assert len(data) == 16
    assert grid["caption"] and grid["caption"].startswith("TABLE 1")
    # legend captured as footnote (its block id resolves to the legend text)
    block_text = {b["block_id"]: b["text_raw"] for b in page["blocks"]}
    legend_texts = [block_text.get(fid, "") for fid in grid.get("footnote_refs", [])]
    assert any("sensitive" in t and "resistant" in t for t in legend_texts), legend_texts

    # Extract bound drug rows and isolate columns (axis flipped by vocab).
    rec = c.get(f"/api/v1/jobs/{job_id}/records").json()
    susc = [m for m in rec["measurements"] if m["metric"] == "antimicrobial_susceptibility"]
    assert len(susc) == 16, rec["measurements"]
    assert {m["entity_raw"] for m in susc} == set(DRUGS)
    assert {m["target_raw"] for m in susc} == set(ISOLATES)
    assert rec["table_pages"] == {"1": 1}

    # The region exports as one standalone PNG.
    fig = c.get(
        f"/api/v1/jobs/{job_id}/pages/1/figure.png",
        params={"bbox": ",".join(str(v) for v in grid["bbox"])},
    )
    assert fig.status_code == 200, fig.text
    assert fig.headers["content-type"] == "image/png"
    assert fig.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(fig.content) > 1000

    # Issues surface the reconstruction for human review.
    issues = c.get(f"/api/v1/jobs/{job_id}/issues").json()
    codes = {i["reason_code"] for i in issues["issues"]}
    assert "TABLE_GEOMETRY_RECONSTRUCTED" in codes, codes


def test_figure_endpoint_validates_bbox(client) -> None:
    c, tmp_path = client
    pdf = _matrix_pdf(tmp_path / "m.pdf")
    doc = c.post(
        "/api/v1/documents",
        files={"file": ("m.pdf", pdf.read_bytes(), "application/pdf")},
    ).json()
    job = c.post(
        "/api/v1/jobs", json={"document_id": doc["document_id"], "parser": "native"}
    ).json()
    from bio_pdf.workflow.queue_worker import run_worker

    run_worker(tmp_path / "data", once=True)
    bad = c.get(
        f"/api/v1/jobs/{job['job_id']}/pages/1/figure.png",
        params={"bbox": "10,10,9,9"},
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "BAD_BBOX"
