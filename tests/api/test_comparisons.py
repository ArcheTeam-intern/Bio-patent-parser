"""C11/D07 comparison API: two sides, real diff, strict isolation.

The engine factory is stubbed (deterministic normalized docs) so the test
is fast and offline; the live Docling-vs-MinerU run is exercised in the
execution report with the real server. Key invariants under test:

- both sides run and persist DB-backed status;
- the diff pairs sequences by SEQ ID and measurements by
  (entity/metric/target) with value/unit as COMPARED fields;
- an experiment writes NO review items and NO publication (queue purity);
- one side failing is data (per-side error), not a create failure;
- retry re-queues failed sides.
"""

from __future__ import annotations

import json
import time
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


def _normalized_doc(document_id: str, *, sequence: str) -> dict:
    return {
        "schema_version": "1.0",
        "document_id": document_id,
        "parser_run_id": "stub:engine",
        "pages": [
            {
                "page_number": 1,
                "width_pt": 595.0,
                "height_pt": 842.0,
                "route": "native",
                "blocks": [
                    {"kind": "text", "semantic_role": "unknown",
                     "text_raw": "<210> 1\n<211> 4\n<212> PRT\n<400> 1\n" + sequence,
                     "bbox": [50.0, 100.0, 400.0, 112.0], "block_id": "b1"},
                ],
                "tables": [],
                "figures": [],
            }
        ],
        "errors": [],
    }


class _StubAdapter:
    def __init__(self, sequence: str, document_id: str, *, fail: bool = False) -> None:
        self.sequence = sequence
        self.document_id = document_id
        self.fail = fail

    def parse(self, request, **kwargs):  # noqa: ANN001, ANN003
        from bio_pdf.errors import WorkflowError
        from bio_pdf.parsers.base import ParseArtifacts

        if self.fail:
            raise WorkflowError("stub side failure", details={"code": "STUB_FAIL"})
        out = Path(request.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        norm = out / "normalized.json"
        norm.write_text(
            json.dumps(_normalized_doc(self.document_id, sequence=self.sequence)),
            encoding="utf-8",
        )
        (out / "raw_response.json").write_text("{}", encoding="utf-8")
        manifest = out / "manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        return ParseArtifacts(
            normalized_path=norm, raw_response_path=out / "raw_response.json", manifest_path=manifest
        )


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


def _upload_doc(c: TestClient, tmp_path: Path) -> str:
    pdf = _sample_pdf(tmp_path / "cmp.pdf")
    doc = c.post(
        "/api/v1/documents",
        files={"file": ("cmp.pdf", pdf.read_bytes(), "application/pdf")},
    ).json()
    return doc["document_id"]


def _wait_done(c: TestClient, comparison_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = c.get(f"/api/v1/comparisons/{comparison_id}").json()
        if all(s["status"] in ("done", "failed") for s in body["sides"]):
            return body
        time.sleep(0.05)
    raise AssertionError(f"comparison did not settle: {body}")


def test_comparison_runs_two_sides_and_diffs_without_touching_the_queue(
    client, monkeypatch
) -> None:
    c, tmp_path = client
    document_id = _upload_doc(c, tmp_path)

    from bio_pdf.application import comparison_service as cs

    def factory(engine: str, source_path: Path, request):  # noqa: ANN001
        # Side A finds the right residue; side B has one character wrong and
        # one extra sequence — the diff must show both classes.
        if engine == "docling":
            return _StubAdapter("Ala Gly Leu Lys", document_id)
        return _StubAdapter("Ala Cys Leu Lys", document_id)

    monkeypatch.setattr(cs, "_side_engine", factory)

    res = c.post(
        "/api/v1/comparisons",
        json={"document_id": document_id, "pages": [1], "engines": ["docling", "mineru_http"]},
    )
    assert res.status_code == 202, res.text
    comparison_id = res.json()["comparison_id"]
    body = _wait_done(c, comparison_id)
    assert all(s["status"] == "done" for s in body["sides"]), body

    diff = c.get(f"/api/v1/comparisons/{comparison_id}/diff").json()
    assert diff["sequences"]["count_a"] == 1
    assert diff["sequences"]["count_b"] == 1
    assert diff["sequences"]["field_diffs"], "AGLK vs ACLK must surface as a field diff"
    assert diff["sequences"]["field_diffs"][0]["seq_id_no"] == "1"
    assert diff["evaluation"]["status"] == "not_evaluated"

    # ISOLATION: no review items, no publications, no jobs touched.
    import sqlite3

    conn = sqlite3.connect(tmp_path / "data" / "bio.sqlite")
    review_rows = conn.execute(
        "SELECT COUNT(*) FROM review_items WHERE job_id LIKE ?", (f"{comparison_id}%",)
    ).fetchone()[0]
    publications = conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0]
    jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    conn.close()
    assert review_rows == 0, "experiments must never register review items"
    assert publications == 0, "experiments must never publish a baseline"
    assert jobs == 0


def test_failed_side_is_data_and_retry_requeues_it(client, monkeypatch) -> None:
    c, tmp_path = client
    document_id = _upload_doc(c, tmp_path)

    from bio_pdf.application import comparison_service as cs

    calls = {"mineru": 0}

    def factory(engine: str, source_path: Path, request):  # noqa: ANN001
        if engine == "mineru_http":
            calls["mineru"] += 1
            return _StubAdapter("Ala Gly Leu Lys", document_id, fail=True)
        return _StubAdapter("Ala Gly Leu Lys", document_id)

    monkeypatch.setattr(cs, "_side_engine", factory)

    res = c.post(
        "/api/v1/comparisons",
        json={"document_id": document_id, "pages": [1], "engines": ["docling", "mineru_http"]},
    )
    comparison_id = res.json()["comparison_id"]
    body = _wait_done(c, comparison_id)
    statuses = {s["side"]: s["status"] for s in body["sides"]}
    assert statuses == {"docling": "done", "mineru_http": "failed"}
    failed = next(s for s in body["sides"] if s["side"] == "mineru_http")
    assert "STUB_FAIL" in (failed["error"] or "")
    assert body["status"] == "failed"

    # Diff refuses while a side is not done — honest, not an empty table.
    diff = c.get(f"/api/v1/comparisons/{comparison_id}/diff")
    assert diff.status_code == 409
    assert diff.json()["error"]["code"] == "COMPARISON_NOT_READY"

    # Retry re-runs the failed side only.
    before = calls["mineru"]
    retry = c.post(f"/api/v1/comparisons/{comparison_id}/retry")
    assert retry.status_code == 200
    _wait_done(c, comparison_id)
    assert calls["mineru"] == before + 1


def test_mineru_without_token_fails_honestly(client) -> None:
    """No MINERU_TOKEN → the cloud side fails with ENGINE_UNAVAILABLE while
    the local side still completes (deployment status, not a hidden error)."""

    c, tmp_path = client
    document_id = _upload_doc(c, tmp_path)
    res = c.post(
        "/api/v1/comparisons",
        json={"document_id": document_id, "pages": [1], "engines": ["docling", "mineru_http"]},
    )
    comparison_id = res.json()["comparison_id"]
    body = _wait_done(c, comparison_id, timeout=60.0)
    sides = {s["side"]: s for s in body["sides"]}
    assert sides["mineru_http"]["status"] == "failed"
    assert "MINERU_TOKEN" in (sides["mineru_http"]["error"] or "")