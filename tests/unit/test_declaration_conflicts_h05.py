"""H05/P02: digit CONTAINMENT must not bypass declaration conflicts.

G06 made the fingerprints digit-faithful, but the coverage/supersede path
still used plain substring containment: ``<210> 1`` is a substring of
``<210> 12``, so the shorter declaration was silently consumed (or the
longer one silently replaced the shorter) with no conflict event.
Declaration-bearing lines now compare by tag SIGNATURE; the substring
route applies only to tag-free content.
"""

from __future__ import annotations

from bio_pdf.workflow.handlers import _merge_ocr_page_lines


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


_BOX = [10.0, 100.0, 110.0, 110.0]


def _conflicts(records):
    return [r for r in records if r.get("kind") == "content_conflict"]


def _supersedes(records):
    return [r for r in records if r.get("kind") == "coverage_supersede"]


def test_native_longer_declaration_does_not_consume_shorter_ocr() -> None:
    """<210> 12 (native) + <210> 1 (OCR): 12 must not "cover" 1."""
    merged, records, _ = _merge_ocr_page_lines(
        [_native("<210> 12", _BOX)], {1: [(10.0, 100.0, 110.0, 110.0, "<210> 1")]}
    )
    assert [str(b.get("text")) for b in merged] == ["<210> 12"], "native stays"
    assert _conflicts(records), "the disagreement must be recorded"
    assert not _supersedes(records)


def test_native_shorter_declaration_does_not_get_superseded_by_longer_ocr() -> None:
    """<210> 1 (native) + <210> 12 (OCR): no blanket supersede — conflict."""
    merged, records, _ = _merge_ocr_page_lines(
        [_native("<210> 1", _BOX)], {1: [(10.0, 100.0, 110.0, 110.0, "<210> 12")]}
    )
    assert [str(b.get("text")) for b in merged] == ["<210> 1"], "native stays"
    assert _conflicts(records)
    assert not _supersedes(records)


def test_tag_number_prefix_pairs_are_all_conflicts() -> None:
    for native_tag, ocr_tag in (
        ("<210> 2", "<210> 20"),
        ("<211> 5", "<211> 50"),
        ("<211> 10", "<211> 100"),
    ):
        for a, b in ((native_tag, ocr_tag), (ocr_tag, native_tag)):
            merged, records, _ = _merge_ocr_page_lines(
                [_native(a, _BOX)], {1: [(10.0, 100.0, 110.0, 110.0, b)]}
            )
            assert [str(x.get("text")) for x in merged] == [a], (a, b)
            assert _conflicts(records), (a, b)
            assert not _supersedes(records), (a, b)


def test_identical_declarations_still_merge() -> None:
    merged, records, _ = _merge_ocr_page_lines(
        [_native("<210> 1", _BOX)], {1: [(10.0, 100.0, 110.0, 110.0, "<210> 1")]}
    )
    assert len(merged) == 1 and records == []


def test_identical_multi_tag_lines_still_merge() -> None:
    text = "<210> 1 <211> 5 <212> PRT <400> 1"
    merged, records, _ = _merge_ocr_page_lines(
        [_native(text, _BOX)], {1: [(10.0, 100.0, 110.0, 110.0, text)]}
    )
    assert len(merged) == 1 and records == []


def test_multi_tag_lines_with_different_values_conflict() -> None:
    merged, records, _ = _merge_ocr_page_lines(
        [_native("<210> 1 <211> 5", _BOX)],
        {1: [(10.0, 100.0, 110.0, 110.0, "<210> 1 <211> 50")]},
    )
    assert [str(b.get("text")) for b in merged] == ["<210> 1 <211> 5"]
    assert _conflicts(records)
    assert not _supersedes(records)


def test_tagfree_paragraph_containment_still_dedupes() -> None:
    """Ordinary residue/paragraph containment (F05) is untouched: a native
    paragraph that fully covers an OCR line still consumes it."""

    paragraph = "ACGT ACGT ACGT ACGT ACGT ACGT ACGT ACGT"
    merged, records, _ = _merge_ocr_page_lines(
        [_native(paragraph, [40.0, 100.0, 500.0, 130.0])],
        {1: [(40.0, 102.0, 120.0, 112.0, "ACGT")]},
    )
    assert [str(b.get("text")) for b in merged] == [paragraph]
    assert records == []


# ---- I05/P01: compatible ADDITIONS merge; only shared-value conflicts block -


def _feed_sequence(merged_blocks) -> list:
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
    records, _scan = parse_st25_blocks_with_scan(blocks, "sha256:fixture")
    return records


def test_compatible_length_addition_from_ocr_enters_the_parse_feed() -> None:
    """I05 recheck repro: native ``<210> 1`` + OCR ``<210> 1 <211> 5`` — the
    extra <211> is COMPATIBLE information and must reach the parser."""

    blocks = [
        _native("<210> 1", _BOX),
        _native("Ala Gly Leu Lys", [10.0, 200.0, 200.0, 210.0]),
    ]
    merged, records, _ = _merge_ocr_page_lines(
        blocks,
        {1: [(10.0, 100.0, 110.0, 110.0, "<210> 1 <211> 5 <400> 1")]},
    )
    texts = [str(b.get("text")) for b in merged]
    assert any("<211> 5" in t for t in texts), texts
    assert not _conflicts(records), records
    # The field actually reaches the residue parse.
    sequences = _feed_sequence(merged)
    assert sequences and sequences[0].declared_length == 5, sequences


def test_native_superset_is_kept_and_ocr_subset_consumed() -> None:
    merged, records, _ = _merge_ocr_page_lines(
        [_native("<210> 1 <211> 5", _BOX)],
        {1: [(10.0, 100.0, 110.0, 110.0, "<210> 1")]},
    )
    assert [str(b.get("text")) for b in merged] == ["<210> 1 <211> 5"]
    assert not _conflicts(records)


def test_disjoint_additions_merge_into_one_logical_declaration() -> None:
    """J04: native declares length, OCR declares the molecule type — they are
    merged into ONE logical declaration (a second raw ``<210>`` line would
    restart the parser's declaration state and lose the first field)."""

    merged, records, _ = _merge_ocr_page_lines(
        [_native("<210> 1 <211> 5", _BOX)],
        {1: [(10.0, 100.0, 110.0, 110.0, "<210> 1 <212> PRT")]},
    )
    texts = [str(b.get("text")) for b in merged]
    assert texts == ["<210> 1 <211> 5 <212> PRT"], texts
    assert not _conflicts(records)
    merges = [r for r in records if r.get("kind") == "declaration_merge"]
    assert merges, records
    fields = {f["field"]: f["origin"] for f in merges[0]["fields"]}
    assert fields["211"] == "native" and fields["212"] == "ocr"
    assert merges[0]["merged_text"] == "<210> 1 <211> 5 <212> PRT"
    # Provenance for BOTH channels rides on the surviving block.
    block = next(b for b in merged if "<211> 5" in str(b.get("text")))
    kinds = {a["kind"] for a in block.get("source_aliases") or []}
    assert {"declaration_merge_native", "declaration_merge_ocr"} <= kinds


def test_multi_sequence_declaration_blocks_compare_per_declaration() -> None:
    text = "<210> 1 <211> 5 <210> 2 <211> 10"
    merged, records, _ = _merge_ocr_page_lines(
        [_native(text, _BOX)], {1: [(10.0, 100.0, 110.0, 110.0, text)]}
    )
    assert len(merged) == 1 and records == []

    merged2, records2, _ = _merge_ocr_page_lines(
        [_native(text, _BOX)],
        {1: [(10.0, 100.0, 110.0, 110.0, "<210> 1 <211> 5 <210> 2 <211> 20")]},
    )
    assert [str(b.get("text")) for b in merged2] == [text], "the 2/20 conflict keeps the native"
    assert _conflicts(records2)
    assert not _supersedes(records2)


def test_field_value_conflicts_still_block_both_directions() -> None:
    for native_text, ocr_text in (
        ("<210> 1 <211> 5", "<210> 1 <211> 50"),
        ("<210> 1 <211> 50", "<210> 1 <211> 5"),
        ("<210> 12", "<210> 1"),
    ):
        merged, records, _ = _merge_ocr_page_lines(
            [_native(native_text, _BOX)],
            {1: [(10.0, 100.0, 110.0, 110.0, ocr_text)]},
        )
        assert [str(b.get("text")) for b in merged] == [native_text], (native_text, ocr_text)
        assert _conflicts(records), (native_text, ocr_text)
