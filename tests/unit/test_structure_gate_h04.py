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


def _replay_html(html: str, caption: str | None = None):
    item = {"type": "table", "page_idx": 0, "bbox": [100, 100, 900, 600], "table_body": html}
    if caption:
        item["table_caption"] = [caption]
    items = [item]
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


def test_organism_label_row_does_not_make_the_grid_ragged() -> None:
    """Live Table 4a/4c: MinerU writes the organism in a row of its OWN
    (label + empty placeholders) and its strains' values in the following
    rows (empty cell in column 0, values from column 1). The label row is as
    wide as the block it labels — otherwise a perfect 10-organism grid read
    as "row widths range 1..5" and the severe gate refused every value."""

    rows = [
        "<tr><td>Bacteria*</td><td>Strains</td><td>Chain104</td><td>Chain105</td></tr>",
        "<tr><td>Escherichia coli</td><td></td><td></td><td></td></tr>",
        "<tr><td></td><td>1</td><td>4</td><td>1</td></tr>",
        "<tr><td></td><td>2</td><td>4</td><td>4</td></tr>",
        "<tr><td>Klebsiella pneumoniae</td><td></td><td></td><td></td></tr>",
        "<tr><td></td><td>1</td><td>8</td><td>4</td></tr>",
        "<tr><td></td><td>2</td><td>4</td><td>2</td></tr>",
    ]
    document, summary = _replay_html(
        "<table>" + "".join(rows) + "</table>",
        caption="Table 4a: Minimum inhibitory concentration (MIC) of peptides",
    )
    assert not [i for i in _issues(document) if "RAGGED" in i], _issues(document)
    sri = [m for m in summary["measurements"] if m["metric"] == "MIC"]
    # Two peptides x two strains per organism — the leading "Strains" number
    # column is a row index, not a peptide, and must never become a value.
    assert len(sri) == 8, sri
    assert {m["entity"] for m in sri} == {"Chain104", "Chain105"}
    assert {m["target"] for m in sri} == {"Escherichia coli", "Klebsiella pneumoniae"}
    by_key = {(m["target"], m["entity"]): m["value_raw"] for m in sri}
    assert by_key[("Escherichia coli", "Chain104")] == "4"
    assert by_key[("Klebsiella pneumoniae", "Chain105")] == "2"


def test_full_width_group_banner_names_the_rows_below_it() -> None:
    """MinerU writes the last organism of live Table 4a as a FULL-WIDTH row
    (``<td colspan=5>Acinetobacter johnsonii</td>``) instead of a left label.
    That banner is the block's label: no overlap may be reported, and the
    organism must reach the values underneath (never the other way round —
    read as a merged column header it became their TARGET)."""

    rows = [
        "<tr><td>Bacteria*</td><td>Strains</td><td>Chain104</td><td>Chain105</td></tr>",
        "<tr><td>Escherichia coli</td><td></td><td></td><td></td></tr>",
        "<tr><td></td><td>1</td><td>4</td><td>1</td></tr>",
        "<tr><td colspan='4'>Klebsiella pneumoniae</td></tr>",
        "<tr><td></td><td>1</td><td>8</td><td>4</td></tr>",
    ]
    document, summary = _replay_html(
        "<table>" + "".join(rows) + "</table>",
        caption="Table 4a: Minimum inhibitory concentration (MIC) of peptides",
    )
    assert not [i for i in _issues(document) if "OVERLAP" in i], _issues(document)
    sri = [m for m in summary["measurements"] if m["metric"] == "MIC"]
    assert len(sri) == 4, sri
    by_target = {(m["target"], m["entity"]): m["value_raw"] for m in sri}
    assert by_target[("Klebsiella pneumoniae", "Chain104")] == "8"
    assert by_target[("Escherichia coli", "Chain104")] == "4"


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
