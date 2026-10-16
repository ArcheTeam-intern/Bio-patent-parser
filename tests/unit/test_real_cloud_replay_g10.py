"""G10+G11: replay the REAL MinerU cloud content_list artifacts.

These fixtures are the actual cloud responses the reviewer obtained on
2026-10-16/17 (credential authorized, sanitized of auth headers and signed
URLs). Replaying them through the production projection/classification
chain turns the visual spot-checks into deterministic regressions:

- EP 30–33: SEQ 1 ``SYGVH`` was lost to the ``50 <400> 1`` margin prefix —
  after G06 it must be recovered from the SAME bytes.
- Paper 8–9: TABLE 3 (70 S/R/I td-only cells) must classify as a
  susceptibility matrix (G11) while TABLE 4 (scoring) stays at zero, and
  the malformed page-8 grid (4588 cells) must be a VISIBLE structural
  issue, never a silently extracted healthy matrix.
"""

from __future__ import annotations

import json
from pathlib import Path

from bio_pdf.application.comparison_service import _domain_pass_spec
from bio_pdf.parsers.mineru_adapter import _project_mineru_payload
from bio_pdf.parsers.normalize_docling import normalize_docling_document

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "real_cloud"


def _replay(name: str, *, page_size=(595.0, 842.0, 0)):
    items = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    page_order = sorted({int(i.get("page_idx") or 0) for i in items if isinstance(i, dict)})
    page_order = [p + 1 for p in page_order]
    page_sizes = {p: page_size for p in page_order}
    projected = _project_mineru_payload(
        {"content_list": items},
        page_numbers=set(page_order),
        page_order=page_order,
        page_sizes=page_sizes,
    )
    projected.pop("_projection_warnings", None)
    projected.pop("_coordinate_basis", None)
    document = normalize_docling_document(projected, parser_run_id="run", document_id="doc")
    summary = _domain_pass_spec(
        json.loads(document.model_dump_json()), document_id="doc", engine="mineru_http"
    )
    return document, summary


def test_real_ep_30_33_replay_recovers_seq1_through_the_margin_lexer() -> None:
    document, summary = _replay("mineru_EP_30_33_content_list.json")
    sequences = {str(s["seq_id_no"]): s["normalized_sequence"] for s in summary["sequences"]}
    # The reviewer's control experiment: removing the margin counts made
    # SEQ 1 appear as SYGVH. The lexer fix must do it on the raw bytes.
    assert sequences.get("1") == "SYGVH", sequences
    assert len(sequences) >= 8, sorted(sequences)


def test_real_paper_table3_is_classified_susceptible_but_header_semantics_broken() -> None:
    """The REAL page-9 TABLE 3 (129 td, 70 S/R/I, no th): it must NOT be
    silently classified as an inventory. Its rows are actually uniform in
    width (H04: the old per-cell check falsely blamed raggedness), but the
    header row itself contains S/R/I VALUES — pairing values under an "R"
    header would fabricate drug→strain relations, so the table carries a
    VISIBLE header-semantics issue and its extraction is refused."""

    from bio_pdf.extract.activities.table_classifier import TableKind, classify_table

    document, summary = _replay("mineru_paper_8_9_content_list.json")
    page9 = next(p for p in document.pages if p.page_number == 2)
    suspect = next(
        t for t in page9.tables if "HEADER_SEMANTICS_BROKEN" in " ".join(t.structure_issues)
    )
    # H04: a consistent row-width grid must NOT carry a raggedness issue.
    assert not any(
        "RAGGED" in issue for issue in suspect.structure_issues
    ), suspect.structure_issues
    assert classify_table(suspect) is TableKind.SUSCEPTIBILITY, (
        "a td-only S/R/I matrix is never an inventory"
    )
    assert all(
        m["metric"] != "antimicrobial_susceptibility" for m in summary["measurements"]
    ), "a header-mangled grid must not be converted into fabricated activities"

    # TABLE 4 (scoring) stays zero — its values are not S/R/I.
    assert all(
        m["value_raw"] not in ("0", "1", "2", "3")
        for m in summary["measurements"]
    )


def test_clean_td_only_susceptibility_matrix_extracts_with_axes() -> None:
    """Positive case: a td-only matrix with a CONSISTENT grid must classify
    as susceptibility and produce drug→strain S/R/I measurements with
    evidence — the acceptance's "正确的药物—菌株—S/R/I 关系有证据"."""

    from bio_pdf.application.comparison_service import _domain_pass_spec
    from bio_pdf.parsers.mineru_adapter import _project_mineru_payload
    from bio_pdf.parsers.normalize_docling import normalize_docling_document

    html = (
        "<table>"
        "<tr><td>Species</td><td>Isolate number</td><td>Vancomycin</td><td>Colistin</td></tr>"
        "<tr><td>Acinetobacter baumannii</td><td>1701</td><td>S</td><td>R</td></tr>"
        "<tr><td>Pseudomonas aeruginosa</td><td>1702</td><td>R</td><td>I</td></tr>"
        "<tr><td>Klebsiella pneumoniae</td><td>1703</td><td>I</td><td>S</td></tr>"
        "</table>"
    )
    items = [{"type": "table", "page_idx": 0, "bbox": [100, 100, 900, 500], "table_body": html}]
    projected = _project_mineru_payload(
        {"content_list": items}, page_numbers={1}, page_order=[1], page_sizes={1: (595.0, 842.0, 0)}
    )
    projected.pop("_projection_warnings", None)
    projected.pop("_coordinate_basis", None)
    document = normalize_docling_document(projected, parser_run_id="run", document_id="doc")
    summary = _domain_pass_spec(
        json.loads(document.model_dump_json()), document_id="doc", engine="mineru_http"
    )
    sri = [m for m in summary["measurements"] if m["metric"] == "antimicrobial_susceptibility"]
    assert len(sri) == 6, sri
    pairs = {(m["entity"], m["target"], m["value_raw"]) for m in sri}
    assert ("Vancomycin", "Acinetobacter baumannii", "S") in pairs
    assert ("Colistin", "Klebsiella pneumoniae", "S") in pairs


def test_real_paper_page8_malformed_grid_is_a_visible_issue_not_health() -> None:
    document, summary = _replay("mineru_paper_8_9_content_list.json")
    page8 = next(p for p in document.pages if p.page_number == 1)
    assert page8.tables, "the malformed table is still preserved for audit"
    issues = " ".join(
        str(i) for t in page8.tables for i in t.structure_issues
    )
    assert "GRID_SIZE_SUSPECT" in issues, page8.tables[0].structure_issues
    # And the comparison chain skipped it (no measurements from that grid).
    assert summary["measurements"] == [] or all(
        m["page_number"] != 1 for m in summary["measurements"]
    )


def test_real_ep_boxes_convert_into_the_page_frame() -> None:
    """G09 on real bytes: every EP content_list box converts to page points
    inside 595x842 and the converted frame keeps the real page relationships
    (e.g. header text near the top)."""

    items = json.loads((FIXTURES / "mineru_EP_30_33_content_list.json").read_text(encoding="utf-8"))
    projected = _project_mineru_payload(
        {"content_list": items},
        page_numbers={1, 2, 3, 4},
        page_order=[1, 2, 3, 4],
        page_sizes={p: (595.0, 842.0, 0) for p in (1, 2, 3, 4)},
    )
    projected.pop("_projection_warnings", None)
    projected.pop("_coordinate_basis", None)
    document = normalize_docling_document(projected, parser_run_id="run", document_id="doc")
    boxes = [b.bbox for p in document.pages for b in p.blocks if b.bbox]
    assert boxes
    for x0, y0, x1, y1 in boxes:
        assert -2 <= x0 and x1 <= 597 and -2 <= y0 and y1 <= 844, (x0, y0, x1, y1)
    # Conversion (not raw 0–1000) is proven by the values being small in
    # page points, e.g. every width < 600 works but a raw 0-1000 box would
    # routinely exceed 595.
    assert all(b[2] <= 597 for b in boxes)
    assert any(b[1] < 100 for b in boxes), "headers sit near the page top in pt"
