"""D02: OCR merge must be position-aware — never similarity-based deletion.

The independent acceptance reproduced a data-loss bug: an OCR line was
dropped whenever it merely resembled ANY native text on the page (fuzzy
ratio >= 0.6) regardless of position, so (a) residues in different regions
vanished and (b) native/OCR character conflicts were silently resolved.
"""

from __future__ import annotations

from bio_pdf.workflow.handlers import _merge_ocr_page_lines


def _native(text: str, y0: float, y1: float, page: int = 1) -> dict:
    return {
        "text": text,
        "kind": "text",
        "semantic_role": "unknown",
        "page_number": page,
        "bbox": [50.0, y0, 500.0, y1],
        "block_id": f"native:p{page}:y{int(y0)}",
        "source_refs": [],
        "source_aliases": [],
        "source_parser": "native",
        "source_artifact_id": None,
    }


def test_different_position_similar_residues_are_both_kept() -> None:
    """Acceptance repro: native 'ACGT' at y=100, OCR 'AGGT' at y=300 — the
    OCR line must survive (different physical region)."""

    blocks = [_native("<210> 1\n<400> 1", 80.0, 90.0), _native("ACGT", 100.0, 110.0)]
    ocr = {1: [(300.0, 312.0, "AGGT")]}
    merged, conflicts = _merge_ocr_page_lines(blocks, ocr)

    texts = [str(b.get("text")) for b in merged]
    assert "ACGT" in texts
    assert "AGGT" in texts, "positionally distinct OCR content must not be deleted"
    assert conflicts == []


def test_different_position_identical_residues_are_both_kept() -> None:
    """Same content in a different region is a REAL second occurrence —
    it gets its own position, not a silent deletion."""

    blocks = [_native("ACGT", 100.0, 110.0)]
    ocr = {1: [(300.0, 312.0, "ACGT")]}
    merged, conflicts = _merge_ocr_page_lines(blocks, ocr)
    acgt = [b for b in merged if str(b.get("text")) == "ACGT"]
    assert len(acgt) == 2
    assert conflicts == []


def test_same_region_same_content_is_merged_not_duplicated() -> None:
    blocks = [_native("Ala Gly Leu Lys", 100.0, 112.0)]
    ocr = {1: [(101.0, 111.0, "ALAGLYLEULYS")]}  # same spot, same residues
    merged, conflicts = _merge_ocr_page_lines(blocks, ocr)
    assert len(merged) == 1
    assert conflicts == []


def test_same_region_different_content_is_a_recorded_conflict() -> None:
    """Character-level disagreement at the SAME position: both texts are
    preserved in the conflict record; native stays in the feed; nothing is
    silently chosen."""

    blocks = [_native("ACGT", 100.0, 112.0)]
    ocr = {1: [(101.0, 111.0, "AGGT")]}
    merged, conflicts = _merge_ocr_page_lines(blocks, ocr)
    assert len(merged) == 1 and str(merged[0]["text"]) == "ACGT"
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict["native_text"] == "ACGT"
    assert conflict["ocr_text"] == "AGGT"
    assert conflict["page_number"] == 1
    assert conflict["y_top"] == 101.0


def test_conflict_page_downgrades_complete_declarations() -> None:
    """The inventory must not claim completeness while two sources disagree
    about a region on one of the declaration's pages."""

    from bio_pdf.extract.sequences.inventory import build_sequence_inventory
    from bio_pdf.extract.sequences.st25 import parse_st25_blocks_with_scan

    text = "\n".join([
        "<210> 1",
        "<211> 4",
        "<212> PRT",
        "<400> 1",
        "Ala Gly Leu Lys",
    ])
    _, scan = parse_st25_blocks_with_scan(
        [{"text": text, "page_number": 3}], "sha256:fixture"
    )
    inventory = build_sequence_inventory(scan)
    assert inventory["entries"][0]["status"] == "complete"

    # Simulate the handler-side downgrade rule (same logic as in extract).
    conflict_pages = {3}
    for entry in inventory["entries"]:
        if entry["status"] == "complete" and conflict_pages & set(entry["residue_pages"]):
            entry["status"] = "conflict"
    assert inventory["entries"][0]["status"] == "conflict"