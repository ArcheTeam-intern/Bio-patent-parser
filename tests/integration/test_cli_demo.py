"""CLI demo tests (doctor, inventory, parse)."""

from __future__ import annotations

import json
from pathlib import Path

import pypdfium2 as pdfium
from typer.testing import CliRunner

from bio_pdf.cli import app

runner = CliRunner()


def _blank_pdf(path: Path, *, pages: int = 2) -> Path:
    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(width=595, height=842)
    pdf.save(str(path))
    pdf.close()
    return path


def test_parse_native_produces_artifacts(tmp_path: Path) -> None:
    pdf = _blank_pdf(tmp_path / "blank.pdf", pages=2)
    out = tmp_path / "out"

    result = runner.invoke(app, ["parse", "--input", str(pdf), "--pages", "1", "--out", str(out)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["engine"] == "native"
    assert (out / "normalized.json").exists()
    assert (out / "manifest.json").exists()


def test_parse_rejects_html_disguised_as_pdf(tmp_path: Path) -> None:
    fake = tmp_path / "not.pdf"
    fake.write_text("<!DOCTYPE html>", encoding="utf-8")
    result = runner.invoke(app, ["parse", "--input", str(fake)])
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["error"] == "INPUT_NOT_PDF"


def test_parse_invalid_page_range(tmp_path: Path) -> None:
    pdf = _blank_pdf(tmp_path / "blank.pdf", pages=2)
    result = runner.invoke(app, ["parse", "--input", str(pdf), "--pages", "99"])
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["error"] == "PAGE_RANGE_INVALID"


def test_doctor_emits_readiness(tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["model_readiness"] == "not_checked"
    assert payload["modules"]["pypdfium2"] is True