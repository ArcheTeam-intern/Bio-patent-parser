"""B04: per-page execution, effective config and engine dispatch.

Counterexamples from the review:

- the parse plan grouped pages but the docling child still ran one
  layout_native pass, so a mixed document never actually OCR'd page 2;
- targets passed to create_job never reached WorkflowConfig;
- parser_options (tableformer mode / ocr_lang) never reached the Docling
  converter — the cache hash changed but the engine behaved identically;
- docling_serve / mineru_http names both ran the local Docling SDK;
- an unavailable engine was silently substituted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bio_pdf.application.job_service import JobPaths, JobService
from bio_pdf.config import WorkflowConfig, load_workflow_config
from bio_pdf.errors import WorkflowError
from bio_pdf.workflow.parse_plan import ParsePlan, build_parse_plan, group_execution_runs


def _page(page_number: int, route: str) -> dict:
    return {
        "signals": {"page_number": page_number},
        "route": {"route": route, "reasons": []},
    }


def test_execution_runs_split_by_kind_and_contiguity() -> None:
    """A mixed plan becomes (kind, contiguous-run) execution groups."""

    pages = [
        _page(1, "native"),
        _page(2, "scan_suspect"),  # needs OCR
        _page(3, "native"),
        _page(4, "scan_suspect"),
    ]
    plan = build_parse_plan(
        pages, page_numbers=(1, 2, 3, 4), engine="docling_sdk", ocr_policy="auto"
    )
    runs = group_execution_runs(plan)
    # page2/p4 are OCR, page1/page3 native — NEVER one layout_native pass
    # covering all four pages.
    kinds = {(r.kind, tuple(r.pages)) for r in runs}
    assert ("layout_ocr", (2,)) in kinds
    assert ("layout_ocr", (4,)) in kinds
    assert ("native", (1,)) in kinds
    assert ("native", (3,)) in kinds
    # No run may mix OCR and non-OCR pages.
    for run in runs:
        assert not (set(run.pages) & {2, 4}) or run.kind == "layout_ocr"


def test_targets_reach_the_workflow_config(tmp_path: Path) -> None:
    """create_job(targets=[...]) must land in the persisted effective config."""

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    pdf = tmp_path / "in.pdf"
    c = canvas.Canvas(str(pdf), pagesize=A4)
    c.drawString(50, 800, "hello")
    c.save()

    svc = JobService(JobPaths.under(tmp_path / "data"))
    try:
        job, _ = svc.create_job(pdf, targets=("sequences",))
        from bio_pdf.application.job_service import effective_workflow_config

        cfg = effective_workflow_config(svc.job_config(job.job_id))
        assert list(cfg.targets) == ["sequences"]
    finally:
        svc.close()


def test_parser_options_reach_the_docling_converter(monkeypatch) -> None:
    """build_converter must consume the CONFIGURED options, not defaults."""

    from bio_pdf.parsers import docling_adapter

    captured: dict = {}

    class _FakePipelineOptions:
        def __init__(self) -> None:
            self.do_ocr = True
            self.do_table_structure = False
            self.force_backend_text = True
            self.ocr_options = None
            self.table_structure_options = None

    class _FakeConverter:
        def __init__(self, *, format_options) -> None:
            captured["format_options"] = format_options

    import sys
    import types

    fake_base = types.ModuleType("docling.datamodel.base_models")
    fake_base.InputFormat = types.SimpleNamespace(PDF="pdf")
    fake_pipeline = types.ModuleType("docling.datamodel.pipeline_options")

    class _TableStructureOptions:
        def __init__(self, **kw) -> None:
            captured["table_structure_options"] = kw

    class _Tesseract:
        def __init__(self, **kw) -> None:
            captured["ocr_options"] = kw

    class _OcrMode:
        FULL_PAGE = "full"

    fake_pipeline.PdfPipelineOptions = _FakePipelineOptions
    fake_pipeline.TableStructureOptions = _TableStructureOptions
    fake_pipeline.TesseractCliOcrOptions = _Tesseract
    fake_pipeline.OcrMode = _OcrMode
    fake_docling = types.ModuleType("docling.document_converter")

    class _PdfFormatOption:
        def __init__(self, **kw) -> None:
            captured["pdf_format_option"] = kw

    fake_docling.DocumentConverter = _FakeConverter
    fake_docling.PdfFormatOption = _PdfFormatOption
    fake_docling.InputFormat = fake_base.InputFormat

    monkeypatch.setitem(sys.modules, "docling", types.ModuleType("docling"))
    monkeypatch.setitem(sys.modules, "docling.datamodel", types.ModuleType("docling.datamodel"))
    monkeypatch.setitem(sys.modules, "docling.datamodel.base_models", fake_base)
    monkeypatch.setitem(sys.modules, "docling.datamodel.pipeline_options", fake_pipeline)
    monkeypatch.setitem(sys.modules, "docling.document_converter", fake_docling)
    monkeypatch.setitem(
        sys.modules, "docling.datamodel.document", types.ModuleType("docling.datamodel.document")
    )

    request = {
        "profile": "layout_ocr",
        "parser_options": {
            "tableformer_mode": "accurate",
            "do_table_structure": True,
            "ocr_lang": "chi_sim",
            "engine_timeout_seconds": 300.0,
        },
    }
    docling_adapter._build_converter_from_request(request)
    assert captured.get("ocr_options", {}).get("lang") == ["chi_sim"], captured
    assert captured.get("table_structure_options", {}).get("do_cell_matching") is True


def test_unavailable_engine_rejected_before_enqueue(tmp_path: Path) -> None:
    """docling_serve without a configured service address → ENGINE_UNAVAILABLE."""

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    pdf = tmp_path / "in.pdf"
    c = canvas.Canvas(str(pdf), pagesize=A4)
    c.drawString(50, 800, "hello")
    c.save()

    svc = JobService(JobPaths.under(tmp_path / "data"))
    try:
        with pytest.raises(WorkflowError, match="ENGINE_UNAVAILABLE|unavailable"):
            svc.create_job(pdf, engine="docling_serve")
    finally:
        svc.close()
