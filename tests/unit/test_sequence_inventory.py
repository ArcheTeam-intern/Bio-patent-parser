"""Sequence inventory (C02) tests: declarations, statuses, scope honesty.

Regression scenarios captured from the 2026-10-16 plan review:
- several ST.25 tags printed on ONE line must not leak into the SEQ ID
  (the "SEQ ID swallows <211>" defect);
- a declaration without a residue region is ``declared``, a region without
  recoverable residues is ``missing`` — never conflated;
- duplicate SEQ ID declarations conflict instead of silently merging;
- same residues under different SEQ IDs stay two rows;
- a partial page window never presents itself as whole-document coverage.
"""

from __future__ import annotations

from bio_pdf.extract.sequences.inventory import (
    build_sequence_inventory,
    extract_declared_total,
)
from bio_pdf.extract.sequences.st25 import parse_st25_blocks_with_scan


def _blocks(text: str, page: int = 1) -> list[dict[str, object]]:
    return [{"text": text, "page_number": page}]


def test_inline_tags_on_one_line_are_split() -> None:
    text = "\n".join([
        "<210> 1 <211> 4 <212> PRT",
        "<400> 1",
        "Ala Gly Leu Lys",
    ])
    records, scan = parse_st25_blocks_with_scan(_blocks(text), "sha256:fixture")
    assert [r.seq_id_no for r in records] == ["1"]
    assert records[0].declared_length == 4
    assert records[0].molecule_type == "protein"
    assert scan[0]["declared_length"] == 4


def test_statuses_cover_declared_missing_partial_complete() -> None:
    text = "\n".join([
        "<210> 1",
        "<211> 4",
        "<212> PRT",
        "<400> 1",
        "Ala Gly Leu Lys",
        "<210> 2",
        "<211> 10",
        "<212> PRT",
        "<400> 2",
        "Ala Gly Leu Lys",
        "<210> 3",
        "<211> 5",
        "<212> PRT",
        "<400> 3",
        "This region is an image, no residue text here.",
        "<210> 4",
        "<211> 3",
        "<212> PRT",
    ])
    _, scan = parse_st25_blocks_with_scan(_blocks(text), "sha256:fixture")
    inventory = build_sequence_inventory(scan)
    by_id = {row["seq_id_no"]: row for row in inventory["entries"]}
    assert by_id["1"]["status"] == "complete"
    assert by_id["2"]["status"] == "partial"
    assert "recovered 4 of declared 10" in " ".join(by_id["2"]["reasons"])
    assert by_id["3"]["status"] == "missing"
    assert "OCR candidate" in " ".join(by_id["3"]["reasons"])
    assert by_id["4"]["status"] == "declared"
    summary = inventory["summary"]
    assert summary["observed_declarations"] == 4
    assert summary["complete"] == 1 and summary["partial"] == 1
    assert summary["missing"] == 1 and summary["declared_only"] == 1
    assert summary["located"] == 2


def test_duplicate_seq_id_declarations_conflict() -> None:
    text = "\n".join([
        "<210> 5",
        "<211> 4",
        "<212> PRT",
        "<400> 5",
        "Ala Gly Leu Lys",
        "<210> 5",
        "<211> 6",
        "<212> PRT",
        "<400> 5",
        "Ala Gly Leu Lys",
    ])
    _, scan = parse_st25_blocks_with_scan(_blocks(text), "sha256:fixture")
    inventory = build_sequence_inventory(scan)
    assert inventory["summary"]["conflict"] == 2
    assert all(row["status"] == "conflict" for row in inventory["entries"])


def test_observed_longer_than_declared_is_conflict() -> None:
    text = "\n".join([
        "<210> 9",
        "<211> 2",
        "<212> PRT",
        "<400> 9",
        "Ala Gly Leu Lys",
    ])
    _, scan = parse_st25_blocks_with_scan(_blocks(text), "sha256:fixture")
    inventory = build_sequence_inventory(scan)
    assert inventory["entries"][0]["status"] == "conflict"
    assert "exceeds declared" in inventory["entries"][0]["reasons"][0]


def test_same_residues_different_ids_are_separate_rows() -> None:
    text = "\n".join([
        "<210> 1",
        "<211> 4",
        "<212> PRT",
        "<400> 1",
        "Ala Gly Leu Lys",
        "<210> 2",
        "<211> 4",
        "<212> PRT",
        "<400> 2",
        "Ala Gly Leu Lys",
    ])
    records, scan = parse_st25_blocks_with_scan(_blocks(text), "sha256:fixture")
    assert len(records) == 2
    inventory = build_sequence_inventory(scan)
    assert [row["seq_id_no"] for row in inventory["entries"]] == ["1", "2"]
    assert all(row["status"] == "complete" for row in inventory["entries"])


def test_partial_window_never_claims_full_coverage() -> None:
    text = "\n".join([
        "<160> 82",
        "<210> 1",
        "<211> 4",
        "<212> PRT",
        "<400> 1",
        "Ala Gly Leu Lys",
        "<210> 2",
        "<211> 4",
        "<212> PRT",
        "<400> 2",
        "Ala Gly Leu Lys",
    ])
    blocks = _blocks(text, page=30)
    _, scan = parse_st25_blocks_with_scan(blocks, "sha256:fixture")
    inventory = build_sequence_inventory(
        scan,
        requested_pages=list(range(1, 41)),
        document_page_count=195,
        declared_total=extract_declared_total(blocks),
    )
    summary = inventory["summary"]
    assert summary["declared_total"] == 82
    assert summary["declared_total_unseen"] == 80
    assert summary["scope"]["covers_full_document"] is False
    assert summary["scope"]["document_page_count"] == 195


def test_out_of_scope_declaration_page_is_flagged() -> None:
    text = "<210> 7\n<211> 2\n<212> PRT\n<400> 7\nAla Gly"
    _, scan = parse_st25_blocks_with_scan(_blocks(text, page=50), "sha256:fixture")
    inventory = build_sequence_inventory(scan, requested_pages=list(range(1, 41)))
    assert inventory["entries"][0]["status"] == "outside_scope"
    assert inventory["summary"]["outside_scope"] == 1


def test_unknown_declared_length_is_partial_not_complete() -> None:
    """Residues with no declared length can't claim completeness."""

    text = "\n".join([
        "<210> 3",
        "<212> PRT",
        "<400> 3",
        "Ala Gly Leu Lys",
    ])
    _, scan = parse_st25_blocks_with_scan(_blocks(text), "sha256:fixture")
    inventory = build_sequence_inventory(scan)
    entry = inventory["entries"][0]
    assert entry["status"] == "partial"
    assert "completeness unverified" in entry["reasons"][0]
    assert inventory["summary"]["complete"] == 0
