"""Live integration test for the Docling adapter.

This test is opt-in via the ``BIO_PDF_LIVE_TESTS=1`` environment variable.
The default CI / unit suite skips it because running it requires:

- the ``docling`` extra (``uv sync --extra docling``)
- potentially a heavy model download on first run
- (optionally) a Tesseract install for the ``layout_ocr`` profile

When the marker is missing we emit an informational log line and skip.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pypdfium2 as pdfium
import pytest

from bio_pdf.parsers.docling_adapter import DoclingAdapter, docling_is_available
from bio_pdf.parsers.normalize_docling import normalise_docling_file
from bio_pdf.parsers.base import ParseRequest


LIVE_ENV_VAR = "BIO_PDF_LIVE_TESTS"

pytestmark = pytest.mark.skipif(
    not os.environ.get(LIVE_ENV_VAR),
    reason=f"set {LIVE_ENV_VAR}=1 to enable Docling live tests",
)


def _build_pdf(path: Path, *, pages: int = 1) -> Path:
    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(width=595, height=842)
    pdf.save(str(path))
    pdf.close()
    return path


def test_docling_is_available_marker() -> None:
    """The skipif marker above assumes the extra is installed. If the marker
    is enabled but the package is missing we fail loudly rather than silently
    passing.
    """

    assert docling_is_available(), (
        "BIO_PDF_LIVE_TESTS=1 but docling is not importable. "
        "Install with `uv sync --extra docling`."
    )


def test_normalise_docling_fixture_round_trips(tmp_path: Path) -> None:
    """The offline Docling fixture must produce a usable document."""

    fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "docling" / "fixture_page_one.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    doc = normalise_docling_file(
        fixture_path,
        parser_run_id="run-fixture",
        document_id="sha256:fixture",
    )
    assert len(doc.pages) == 1
    assert any("SEQ ID" in b.text_raw for b in doc.pages[0].blocks)


def test_docling_adapter_runs_on_blank_pdf(tmp_path: Path) -> None:
    pdf_path = _build_pdf(tmp_path / "blank.pdf", pages=1)
    request = ParseRequest(
        job_id="job-live",
        document_id="sha256:live",
        source_path=pdf_path,
        page_numbers=(1,),
        profile="layout_native",
        options_ref="options.json",
        output_dir=tmp_path / "artifacts",
    )
    adapter = DoclingAdapter()
    artifacts = adapter.parse(request)
    assert artifacts.normalized_path.exists()
    assert artifacts.manifest_path.exists()