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
    """C03: EP-style running material must not fabricate residues (EPB1)."""

    def page(seq_id: int) -> str:
        return "\n".join([
            "EP 3 027 649 B1",
            "31",
            "5",
            "10",
            "15",
            f"<210> {seq_id}",
            "<211> 4",
            "<212> PRT",
            f"<400> {seq_id}",
            "Ala Gly Leu Lys",
        ])

    blocks = [{"text": page(n), "page_number": n} for n in (30, 31, 32)]
    records = parse_st25_blocks(blocks, "sha256:fixture")
    # Three pages, each declaring its own SEQ ID; the running header, page
    # number and line-number column are filtered and never reach residues.
    assert len(records) == 3
    for record in records:
        assert "EP" not in record.normalized_sequence
        assert "B1" not in record.raw_sequence
        assert record.normalized_sequence == "AGLK"


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
