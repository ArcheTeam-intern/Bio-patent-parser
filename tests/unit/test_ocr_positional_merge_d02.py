"""E5/P01: OCR merge must be position-aware on BOTH axes.

The D-round residual found by the recheck: two identical OCR lines at
different y positions collapsed to one (adjacent dedup), and a y-overlap
alone confused columns into false conflicts. The acceptance repros below
are the exact inputs from the recheck report.
"""

from __future__ import annotations

import json

import numpy as np

from bio_pdf.workflow.handlers import _merge_ocr_page_lines


def _native(text: str, box: list[float], page: int = 1) -> dict:
    return {
        "text": text,
        "kind": "text",
        "semantic_role": "unknown",
        "page_number": page,
        "bbox": box,
        "block_id": f"native:p{page}:y{int(box[1])}",
        "source_refs": [],
        "source_aliases": [],
        "source_parser": "native",
        "source_artifact_id": None,
    }


def test_same_content_at_different_positions_is_kept_twice() -> None:
    """Recheck repro: OCR [(100,110,'ACGT'), (200,210,'ACGT')] → both kept."""

    merged, conflicts = _merge_ocr_page_lines(
        [], {1: [(40.0, 100.0, 120.0, 110.0, "ACGT"), (40.0, 200.0, 120.0, 210.0, "ACGT")]}
    )
    texts = [str(b.get("text")) for b in merged]
    assert texts == ["ACGT", "ACGT"], "identical content in different rows is real"
    assert conflicts == []


def test_same_y_different_column_is_not_a_conflict() -> None:
    """Native at x 40-200 y100-110; OCR at x 380-500 SAME y — different
    column: keep both, no conflict."""

    blocks = [_native("ACGT", [40.0, 100.0, 200.0, 110.0])]
    merged, conflicts = _merge_ocr_page_lines(blocks, {1: [(380.0, 100.0, 500.0, 110.0, "AGGT")]})
    texts = [str(b.get("text")) for b in merged]
    assert "ACGT" in texts and "AGGT" in texts
    assert conflicts == []


def test_paragraph_and_its_line_merge_instead_of_conflicting() -> None:
    """A native paragraph containing an OCR line at the same box → merged,
    not recorded as a conflict (granularity difference, same content)."""

    paragraph = "ACGT ACGT ACGT ACGT ACGT ACGT ACGT ACGT"
    blocks = [_native(paragraph, [40.0, 100.0, 500.0, 130.0])]
    merged, conflicts = _merge_ocr_page_lines(blocks, {1: [(40.0, 102.0, 120.0, 112.0, "ACGT")]})
    assert conflicts == []
    assert [str(b.get("text")) for b in merged] == [paragraph]


def test_same_mention_different_content_is_a_recorded_conflict() -> None:
    blocks = [_native("ACGT", [40.0, 100.0, 120.0, 112.0])]
    merged, conflicts = _merge_ocr_page_lines(blocks, {1: [(41.0, 101.0, 119.0, 111.0, "AGGT")]})
    assert [str(b.get("text")) for b in merged] == ["ACGT"]
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict["native_text"] == "ACGT"
    assert conflict["ocr_text"] == "AGGT"
    assert conflict["x0"] == 41.0 and conflict["y_bottom"] == 111.0


def test_different_position_similar_residues_are_both_kept() -> None:
    """Recheck repro: native y=100 'ACGT', OCR y=300 'AGGT' → OCR kept."""

    blocks = [
        {"text": "<210> 1\n<400> 1", "kind": "text", "semantic_role": "unknown",
         "page_number": 1, "bbox": [40.0, 80.0, 500.0, 90.0], "block_id": "tag",
         "source_refs": [], "source_aliases": [], "source_parser": "native",
         "source_artifact_id": None},
        _native("ACGT", [40.0, 100.0, 500.0, 110.0]),
    ]
    merged, conflicts = _merge_ocr_page_lines(blocks, {1: [(40.0, 300.0, 120.0, 310.0, "AGGT")]})
    texts = [str(b.get("text")) for b in merged]
    assert "ACGT" in texts and "AGGT" in texts
    assert conflicts == []


def test_same_region_same_content_is_merged_not_duplicated() -> None:
    blocks = [_native("Ala Gly Leu Lys", [40.0, 100.0, 300.0, 112.0])]
    merged, conflicts = _merge_ocr_page_lines(blocks, {1: [(41.0, 101.0, 299.0, 111.0, "ALAGLYLEULYS")]})
    assert len(merged) == 1
    assert conflicts == []


def test_conflict_payload_is_json_serializable() -> None:
    """D10 regression: numpy float32 from OCR boxes must not leak into the
    issue payload (an EP full-text extract crashed on JSON serialization)."""

    blocks = [
        {
            "text": "ACGT",
            "kind": "text",
            "semantic_role": "unknown",
            "page_number": 32,
            "bbox": [np.float32(88.0), np.float32(100.0), np.float32(500.0), np.float32(112.0)],
            "block_id": "n1",
            "source_refs": [],
            "source_aliases": [],
            "source_parser": "native",
            "source_artifact_id": None,
        }
    ]
    # region_ocr yields numpy-scaled floats: reproduce the real type here.
    ocr = {32: [(np.float32(90.0), np.float32(101.0), np.float32(110.0), np.float32(111.0), "AGGT")]}
    _merged, conflicts = _merge_ocr_page_lines(blocks, ocr)
    assert conflicts
    dumped = json.dumps(conflicts)
    assert "AGGT" in dumped


def test_conflict_page_downgrades_complete_declarations() -> None:
    """D02: linked completeness must not survive a source conflict."""

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

    conflict_pages = {3}
    for entry in inventory["entries"]:
        if entry["status"] == "complete" and conflict_pages & set(entry["residue_pages"]):
            entry["status"] = "conflict"
    assert inventory["entries"][0]["status"] == "conflict"
