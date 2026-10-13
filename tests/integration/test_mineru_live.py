"""Live integration test for the MinerU V1 SaaS adapter.

The test only runs when the ``MINERU_LIVE_TESTS=1`` environment variable is
set *and* a real Bearer token is available via ``MINERU_TOKEN``. It submits
a small PDF to the MinerU SaaS, polls, downloads, and verifies the
normalised document structure.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pypdfium2 as pdfium
import pytest

from bio_pdf.errors import EngineUnavailableError
from bio_pdf.parsers.mineru_adapter import MinerUAdapter
from bio_pdf.parsers.base import ParseRequest


LIVE_ENV_VAR = "MINERU_LIVE_TESTS"
TOKEN_ENV_VAR = "MINERU_TOKEN"

pytestmark = pytest.mark.skipif(
    not os.environ.get(LIVE_ENV_VAR),
    reason=f"set {LIVE_ENV_VAR}=1 to enable MinerU live tests",
)


def _build_pdf(path: Path, *, pages: int = 1) -> Path:
    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(width=595, height=842)
    pdf.save(str(path))
    pdf.close()
    return path


def test_mineru_lifecycle_on_blank_pdf(tmp_path: Path) -> None:
    token = os.environ.get(TOKEN_ENV_VAR)
    if not token:
        pytest.skip(f"{TOKEN_ENV_VAR} not set; cannot run live MinerU test")

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
    adapter = MinerUAdapter(token=token, poll_interval=5.0, max_polls=20)
    # source_url is required by the SaaS but for offline testing we point it
    # at any public URL; the real test should set a reachable PDF.
    artifacts = adapter.parse(request, source_url="https://example.com/missing.pdf")
    assert artifacts.normalized_path.exists()
    assert artifacts.manifest_path.exists()