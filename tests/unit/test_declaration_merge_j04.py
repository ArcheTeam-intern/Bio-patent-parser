"""J04: complementary declarations merge into ONE logical declaration across
the FULL domain chain (merge -> ST.25 parse -> inventory).

The reviewer's minimal input: native ``<210> 1 <211> 5`` + OCR
``<210> 1 <212> PRT`` at the same mention, then ``<400> 1`` and 4 residues.
Before the fix the merge kept two raw lines; the second ``<210>`` restarted
the parser and the length 5 disappeared, while the inventory counted two
declarations and flagged two pseudo-conflicts.
"""

from __future__ import annotations

import json

from bio_pdf.application.comparison_service import _domain_pass_spec
from bio_pdf.workflow.handlers import _merge_ocr_page_lines

_BOX = [10.0, 100.0, 110.0, 110.0]


def _native(text: str, box: list[float]) -> dict:
    return {
        "text": text,
        "kind": "text",
        "semantic_role": "unknown",
        "page_number": 1,
        "bbox": box,
        "block_id": f"native:{int(box[1])}",
        "source_refs": [],
        "source_aliases": [],
        "source_parser": "native",
        "source_artifact_id": None,
    }


def _inventory_entry(merged_blocks) -> dict:
    """The REAL ST.25 scan + inventory over the merged feed."""

    from bio_pdf.extract.sequences.inventory import build_sequence_inventory
    from bio_pdf.extract.sequences.st25 import parse_st25_blocks_with_scan

    blocks = [
        {
            "text": str(b.get("text") or ""),
            "kind": "text",
            "semantic_role": "unknown",
            "page_number": int(b.get("page_number") or 1),
            "bbox": b.get("bbox"),
            "block_id": str(b.get("block_id") or "b"),
            "source_refs": [],
            "source_aliases": [],
            "source_parser": str(b.get("source_parser") or "native"),
            "source_artifact_id": None,
        }
        for b in merged_blocks
    ]
    _records, scan = parse_st25_blocks_with_scan(blocks, "sha256:j04")
    inventory = build_sequence_inventory(scan, requested_pages=[1])
    return inventory["entries"][0]


def _merged_domain(native_decl: str, ocr_decl: str, residues: str):
    blocks = [
        _native(native_decl, _BOX),
        _native("<400> 1", [10.0, 120.0, 110.0, 130.0]),
        _native(residues, [10.0, 140.0, 300.0, 150.0]),
    ]
    merged, records = _merge_ocr_page_lines(
        blocks, {1: [(10.0, 100.0, 110.0, 110.0, ocr_decl)]}
    )
    normalized = {
        "schema_version": "1.0",
        "document_id": "sha256:j04",
        "parser_run_id": "stub:engine",
        "pages": [
            {
                "page_number": 1,
                "width_pt": 595.0,
                "height_pt": 842.0,
                "route": "native",
                "blocks": [
                    {
                        "block_id": str(b.get("block_id")),
                        "kind": "text",
                        "semantic_role": "unknown",
                        "text_raw": str(b.get("text") or ""),
                        "bbox": b.get("bbox"),
                    }
                    for b in merged
                ],
                "tables": [],
                "figures": [],
            }
        ],
        "errors": [],
    }
    summary = _domain_pass_spec(
        json.loads(json.dumps(normalized)), document_id="sha256:j04", engine="docling"
    )
    return merged, records, summary


def test_complementary_fields_yield_one_logical_declaration() -> None:
    merged, records, summary = _merged_domain(
        "<210> 1 <211> 5", "<210> 1 <212> PRT", "Ala Gly Leu Lys"
    )
    assert [r.get("kind") for r in records if r.get("kind") == "declaration_merge"]
    sequences = summary["sequences"]
    assert len(sequences) == 1, sequences
    record = sequences[0]
    assert record["declared_length"] == 5, record
    assert record["molecule_type"] == "protein", record
    assert record["observed_length"] == 4, record

    inventory = summary["inventory_summary"]
    assert inventory["observed_declarations"] == 1, inventory
    assert inventory["conflict"] == 0, inventory
    # The REAL 4-vs-5 length mismatch is preserved, not masked by the merge.
    entry = _inventory_entry(merged)
    assert entry["status"] in ("partial", "declared", "missing"), entry


def test_merged_declaration_with_matching_residues_is_complete() -> None:
    merged, _records, _summary = _merged_domain(
        "<210> 1 <211> 5", "<210> 1 <212> PRT", "Ala Gly Leu Lys Val"
    )
    entry = _inventory_entry(merged)
    assert entry["status"] == "complete", entry
    assert entry["observed_length"] == 5


def test_field_conflicts_still_block_completion() -> None:
    for native_decl, ocr_decl in (
        ("<210> 1 <211> 5", "<210> 1 <211> 50"),
        ("<210> 12", "<210> 1"),
    ):
        _merged, records, summary = _merged_domain(
            native_decl, ocr_decl, "Ala Gly Leu Lys"
        )
        assert [
            r for r in records if r.get("kind") == "content_conflict"
        ], (native_decl, ocr_decl)
        merged_blocks, _r2, _s2 = _merged_domain(native_decl, ocr_decl, "Ala Gly Leu Lys")
        entry = _inventory_entry(merged_blocks)
        assert entry["status"] != "complete", (native_decl, ocr_decl, entry)


def test_channel_and_field_order_swaps_still_merge() -> None:
    cases = [
        ("<210> 1 <212> PRT", "<210> 1 <211> 5"),  # swapped channels
        ("<210> 1 <211> 5", "<210> 1 <212> PRT"),
    ]
    for native_decl, ocr_decl in cases:
        _m, _r, summary = _merged_domain(native_decl, ocr_decl, "Ala Gly Leu Lys Val")
        record = summary["sequences"][0]
        assert record["declared_length"] == 5 and record["molecule_type"] == "protein", (
            native_decl,
            ocr_decl,
            record,
        )


def test_multi_sequence_addendum_merges_per_declaration() -> None:
    _merged_blocks, records, summary = _merged_domain(
        "<210> 1 <211> 5 <210> 2 <211> 3",
        "<210> 1 <212> PRT <210> 2 <212> DNA",
        "Ala Gly Leu Lys",
    )
    merges = [r for r in records if r.get("kind") == "declaration_merge"]
    assert merges, records
    assert merges[0]["merged_text"] == "<210> 1 <211> 5 <212> PRT <210> 2 <211> 3 <212> DNA"
    # BOTH declarations survive the merge with their own fields (the merge
    # is per-declaration, not a flattened union) and the inventory counts
    # exactly two declarations — no pseudo-duplicates. (Whether the
    # following ``<400> 1`` attaches to the first or last declaration in a
    # multi-declaration line is a pre-existing state-machine question and is
    # NOT used as evidence here.)
    from bio_pdf.extract.sequences.inventory import build_sequence_inventory
    from bio_pdf.extract.sequences.st25 import parse_st25_blocks_with_scan

    blocks = [
        {
            "text": str(b.get("text") or ""),
            "kind": "text",
            "semantic_role": "unknown",
            "page_number": 1,
            "bbox": b.get("bbox"),
            "block_id": str(b.get("block_id") or "b"),
            "source_refs": [],
            "source_aliases": [],
            "source_parser": str(b.get("source_parser") or "native"),
            "source_artifact_id": None,
        }
        for b in _merged_blocks
    ]
    _records, scan = parse_st25_blocks_with_scan(blocks, "sha256:j04")
    inventory = build_sequence_inventory(scan, requested_pages=[1])
    entries = {str(e["seq_id_no"]): e for e in inventory["entries"]}
    assert (entries["1"]["declared_length"], entries["1"]["molecule_type"]) == (5, "protein"), entries
    assert (entries["2"]["declared_length"], entries["2"]["molecule_type"]) == (3, "DNA"), entries
    assert inventory["summary"]["observed_declarations"] == 2, inventory["summary"]


# ---- K05: many OCR lines at the same mention form ONE logical declaration ---


def test_multi_ocr_lines_at_one_mention_merge_into_single_declaration() -> None:
    """Two OCR lines that BOTH share the same native mention must form
    ONE logical declaration, not two — the second <210> in a feed
    otherwise restarts the ST.25 parser and the first line's fields
    (e.g. <211>) are dropped, leaving a pseudo-conflict and a missing
    declaration length."""

    # Native block has only <210> 1 <211> 5; OCR adds <212> PRT and
    # <213> Homo sapiens on the same mention in TWO separate OCR lines.
    blocks = [
        _native("<210> 1 <211> 5", _BOX),
        _native("<400> 1", [10.0, 120.0, 110.0, 130.0]),
        _native("Ala Gly Leu Lys Val", [10.0, 140.0, 300.0, 150.0]),
    ]
    merged, records, summary = (
        _merge_ocr_page_lines(
            blocks,
            {
                1: [
                    (10.0, 100.0, 110.0, 110.0, "<210> 1 <212> PRT"),
                    (10.0, 100.0, 110.0, 110.0, "<213> Homo sapiens"),
                ],
            },
        )[0],
        None,
        None,
    )
    # K05 re-check: re-run the full domain chain through the merged feed.
    blocks_for_feed = [
        {
            "text": str(b.get("text") or ""),
            "kind": "text",
            "semantic_role": "unknown",
            "page_number": 1,
            "bbox": b.get("bbox"),
            "block_id": str(b.get("block_id") or "b"),
            "source_refs": [],
            "source_aliases": [],
            "source_parser": str(b.get("source_parser") or "native"),
            "source_artifact_id": None,
        }
        for b in merged
    ]
    normalized = {
        "schema_version": "1.0",
        "document_id": "sha256:k05",
        "parser_run_id": "stub:engine",
        "pages": [
            {
                "page_number": 1,
                "width_pt": 595.0,
                "height_pt": 842.0,
                "route": "native",
                "blocks": [
                    {
                        "block_id": str(b.get("block_id")),
                        "kind": "text",
                        "semantic_role": "unknown",
                        "text_raw": str(b.get("text") or ""),
                        "bbox": b.get("bbox"),
                    }
                    for b in blocks_for_feed
                ],
                "tables": [],
                "figures": [],
            }
        ],
        "errors": [],
    }
    summary = _domain_pass_spec(
        normalized, document_id="sha256:k05", engine="docling"
    )
    sequences = summary["sequences"]
    # Exactly ONE declaration — not two pseudo-declarations with the
    # second restarting the parser.
    assert len(sequences) == 1, sequences
    record = sequences[0]
    assert record["declared_length"] == 5, record
    assert record["molecule_type"] == "protein", record
    assert record["observed_length"] == 5, record
    # Inventory must report a single complete declaration, not two with
    # a fake conflict.
    inv = summary["inventory_summary"]
    assert inv["observed_declarations"] == 1, inv
    assert inv["conflict"] == 0, inv


def test_multi_ocr_lines_with_real_conflict_record_one_conflict_per_line() -> None:
    """Two OCR lines at the same mention, ONE conflicts with the native
    value: exactly one ``content_conflict`` record per OCR line that
    disagrees (no silent supersede, no false merge)."""

    blocks = [
        _native("<210> 1 <211> 5", _BOX),
    ]
    # Single OCR pass — two OCR lines that share the same mention.
    merged, records = _merge_ocr_page_lines(
        blocks,
        {
            1: [
                (10.0, 100.0, 110.0, 110.0, "<210> 1 <211> 5"),  # equal
                (10.0, 100.0, 110.0, 110.0, "<210> 1 <211> 50"),  # conflict
            ],
        },
    )
    conflicts = [r for r in records if r.get("kind") == "content_conflict"]
    assert len(conflicts) == 1, records
    assert "50" in conflicts[0]["ocr_text"]
    # Native stays in the feed — never silently consumed.
    texts = [str(b.get("text") or "") for b in merged]
    assert "<210> 1 <211> 5" in texts