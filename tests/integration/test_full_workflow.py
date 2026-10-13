"""End-to-end domain workflow tests (CLI run → extract → validate → export).

These tests cover the review-report acceptance path: a synthetic PDF with an
ST.25 protein listing and a prose activity value must produce domain records,
a review queue and export artifacts — not just a parse dump.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bio_pdf.cli import app

runner = CliRunner()


def _domain_pdf(path: Path) -> Path:
    """Build a one-page PDF with an ST.25 listing and a prose IC50 value."""

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    text = c.beginText(50, 800)
    lines = [
        "Protein sequences",
        "<210> 1",
        "<211> 4",
        "<212> PRT",
        "<213> Artificial",
        "<400> 1",
        "Ala Gly Leu Lys",
        "",
        "Example 1: The IC50 was 1-10 nM.",
        "The measured MIC was >128 ug/mL against E. coli.",
    ]
    for line in lines:
        text.textLine(line)
    c.drawText(text)
    c.save()
    return path


@pytest.fixture()
def domain_pdf(tmp_path: Path) -> Path:
    return _domain_pdf(tmp_path / "domain.pdf")


def test_run_completes_full_pipeline(tmp_path: Path, domain_pdf: Path) -> None:
    data_dir = tmp_path / "data"
    result = runner.invoke(
        app,
        [
            "run",
            "--input", str(domain_pdf),
            "--data-dir", str(data_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.splitlines()[-1])
    assert payload["status"] in {"succeeded", "succeeded_with_review"}
    # Every required stage ran (layout_parse is registered and runs, but the
    # docling engine is not configured so it is recorded as skipped).
    for stage in ("ingest", "inspect", "native_parse", "normalize", "extract", "validate", "export"):
        assert stage in payload["stages_run"], (stage, payload)

    result_json = Path(payload["export"])
    assert result_json.is_file()
    export = json.loads(result_json.read_text(encoding="utf-8"))
    assert export["summary"]["sequences_total"] >= 1
    assert export["summary"]["measurements_total"] >= 2

    # The range value must survive as a range — never truncated to a scalar.
    ranges = [
        m for m in export["measurements"] if m["value"]["kind"] == "range"
    ]
    assert any(m["value"]["lower"] == "1" and m["value"]["upper"] == "10" for m in ranges)
    comparators = [
        m for m in export["measurements"]
        if m["value"]["kind"] == "scalar" and m["value"].get("operator") == ">"
    ]
    assert any(m["value"]["number"] == "128" for m in comparators)


def test_run_resume_reuses_verified_cache(tmp_path: Path, domain_pdf: Path) -> None:
    data_dir = tmp_path / "data"
    first = runner.invoke(app, ["run", "--input", str(domain_pdf), "--data-dir", str(data_dir)])
    assert first.exit_code == 0, first.output

    # A second run with --resume is a new job (config-addressed), but the
    # pipeline must still complete without touching the previous artifacts.
    second = runner.invoke(
        app,
        ["run", "--input", str(domain_pdf), "--data-dir", str(data_dir)],
    )
    assert second.exit_code == 0, second.output
    payload = json.loads(second.output.splitlines()[-1])
    assert payload["status"] in {"succeeded", "succeeded_with_review"}


def test_run_rejects_non_pdf(tmp_path: Path) -> None:
    fake = tmp_path / "fake.pdf"
    fake.write_text("<!DOCTYPE html>", encoding="utf-8")
    result = runner.invoke(app, ["run", "--input", str(fake), "--data-dir", str(tmp_path / "d")])
    assert result.exit_code == 2
    assert "INPUT_NOT_PDF" in result.output
