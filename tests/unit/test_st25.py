"""ST.25 sequence-listing parser tests."""

from __future__ import annotations

from bio_pdf.extract.sequences.st25 import (
    parse_st25_blocks,
    parse_st25_blocks_with_scan,
    parse_st25_text,
)


def test_three_letter_sequence_keeps_unknown_residue_and_length() -> None:
    text = "\n".join([
        "<210> 7",
        "<211> 5",
        "<212> PRT",
        "<213> Artificial Sequence",
        "<400> 7",
        "Met Lys Thr Xaa Gly",
        "1 5",
    ])
    records = parse_st25_text(text, document_id="sha256:fixture")
    assert len(records) == 1
    record = records[0]
    assert record.seq_id_no == "7"
    assert record.normalized_sequence == "MKTXG"
    assert record.declared_length == 5
    assert record.observed_length == 5
    assert record.acceptance_state != "accepted"


def test_nucleic_sequence_uppercases_and_keeps_ambiguity() -> None:
    text = "\n".join([
        "<210> 1",
        "<211> 7",
        "<212> DNA",
        "<400> 1",
        "acgt nry",
    ])
    records = parse_st25_text(text, document_id="sha256:fixture")
    assert records[0].normalized_sequence == "ACGTNRY"
    assert records[0].molecule_type == "DNA"
    assert records[0].observed_length == 7


def test_rna_keeps_u_not_converted_to_t() -> None:
    text = "\n".join([
        "<210> 1",
        "<212> RNA",
        "<400> 1",
        "acgu",
    ])
    records = parse_st25_text(text, document_id="sha256:fixture")
    assert records[0].normalized_sequence == "ACGU"


def test_protein_nonstandard_residues_sec_pyl() -> None:
    text = "\n".join([
        "<210> 1",
        "<212> PRT",
        "<400> 1",
        "Sec Pyl",
    ])
    records = parse_st25_text(text, document_id="sha256:fixture")
    assert records[0].normalized_sequence == "UO"


def test_feature_notes_are_captured() -> None:
    text = "\n".join([
        "<210> 1",
        "<212> PRT",
        "<220>",
        "<223> N-terminal acetylated",
        "<400> 1",
        "Met Lys",
    ])
    records = parse_st25_text(text, document_id="sha256:fixture")
    assert "N-terminal acetylated" in records[0].feature_notes


def test_position_numbers_are_removed() -> None:
    text = "\n".join([
        "<210> 1",
        "<212> PRT",
        "<400> 1",
        "Met Lys Thr",
        "1 5",
        "Gly Ala",
    ])
    records = parse_st25_text(text, document_id="sha256:fixture")
    # Position numbers "1 5" must not become residues.
    assert records[0].normalized_sequence == "MKTGA"
    assert records[0].observed_length == 5


def test_multiple_sequences_are_separated() -> None:
    text = "\n".join([
        "<210> 1",
        "<212> PRT",
        "<400> 1",
        "Met Lys",
        "<210> 2",
        "<212> PRT",
        "<400> 2",
        "Gly Ala",
    ])
    records = parse_st25_text(text, document_id="sha256:fixture")
    assert len(records) == 2
    assert records[0].seq_id_no == "1"
    assert records[0].normalized_sequence == "MK"
    assert records[1].seq_id_no == "2"
    assert records[1].normalized_sequence == "GA"


def test_sequence_continues_across_blocks() -> None:
    blocks = [
        {"text": "<210> 1\n<212> PRT\n<400> 1\nMet Lys Thr", "page_number": 1},
        {"text": "Xaa Gly", "page_number": 2},
    ]
    records = parse_st25_blocks(blocks, document_id="sha256:fixture")
    assert len(records) == 1
    assert records[0].normalized_sequence == "MKTXG"
    assert records[0].observed_length == 5


def test_declared_length_mismatch_marks_incomplete() -> None:
    text = "\n".join([
        "<210> 1",
        "<211> 10",
        "<212> PRT",
        "<400> 1",
        "Met Lys Thr",
    ])
    records = parse_st25_text(text, document_id="sha256:fixture")
    assert records[0].declared_length == 10
    assert records[0].observed_length == 3
    assert records[0].is_complete is False


def test_unresolved_three_letter_token_is_preserved_not_dropped() -> None:
    text = "\n".join([
        "<210> 1",
        "<212> PRT",
        "<400> 1",
        "Met Zzz Gly",
    ])
    records = parse_st25_text(text, document_id="sha256:fixture")
    record = records[0]
    # Unknown token becomes "?" and length is preserved (3 residues).
    assert record.normalized_sequence == "M?G"
    assert record.observed_length == 3
    assert record.representation == "unresolved"


def test_empty_text_returns_no_records() -> None:
    assert parse_st25_text("", document_id="sha256:fixture") == []

def test_running_header_and_line_numbers_never_become_residues() -> None:
    """C03/D03: EP-style running material must not fabricate residues (EPB1).

    D03 tightened the rule: repetition alone cannot delete content — the
    header/page-number/line-number occurrences carry the REAL geometry
    (top margin, bottom margin, narrow left column) that justifies treating
    them as furniture.
    """

    PAGE_W, PAGE_H = 595.22, 842.0

    def line_block(text: str, page: int, bbox: list[float]) -> dict:
        return {
            "text": text,
            "page_number": page,
            "bbox": bbox,
            "page_width": PAGE_W,
            "page_height": PAGE_H,
            "semantic_role": "unknown",
        }

    blocks: list[dict] = []
    for n in (30, 31, 32):
        # Header in the top margin, footer page number, left line-number
        # column, then the real declaration + residue line in the body.
        blocks.append(line_block("EP 3 027 649 B1", n, [267.0, 45.0, 341.0, 56.0]))
        for y in (118.0, 177.0, 236.0):
            blocks.append(line_block(str(int(y)), n, [44.0, y, 56.0, y + 9.0]))
        blocks.append(line_block(f"<210> {n - 29}", n, [88.0, 300.0, 140.0, 310.0]))
        blocks.append(line_block("<211> 4", n, [88.0, 312.0, 140.0, 322.0]))
        blocks.append(line_block("<212> PRT", n, [88.0, 324.0, 140.0, 334.0]))
        blocks.append(line_block(f"<400> {n - 29}", n, [88.0, 336.0, 140.0, 346.0]))
        blocks.append(line_block("Ala Gly Leu Lys", n, [88.0, 348.0, 200.0, 358.0]))
        blocks.append(line_block(str(n), n, [300.0, 805.0, 309.0, 812.0]))

    records = parse_st25_blocks(blocks, "sha256:fixture")
    # Three pages, each declaring its own SEQ ID; the running header, page
    # number and line-number column are filtered and never reach residues.
    assert len(records) == 3
    for record in records:
        assert "EP" not in record.normalized_sequence
        assert "B1" not in record.raw_sequence
        assert record.normalized_sequence == "AGLK"


def test_repeated_legit_residue_lines_without_margin_evidence_survive() -> None:
    """D03 acceptance repro: three pages each declaring 'Ala Cys 2' in the
    body — repetition is content, not furniture: all three must recover AC."""

    PAGE_W, PAGE_H = 595.22, 842.0

    def body_block(text: str, page: int, y: float) -> dict:
        return {
            "text": text,
            "page_number": page,
            "bbox": [88.0, y, 300.0, y + 10.0],
            "page_width": PAGE_W,
            "page_height": PAGE_H,
            "semantic_role": "unknown",
        }

    blocks: list[dict] = []
    for n in (1, 2, 3):
        blocks.append(body_block(f"<210> {n}", n, 200.0))
        blocks.append(body_block("<211> 2", n, 212.0))
        blocks.append(body_block("<212> PRT", n, 224.0))
        blocks.append(body_block(f"<400> {n}", n, 236.0))
        blocks.append(body_block("Ala Cys 2", n, 248.0))

    records = parse_st25_blocks(blocks, "sha256:fixture")
    assert len(records) == 3, "legitimate repeated residue lines must not be deleted"
    for record in records:
        assert record.normalized_sequence == "AC"
        assert record.observed_length == 2


def test_short_nonrepeated_text_is_not_filtered() -> None:
    """A one-off short line inside a residue block is still residue input."""

    page = "\n".join([
        "<210> 1",
        "<211> 3",
        "<212> PRT",
        "<400> 1",
        "Ala",
        "Gly",
        "Leu",
    ])
    records = parse_st25_blocks([{"text": page, "page_number": 1}], "sha256:fixture")
    assert records[0].normalized_sequence == "AGL"


def test_number_only_region_is_missing_not_a_crash() -> None:
    """OCR of a line-number column must not build an empty record (C03)."""

    page = "\n".join([
        "<210> 4",
        "<211> 10",
        "<212> PRT",
        "<400> 4",
        "1 5",
        "10 15",
        "20 25",
    ])
    records = parse_st25_blocks([{"text": page, "page_number": 1}], "sha256:fixture")
    assert records == []
    _, scan = parse_st25_blocks_with_scan(
        [{"text": page, "page_number": 1}], "sha256:fixture"
    )
    assert scan[0]["saw_400"] is True
    assert scan[0]["observed_length"] is None


def test_repeated_declaration_tag_lines_survive_the_running_filter() -> None:
    """C03 regression: "<212> DNA"/"<211> 364" repeat across pages in real
    catalogs — the running-material filter must never drop tag lines."""

    def page(seq_id: int) -> str:
        return "\n".join([
            "EP 3 027 649 B1",
            "5",
            "10",
            f"<210> {seq_id}",
            "<211> 364",
            "<212> DNA",
            f"<400> {seq_id}",
            "atg gtg agc aag ggc gag gag ctg ttc acc ggg gtg gtg ccc atc",
        ])

    blocks = [{"text": page(n), "page_number": n} for n in (30, 31, 32)]
    records = parse_st25_blocks(blocks, "sha256:fixture")
    assert len(records) == 3
    for record in records:
        assert record.declared_length == 364
        assert record.molecule_type == "DNA"


def test_one_header_occurrence_never_blacklists_body_duplicates() -> None:
    """E6/P02 recheck repro: three body pages declare SEQ 1/2/3 with the
    same `Ala Cys 2` line; a fourth page prints the SAME text as a running
    header. The body residue must still recover AC on all three — a single
    header occurrence must not blacklist the string globally."""

    PAGE_W, PAGE_H = 595.22, 842.0

    def body(text: str, page: int, y: float) -> dict:
        return {
            "text": text,
            "page_number": page,
            "bbox": [88.0, y, 300.0, y + 10.0],
            "page_width": PAGE_W,
            "page_height": PAGE_H,
            "semantic_role": "unknown",
        }

    blocks: list[dict] = []
    for n in (1, 2, 3):
        blocks.append(body(f"<210> {n}", n + 29, 200.0))
        blocks.append(body("<211> 2", n + 29, 212.0))
        blocks.append(body("<212> PRT", n + 29, 224.0))
        blocks.append(body(f"<400> {n}", n + 29, 236.0))
        blocks.append(body("Ala Cys 2", n + 29, 248.0))
    # Fourth page: the identical text appears as a running header (top margin).
    blocks.append(
        {
            "text": "Ala Cys 2",
            "page_number": 99,
            "bbox": [200.0, 30.0, 300.0, 45.0],
            "page_width": PAGE_W,
            "page_height": PAGE_H,
            "semantic_role": "header",
        }
    )

    records = parse_st25_blocks(blocks, "sha256:fixture")
    assert len(records) == 3, "body occurrences must survive a header twin"
    for record in records:
        assert record.normalized_sequence == "AC"
