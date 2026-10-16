"""H04/P01: the structural quality gate must judge ROW widths, not cells.

The G-round check collected every cell's end column — a regular 2x6
rectangle produced ends {1..6} and was flagged RAGGED_ROWS_SEVERE,
dropping its legitimate activities (the reviewer's 5-activity case).
These tests lock: regular rectangles of any width produce NO raggedness
issue (mild or severe) and extract every value; genuinely distorted grids
(the real 12x392 and severe handcrafted raggedness) stay refused.
"""

from __future__ import annotations

import json

from bio_pdf.application.comparison_service import _domain_pass_spec
from bio_pdf.parsers.mineru_adapter import _project_mineru_payload
from bio_pdf.parsers.normalize_docling import normalize_docling_document


def _replay_html(html: str):
    items = [{"type": "table", "page_idx": 0, "bbox": [100, 100, 900, 600], "table_body": html}]
    projected = _project_mineru_payload(
        {"content_list": items},
        page_numbers={1},
        page_order=[1],
        page_sizes={1: (595.0, 842.0, 0)},
    )
    projected.pop("_projection_warnings", None)
    projected.pop("_coordinate_basis", None)
    document = normalize_docling_document(projected, parser_run_id="run", document_id="doc")
    summary = _domain_pass_spec(
        json.loads(document.model_dump_json()), document_id="doc", engine="mineru_http"
    )
    return document, summary


def _susceptibility_html(*, species_rows: int, drug_count: int) -> str:
    drugs = [f"Drug{i}" for i in range(drug_count)]
    rows = ["<tr><td>Species</td>" + "".join(f"<td>{d}</td>" for d in drugs) + "</tr>"]
    for r in range(species_rows):
        values = ["S", "R", "I"]
        rows.append(
            "<tr><td>Strain{}</td>".format(r)
            + "".join(f"<td>{values[(r + c) % 3]}</td>" for c in range(drug_count))
            + "</tr>"
        )
    return "<table>" + "".join(rows) + "</table>"


def _issues(document) -> list[str]:
    return [
        str(i)
        for page in document.pages
        for table in page.tables
        for i in table.structure_issues
    ]


def test_reviewers_five_activity_case_no_longer_dropped() -> None:
    """The exact repro: a 2x6 td-only table (Drug + 5 strains, Vancomycin +
    5 S values) — 5 activities, zero raggedness issues of any level."""

    html = (
        "<table>"
        "<tr><td>Drug</td><td>S1</td><td>S2</td><td>S3</td><td>S4</td><td>S5</td></tr>"
        "<tr><td>Vancomycin</td><td>S</td><td>S</td><td>S</td><td>S</td><td>S</td></tr>"
        "</table>"
    )
    document, summary = _replay_html(html)
    assert not [i for i in _issues(document) if "RAGGED" in i], _issues(document)
    assert not [i for i in _issues(document) if "SEVERE" in i], _issues(document)
    sri = [m for m in summary["measurements"] if m["metric"] == "antimicrobial_susceptibility"]
    assert len(sri) == 5, sri
    assert {m["entity"] for m in sri} == {"Vancomycin"}
    assert {m["target"] for m in sri} == {"S1", "S2", "S3", "S4", "S5"}


def test_regular_rectangles_of_any_width_have_no_warnings() -> None:
    for cols in (5, 6, 10, 20):
        document, summary = _replay_html(_susceptibility_html(species_rows=3, drug_count=cols - 1))
        assert not [i for i in _issues(document) if "RAGGED" in i], (cols, _issues(document))
        sri = [
            m for m in summary["measurements"] if m["metric"] == "antimicrobial_susceptibility"
        ]
        assert len(sri) == (cols - 1) * 3, (cols, len(sri))


def test_severely_ragged_grid_still_refused() -> None:
    """Real distortion (rows of 6, 2 and 1 wide) keeps the severe issue and
    produces zero activities — the gate was corrected, not disabled."""

    html = (
        "<table>"
        "<tr><td>Drug</td><td>S1</td><td>S2</td><td>S3</td><td>S4</td><td>S5</td></tr>"
        "<tr><td>Vancomycin</td><td>S</td></tr>"
        "<tr><td>S</td></tr>"
        "</table>"
    )
    document, summary = _replay_html(html)
    assert [i for i in _issues(document) if "RAGGED_ROWS_SEVERE" in i], _issues(document)
    assert not [
        m for m in summary["measurements"] if m["metric"] == "antimicrobial_susceptibility"
    ]


def test_header_row_containing_values_is_broken_and_refused() -> None:
    """S/R/I values inside the header row (the real TABLE 3 shape) — the
    axes cannot be trusted; visible issue, zero fabricated activities."""

    html = (
        "<table>"
        "<tr><td>Species</td><td>Drug1</td><td>R</td><td>S</td></tr>"
        "<tr><td>StrainA</td><td>S</td><td>R</td><td>I</td></tr>"
        "</table>"
    )
    document, summary = _replay_html(html)
    assert [i for i in _issues(document) if "HEADER_SEMANTICS_BROKEN" in i], _issues(document)
    assert not [
        m for m in summary["measurements"] if m["metric"] == "antimicrobial_susceptibility"
    ]


def test_rowspan_coverage_does_not_count_as_short_rows() -> None:
    """A merged header spanning the whole grid width must not make its
    covered rows look short (span coverage counts toward their width)."""

    html = (
        "<table>"
        "<tr><td colspan='6'>Combined header</td></tr>"
        "<tr><td>Drug</td><td>S1</td><td>S2</td><td>S3</td><td>S4</td><td>S5</td></tr>"
        "<tr><td>Vancomycin</td><td>S</td><td>R</td><td>S</td><td>I</td><td>S</td></tr>"
        "</table>"
    )
    document, summary = _replay_html(html)
    assert not [i for i in _issues(document) if "RAGGED" in i], _issues(document)
    sri = [m for m in summary["measurements"] if m["metric"] == "antimicrobial_susceptibility"]
    assert len(sri) == 5, sri
