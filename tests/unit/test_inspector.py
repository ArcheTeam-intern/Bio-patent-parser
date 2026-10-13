"""Inspector and inventory regression tests.

These tests intentionally do NOT depend on any model/OCR. The minimal
PDF fixtures are constructed in-memory by pypdfium2 itself.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pypdfium2 as pdfium
import pytest

from bio_pdf.errors import (
    PageRangeInvalidError,
    WorkflowError,
)
from bio_pdf.ingest.inspector import (
    build_inventory_report,
    inspect_directory,
    inspect_pdf,
    parse_page_selection,
)


def _make_blank_pdf(path: Path, *, pages: int = 1) -> Path:
    """Write a minimal blank PDF with ``pages`` pages using pypdfium2."""

    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(width=595, height=842)
    pdf.save(str(path))
    pdf.close()
    return path


# ---------------------------------------------------------------------------
# inspect_pdf
# ---------------------------------------------------------------------------


def test_html_disguised_as_pdf_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "patent.pdf"
    document = "<!DOCTYPE html><title>download page</title>"
    source.write_text(document, encoding="utf-8")
    with pytest.raises(WorkflowError) as caught:
        inspect_pdf(source)
    assert caught.value.code == "INPUT_NOT_PDF"


def test_blank_pdf_is_accepted(tmp_path: Path) -> None:
    source = _make_blank_pdf(tmp_path / "blank.pdf", pages=2)
    inspection = inspect_pdf(source)
    assert inspection.page_count == 2
    assert inspection.is_encrypted is False
    assert inspection.document_id.startswith("sha256:")
    assert len(inspection.pages) == 2
    assert inspection.pages[0].width_pt == pytest.approx(595.0)
    assert inspection.pages[0].height_pt == pytest.approx(842.0)


def test_corrupt_pdf_raises_input_corrupt(tmp_path: Path) -> None:
    source = tmp_path / "broken.pdf"
    source.write_bytes(b"%PDF-1.4\n%broken stream\n")
    with pytest.raises(WorkflowError) as caught:
        inspect_pdf(source)
    # pypdfium2 may report the file as non-PDF if it cannot read objects.
    assert caught.value.code in {"INPUT_CORRUPT", "INPUT_NOT_PDF"}


def test_sha256_matches_independent_hash(tmp_path: Path) -> None:
    source = _make_blank_pdf(tmp_path / "blank.pdf")
    inspection = inspect_pdf(source)
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    assert inspection.sha256 == expected


def test_non_existent_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(WorkflowError) as caught:
        inspect_pdf(tmp_path / "missing.pdf")
    assert caught.value.code == "INPUT_NOT_PDF"


def test_directory_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(WorkflowError) as caught:
        inspect_pdf(tmp_path)
    assert caught.value.code == "INPUT_NOT_PDF"


# ---------------------------------------------------------------------------
# parse_page_selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("selection", "page_count", "expected"),
    [
        ("all", 3, (1, 2, 3)),
        ("", 3, (1, 2, 3)),
        ("1", 10, (1,)),
        ("1,3", 10, (1, 3)),
        ("3,1", 10, (1, 3)),  # sorted, deduped
        ("1-3", 10, (1, 2, 3)),
        ("1-3,7", 10, (1, 2, 3, 7)),
        ("1-3,2,4", 10, (1, 2, 3, 4)),
    ],
)
def test_parse_page_selection_valid(
    selection: str, page_count: int, expected: tuple[int, ...]
) -> None:
    assert parse_page_selection(selection, page_count=page_count) == expected


@pytest.mark.parametrize(
    "selection",
    [
        "0",
        "-1",
        "11",  # out of range for page_count=10
        "3-2",
        "abc",
        "1,,2",
        "1-2-3",
    ],
)
def test_parse_page_selection_rejects_invalid(selection: str) -> None:
    with pytest.raises(PageRangeInvalidError):
        parse_page_selection(selection, page_count=10)


def test_parse_page_selection_zero_page_count() -> None:
    with pytest.raises(PageRangeInvalidError):
        parse_page_selection("1", page_count=0)


# ---------------------------------------------------------------------------
# inspect_directory + build_inventory_report
# ---------------------------------------------------------------------------


def test_inspect_directory_does_not_modify_files(tmp_path: Path) -> None:
    pdf_dir = tmp_path / "corpus"
    pdf_dir.mkdir()
    pdf_path = _make_blank_pdf(pdf_dir / "ok.pdf")
    html_path = pdf_dir / "not_pdf.pdf"
    html_path.write_text("<!DOCTYPE html>", encoding="utf-8")

    before = {p.name: p.stat().st_mtime_ns for p in pdf_dir.iterdir()}
    records = inspect_directory(pdf_dir)
    after = {p.name: p.stat().st_mtime_ns for p in pdf_dir.iterdir()}

    assert before == after
    statuses = {Path(record["path"]).name: record["status"] for record in records}
    assert statuses["ok.pdf"] == "ok"
    assert statuses["not_pdf.pdf"] == "error"


def test_build_inventory_report_counts_outcomes(tmp_path: Path) -> None:
    pdf_dir = tmp_path / "corpus"
    pdf_dir.mkdir()
    _make_blank_pdf(pdf_dir / "a.pdf")
    (pdf_dir / "b.pdf").write_text("<html></html>", encoding="utf-8")

    records = inspect_directory(pdf_dir)
    report = build_inventory_report(records)
    assert report["count"] == 2
    assert report["summary"]["ok"] == 1
    assert report["summary"]["INPUT_NOT_PDF"] == 1


def test_inspect_directory_against_real_corpus_signature_audit(tmp_path: Path) -> None:
    """Smoke check against the project PDF corpus. We do not assert exact
    counts because the corpus may grow; we assert that all currently
    signature-matching files succeed and the previously failing HTML files keep
    their known error code."""

    repo_root = Path(__file__).resolve().parents[2]
    pdf_root = repo_root / "pdf"
    if not pdf_root.exists():
        pytest.skip(f"real corpus directory not found: {pdf_root}")
    records = inspect_directory(pdf_root)
    by_name = {Path(rec["path"]).name: rec for rec in records}
    known_good = {
        "paper_TAT-RasGAP_broad_antimicrobial_2017.pdf",
        "patent_CN114144196B_engineered_vaccinia_zh.pdf",
        "patent_US20240026005A1_anti_PD-L2_antibodies.pdf",
        "patent_US5459060A_HIV_gp41_antibodies.pdf",
        "patent_WO2017060493A1_polypeptides.pdf",
        "patent_WO2020156367A1_rice_RAY1_zh.pdf",
        "patent_WO2021207200A2_engineered_transaminase.pdf",
    }
    known_bad = {
        "patent_EP3027649A1_TNFa_IL17_bispecific_antibodies.pdf",
        "patent_EP3423477A1_antimicrobial_peptides.pdf",
        "probe.pdf",
    }
    for name in known_good:
        if name in by_name:
            assert by_name[name]["status"] == "ok", f"{name} should parse but got {by_name[name]}"
    for name in known_bad:
        if name in by_name:
            assert by_name[name]["status"] == "error", f"{name} should be rejected"
            assert by_name[name]["error_code"] in {"INPUT_NOT_PDF", "INPUT_CORRUPT"}


def test_inspect_directory_writes_no_files(tmp_path: Path) -> None:
    """The inventory must not write any side files (caches, locks, …)."""

    pdf_dir = tmp_path / "corpus"
    pdf_dir.mkdir()
    _make_blank_pdf(pdf_dir / "a.pdf")
    files_before = sorted(p.name for p in tmp_path.iterdir() if p.is_file())
    inspect_directory(pdf_dir)
    files_after = sorted(p.name for p in tmp_path.iterdir() if p.is_file())
    assert files_before == files_after