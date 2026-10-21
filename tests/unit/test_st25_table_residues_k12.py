"""K12: residue rows that a layout parser emitted as a TABLE, plus the
adjacent listing defects the live EP3423477B1 run exposed.

- Seq 1/2/3 printed their FIRST residue row on the ``<400>`` line itself
  (``<400> 2 catatgagac …``); the old lexer kept only the seq number and
  reported the listings as zero-residue "missing".
- Page 31 was one 16x16 grid of three-letter codes with NO text lines, so
  ST.25 stopped at 348/549; the page's evidence anchored on the footer "31".
- A residue code can carry its position (``Glu180``), which used to hide the
  three-letter form entirely.
- Table 4b's frame ended at 0.893 of the page height — under the "0.92 =
  touches the bottom" test — while its page-24 rows sat inside page 23's
  table_body, and the page's running header was used as the table's title.
"""

from __future__ import annotations

from bio_pdf.contracts import NormalizedDocument, NormalizedPage, Table, TableCell
from bio_pdf.extract.activities.table_classifier import TableKind, classify_table
from bio_pdf.extract.evidence import EvidenceIndex
from bio_pdf.extract.sequences.residue_tables import (
    is_residue_table,
    residue_table_blocks,
)
from bio_pdf.extract.sequences.st25 import (
    _St25StateMachine,
    parse_st25_blocks_with_scan,
)
from bio_pdf.parsers.mineru_adapter import (
    _demote_running_header_captions,
    _mark_cross_page_continuations,
)

_CODES = [
    "Glu", "Leu", "Lys", "Val", "Ala", "Gly",
    "Ser", "Thr", "Pro", "Arg", "Asp",
]


def _feed(lines: list[str], page: int = 32) -> _St25StateMachine:
    machine = _St25StateMachine(document_id="sha256:k12")
    for line in lines:
        machine.feed_line(line, page_number=page)
    machine.finish()
    return machine


def _cell(row: int, column: int, text: str, *, table_id: str = "31:t0") -> TableCell:
    return TableCell(
        cell_id=f"{table_id}:c{row}_{column}",
        row_index=row,
        column_index=column,
        text_raw=text,
        header_role="data",
    )


def _residue_table(*, table_id: str = "31:t0", page: int = 31) -> Table:
    cells: list[TableCell] = []
    for row in range(4):
        cells.append(_cell(row, 0, str(row * len(_CODES) + 1), table_id=table_id))
        for column, code in enumerate(_CODES, start=1):
            cells.append(_cell(row, column, code, table_id=table_id))
    return Table(
        table_id=table_id,
        page_number=page,
        row_count=4,
        column_count=len(_CODES) + 1,
        cells=cells,
        bbox=(58.9, 66.5, 500.0, 733.4),
    )


def _block(text: str, page: int, bbox: list[float]) -> dict:
    return {
        "text": text,
        "kind": "text",
        "semantic_role": "unknown",
        "page_number": page,
        "bbox": bbox,
        "block_id": f"p{page}:{int(bbox[1])}",
        "source_refs": [],
        "source_aliases": [],
        "source_parser": "native",
        "source_artifact_id": None,
    }


# ---- <400> lines that carry the first residue row -------------------------


def test_inline_residues_on_the_400_line_are_parsed() -> None:
    machine = _feed(
        ["<210> 2", "<211> 10", "<212> DNA", "<400> 2 catgacgtac"]
    )
    assert len(machine.records) == 1
    record = machine.records[0]
    assert record.normalized_sequence == "CATGACGTAC"
    assert record.observed_length == 10
    assert record.is_complete is True
    # The row is listing CONTENT, not a seq-number-only line.
    assert "catgacgtac" in record.raw_sequence


def test_prose_after_400_is_not_swallowed_as_residues() -> None:
    machine = _feed(["<210> 3", "<211> 2", "<212> PRT", "<400> 3 Ser Tyr"])
    assert machine.records[0].normalized_sequence == "SY"
    prose = _feed(
        ["<210> 4", "<211> 2", "<212> PRT", "<400> 4 as described below."]
    )
    assert prose.records == []
    assert prose.zero_residue_listings == [("4", 32)]


# ---- position-suffixed three-letter codes --------------------------------


def test_position_suffixed_code_maps_and_keeps_three_letter_form() -> None:
    machine = _feed(
        ["<210> 1", "<211> 2", "<212> PRT", "<400> 1 Glu180 Leu20"]
    )
    record = machine.records[0]
    assert record.normalized_sequence == "EL"
    assert record.representation == "three_letter"
    assert record.is_complete is True


# ---- residue tables become TEXT lines ------------------------------------


def test_residue_table_becomes_reading_order_lines() -> None:
    table = _residue_table()
    assert is_residue_table(table) is True

    document = NormalizedDocument(
        schema_version="1.0",
        document_id="sha256:k12",
        parser_run_id="run",
        pages=[
            NormalizedPage(
                page_number=31, width_pt=595.0, height_pt=842.0, tables=[table]
            )
        ],
    )
    blocks = residue_table_blocks(
        document, parser="native", source_artifact_id="sha256:k12"
    )
    assert [b["block_id"] for b in blocks] == [
        "31:t0:r0",
        "31:t0:r1",
        "31:t0:r2",
        "31:t0:r3",
    ]
    assert blocks[0]["text"] == "1 " + " ".join(_CODES)
    # Geometry is the TABLE's own box — no per-row coordinates are invented.
    assert list(blocks[0]["bbox"]) == [58.9, 66.5, 500.0, 733.4]
    assert blocks[0]["page_number"] == 31
    assert blocks[0]["source_refs"], "each row cites its cells"


def test_residue_table_recovers_a_complete_sequence_and_anchors_evidence() -> None:
    table = _residue_table()
    document = NormalizedDocument(
        schema_version="1.0",
        document_id="sha256:k12",
        parser_run_id="run",
        pages=[
            NormalizedPage(
                page_number=31, width_pt=595.0, height_pt=842.0, tables=[table]
            )
        ],
    )
    rows = residue_table_blocks(document, parser="native")
    declaration_box = [84.5, 700.0, 300.0, 760.0]
    blocks = [
        _block("<210> 1\n<211> 44\n<212> PRT\n<400> 1", 30, declaration_box),
        *rows,
    ]
    index = EvidenceIndex()
    records, _scan = parse_st25_blocks_with_scan(
        blocks, "sha256:k12", parser_run_id="run", evidence_index=index
    )
    assert len(records) == 1
    record = records[0]
    assert record.observed_length == 44
    assert record.declared_length == 44
    assert record.is_complete is True
    assert record.representation == "three_letter"

    spans = {s["page_number"]: s for s in index.to_payload()}
    assert set(spans) == {30, 31}
    assert spans[31]["geometry_precision"] == "block"
    assert [round(v, 1) for v in spans[31]["bbox"]] == [58.9, 66.5, 500.0, 733.4]
    assert spans[31]["raw_text"].startswith("1 Glu Leu")
    # The declaration page keeps the <210> block, not the whole page.
    assert [round(v, 1) for v in spans[30]["bbox"]] == declaration_box


def test_activity_matrix_is_never_reinterpreted_as_residues() -> None:
    cells = []
    for row in range(4):
        cells.append(_cell(row, 0, "E. coli", table_id="23:t2"))
        for column, value in enumerate(["0.5", "1", "2", ">64"], start=1):
            cells.append(_cell(row, column, value, table_id="23:t2"))
    table = Table(
        table_id="23:t2",
        page_number=23,
        row_count=4,
        column_count=5,
        cells=cells,
        bbox=(84.5, 74.9, 504.1, 670.2),
    )
    assert is_residue_table(table) is False


# ---- cross-page continuation --------------------------------------------


def _table_payload(self_ref: str, bottom: float) -> dict:
    return {
        "self_ref": self_ref,
        "prov": [{"bbox": {"b": bottom}}],
        "structure_issues": [],
    }


def _page(no: int, *, texts: list[dict], tables: list[dict]) -> dict:
    return {"page": no, "height": 842.0, "width": 595.0, "texts": texts, "tables": tables}


def test_cross_page_continuation_is_marked_at_the_page_fold() -> None:
    table = _table_payload("mineru:table:23:1", 751.91)  # 0.893 of 842
    pages = {
        23: _page(23, texts=[{"text": "body"}], tables=[table]),
        24: _page(24, texts=[{"text": "Strains continued"}], tables=[]),
    }
    _mark_cross_page_continuations(pages)
    assert table["continuation_group"] == "mineru:table:23:1:continues"
    assert any(
        issue.startswith("TABLE_CONTINUES_ON_NEXT_PAGE")
        for issue in table["structure_issues"]
    )


def test_cross_page_continuation_is_not_marked_when_next_page_has_a_table() -> None:
    table = _table_payload("mineru:table:23:1", 751.91)
    pages = {
        23: _page(23, texts=[{"text": "body"}], tables=[table]),
        24: _page(
            24,
            texts=[{"text": "next"}],
            tables=[_table_payload("mineru:table:24:0", 640.0)],
        ),
    }
    _mark_cross_page_continuations(pages)
    assert "continuation_group" not in table
    assert table["structure_issues"] == []


def test_fold_is_still_marked_when_the_next_page_table_starts_below_the_top() -> None:
    """Live page 22/23: MinerU folded page 23's TOP rows into page 22's grid,
    while page 23 still had its own table starting 31% down the sheet.
    "A table on the next page" alone must not disprove the fold."""

    table = _table_payload("mineru:table:22:0", 749.38)
    next_table = {
        "self_ref": "mineru:table:23:0",
        "prov": [{"bbox": {"t": 260.18, "b": 640.0}}],
        "structure_issues": [],
    }
    pages = {
        22: _page(22, texts=[{"text": "body"}], tables=[table]),
        23: _page(23, texts=[{"text": "body"}], tables=[next_table]),
    }
    _mark_cross_page_continuations(pages)
    assert table["continuation_group"] == "mineru:table:22:0:continues"
    note = table["structure_issues"][-1]
    assert note.startswith("TABLE_CONTINUES_ON_NEXT_PAGE")
    assert "31% down the page" in note


def test_table_ending_mid_page_is_not_a_continuation() -> None:
    table = _table_payload("mineru:table:26:0", 300.0)
    pages = {
        26: _page(26, texts=[{"text": "body"}], tables=[table]),
        27: _page(27, texts=[{"text": "next"}], tables=[]),
    }
    _mark_cross_page_continuations(pages)
    assert "continuation_group" not in table


def test_non_contiguous_request_never_marks_the_wrong_next_page() -> None:
    """A job may request 4,23,24,26,… — page 26 continues on 27, which was
    not requested; page 29 is NOT its continuation."""

    table = _table_payload("mineru:table:26:0", 751.91)
    pages = {
        26: _page(26, texts=[{"text": "body"}], tables=[table]),
        29: _page(29, texts=[{"text": "declaration"}], tables=[]),
    }
    _mark_cross_page_continuations(pages)
    assert "continuation_group" not in table
    assert table["structure_issues"] == []


def test_sequence_listing_page_is_not_a_table_continuation() -> None:
    """Page 32 starts with ``<400> 2`` — the page-31 residue grid merely
    ends near the bottom of its own page, it does not fold onto 32."""

    table = _table_payload("mineru:table:31:0", 733.38)
    pages = {
        31: _page(31, texts=[{"text": "Glu Leu Leu"}], tables=[table]),
        32: _page(
            32,
            texts=[{"text": "<210> 2 <211> 35"}, {"text": "<400> 2 catatg"}],
            tables=[],
        ),
    }
    _mark_cross_page_continuations(pages)
    assert "continuation_group" not in table
    assert table["structure_issues"] == []


# ---- a table SPLIT across the fold (the other direction) -----------------


def _grid(
    self_ref: str,
    *,
    top: float,
    bottom: float,
    header: tuple[str, ...] = ("Bacteria*", "Strains", "Chain104", "Chain105"),
    rows: int = 3,
    caption: str | None = None,
) -> dict:
    cells: list[dict] = []
    for column, label in enumerate(header):
        cells.append(
            {
                "start_row_offset_idx": 0,
                "start_col_offset_idx": column,
                "col_span": 1,
                "text": label,
                "is_header": True,
                "column_header": True,
            }
        )
    for row in range(1, rows + 1):
        for column in range(len(header)):
            cells.append(
                {
                    "start_row_offset_idx": row,
                    "start_col_offset_idx": column,
                    "col_span": 1,
                    "text": str(row),
                    "is_header": False,
                    "column_header": False,
                }
            )
    table = {
        "self_ref": self_ref,
        "prov": [{"bbox": {"t": top, "b": bottom}}],
        "data": {"table_cells": cells},
        "structure_issues": [],
    }
    if caption:
        table["caption"] = caption
    return table


def test_table_split_across_the_fold_links_both_pages() -> None:
    """EP3423477B1 p22/p23: the head ends at the fold, p23 opens with a
    header-repeating fragment that carries page 22's last strains."""

    head = _grid("mineru:table:22:1", top=260.0, bottom=751.91,
                 caption="Table 4a. Antibacterial activity of Chain104")
    tail = _grid("mineru:table:23:0", top=84.2, bottom=194.5)
    pages = {
        22: _page(22, texts=[{"text": "body"}], tables=[head]),
        23: _page(23, texts=[{"text": "body"}], tables=[tail]),
    }
    _mark_cross_page_continuations(pages)
    assert head["continuation_group"] == tail["continuation_group"]
    assert head["structure_issues"][-1].startswith("TABLE_CONTINUES_ON_NEXT_PAGE")
    assert "SPLIT" in head["structure_issues"][-1]
    assert tail["structure_issues"][-1].startswith(
        "TABLE_CONTINUED_FROM_PREVIOUS_PAGE"
    )


def test_split_head_marker_replaces_the_projection_promise() -> None:
    """The projection-time fold guess says "MinerU folds its rows into this
    one table" — false when the next page carries the tail. The finding
    replaces the guess instead of stacking two contradicting notes."""

    head = _grid("mineru:table:22:1", top=260.0, bottom=800.0)
    head["structure_issues"] = [
        "TABLE_CONTINUES_ON_NEXT_PAGE: table frame reaches the page bottom — "
        "rows below the fold may belong to the next page (MinerU merges them "
        "into this one table)"
    ]
    head["continuation_group"] = "mineru:22:1:continues"
    tail = _grid("mineru:table:23:0", top=84.2, bottom=194.5)
    pages = {
        22: _page(22, texts=[{"text": "body"}], tables=[head]),
        23: _page(23, texts=[{"text": "body"}], tables=[tail]),
    }
    _mark_cross_page_continuations(pages)
    fold_notes = [
        i for i in head["structure_issues"]
        if i.startswith("TABLE_CONTINUES_ON_NEXT_PAGE")
    ]
    assert len(fold_notes) == 1
    assert "SPLIT" in fold_notes[0]
    # The pre-existing group is kept, so both pages share one identity.
    assert tail["continuation_group"] == "mineru:22:1:continues"


def test_split_tail_inherits_the_head_title_for_extraction_only() -> None:
    """The title (which declares the metric) is printed only on page 22.
    Carried as ``continuation_caption`` — never as the page-23 caption."""

    title = "Table 4a: Minimum inhibitory concentration of peptides"
    head = _grid("mineru:table:22:1", top=260.0, bottom=751.91, caption=title)
    tail = _grid("mineru:table:23:0", top=84.2, bottom=194.5)
    pages = {
        22: _page(22, texts=[{"text": "body"}], tables=[head]),
        23: _page(23, texts=[{"text": "body"}], tables=[tail]),
    }
    _mark_cross_page_continuations(pages)
    assert tail["continuation_caption"] == title
    assert "caption" not in tail


def test_split_links_on_the_repeated_header_alone() -> None:
    """Live page 25/26: the head's frame ended at 0.856 of the page — just
    under the 0.86 fold ratio — yet page 26 re-printed its exact header.
    The repeated header is conclusive on its own."""

    head = _grid("mineru:table:25:0", top=84.2, bottom=720.75,
                 header=("Strains", "Chain104", "Chain105", "Chain109"),
                 caption="Table 4c. Antibacterial activity")
    tail = _grid("mineru:table:26:0", top=99.36, bottom=600.0,
                 header=("Strains", "Chain104", "Chain105", "Chain109"))
    pages = {
        25: _page(25, texts=[{"text": "body"}], tables=[head]),
        26: _page(26, texts=[{"text": "body"}], tables=[tail]),
    }
    _mark_cross_page_continuations(pages)
    assert head["continuation_group"] == tail["continuation_group"]
    assert tail["continuation_caption"] == "Table 4c. Antibacterial activity"
    assert tail["structure_issues"][-1].startswith(
        "TABLE_CONTINUED_FROM_PREVIOUS_PAGE"
    )


def test_unrelated_table_at_the_page_top_is_not_linked() -> None:
    head = _grid("mineru:table:22:1", top=260.0, bottom=780.0)
    tail = _grid(
        "mineru:table:23:0", top=84.2, bottom=194.5,
        header=("Compound", "MIC", "Notes"),
        caption="Table 5. Something else entirely",
    )
    pages = {
        22: _page(22, texts=[{"text": "body"}], tables=[head]),
        23: _page(23, texts=[{"text": "body"}], tables=[tail]),
    }
    _mark_cross_page_continuations(pages)
    assert "continuation_group" not in head
    assert "continuation_group" not in tail


def test_tail_outside_the_parsed_range_is_flagged_as_a_suspicion() -> None:
    """Page 22 was not requested: the pair cannot be verified, so the fragment
    is labelled as a suspicion with the reason — never linked to a guess."""

    tail = _grid("mineru:table:23:0", top=84.2, bottom=194.5)
    pages = {23: _page(23, texts=[{"text": "body"}], tables=[tail])}
    _mark_cross_page_continuations(pages)
    assert "continuation_group" not in tail
    assert any(
        issue.startswith("TABLE_MAYBE_CONTINUED_FROM_PREVIOUS_PAGE")
        for issue in tail["structure_issues"]
    )


def test_titled_table_at_the_page_top_is_not_a_suspicion() -> None:
    tail = _grid("mineru:table:23:0", top=84.2, bottom=194.5, caption="Table 5. MICs")
    pages = {23: _page(23, texts=[{"text": "body"}], tables=[tail])}
    _mark_cross_page_continuations(pages)
    assert tail["structure_issues"] == []


def test_residue_grid_at_the_page_top_is_not_a_table_fragment() -> None:
    """Page 31's 16x16 grid of three-letter codes opens at 0.08 of the page,
    but its first row is residues, not headings — declaring it a table tail
    would put a structure-issue badge on sequence content."""

    residue = _grid(
        "mineru:table:31:0", top=66.52, bottom=733.38,
        header=("Glu", "Leu", "Leu", "Glu180"),
    )
    pages = {31: _page(31, texts=[{"text": "Glu Leu Leu"}], tables=[residue])}
    _mark_cross_page_continuations(pages)
    assert residue["structure_issues"] == []


# ---- running-header captions --------------------------------------------


def test_running_header_caption_is_demoted_not_kept_as_title() -> None:
    header_table = _table_payload("mineru:table:23:0", 200.0)
    header_table["caption"] = "EP 3 423 477 B1 (continued)"
    titled_table = _table_payload("mineru:table:23:1", 751.91)
    titled_table["caption"] = "Table 4b. Antibacterial activity of Chain104"
    pages = {
        23: _page(23, texts=[], tables=[header_table, titled_table]),
    }
    _demote_running_header_captions(pages)
    assert "caption" not in header_table
    assert any(
        issue.startswith("CAPTION_DROPPED_RUNNING_HEADER")
        for issue in header_table["structure_issues"]
    )
    assert titled_table["caption"] == "Table 4b. Antibacterial activity of Chain104"


# ---- table classification ------------------------------------------------


def test_residue_table_classifies_as_sequence() -> None:
    assert classify_table(_residue_table()) is TableKind.SEQUENCE


# ---- declaration evidence ------------------------------------------------


def test_declaration_page_anchors_the_210_block_only() -> None:
    blocks = [
        _block("<210> 1\n<211> 4\n<212> PRT", 29, [84.5, 712.3, 134.5, 747.7]),
        # The organism line sits at the TOP of the page; unioning it would
        # highlight almost the whole sheet.
        _block("<213> Homo sapiens", 29, [84.5, 67.4, 187.5, 90.0]),
        _block("<400> 1", 30, [84.5, 74.9, 120.0, 86.0]),
        _block("Ser Tyr Gly Val His", 30, [120.8, 121.2, 503.0, 582.7]),
    ]
    index = EvidenceIndex()
    records, _scan = parse_st25_blocks_with_scan(
        blocks, "sha256:k12", parser_run_id="run", evidence_index=index
    )
    assert records[0].is_complete is False  # declared 4, observed 5
    spans = {s["page_number"]: s for s in index.to_payload()}
    assert [round(v, 1) for v in spans[29]["bbox"]] == [84.5, 712.3, 134.5, 747.7]
    assert spans[29]["geometry_precision"] == "block"
    assert "210" in spans[29]["raw_text"]
    # The residue page keeps its OWN region (the page-29 top box must not leak).
    assert [round(v, 1) for v in spans[30]["bbox"]] == [120.8, 121.2, 503.0, 582.7]
