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
    merged, records = _merge_ocr_page_lines(
        [_native("<210> 12", _BOX)], {1: [(10.0, 100.0, 110.0, 110.0, "<210> 1")]}
    )
    assert [str(b.get("text")) for b in merged] == ["<210> 12"], "native stays"
    assert _conflicts(records), "the disagreement must be recorded"
    assert not _supersedes(records)


def test_native_shorter_declaration_does_not_get_superseded_by_longer_ocr() -> None:
    """<210> 1 (native) + <210> 12 (OCR): no blanket supersede — conflict."""
    merged, records = _merge_ocr_page_lines(
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
            merged, records = _merge_ocr_page_lines(
                [_native(a, _BOX)], {1: [(10.0, 100.0, 110.0, 110.0, b)]}
            )
            assert [str(x.get("text")) for x in merged] == [a], (a, b)
            assert _conflicts(records), (a, b)
            assert not _supersedes(records), (a, b)


def test_identical_declarations_still_merge() -> None:
    merged, records = _merge_ocr_page_lines(
        [_native("<210> 1", _BOX)], {1: [(10.0, 100.0, 110.0, 110.0, "<210> 1")]}
    )
    assert len(merged) == 1 and records == []


def test_identical_multi_tag_lines_still_merge() -> None:
    text = "<210> 1 <211> 5 <212> PRT <400> 1"
    merged, records = _merge_ocr_page_lines(
        [_native(text, _BOX)], {1: [(10.0, 100.0, 110.0, 110.0, text)]}
    )
    assert len(merged) == 1 and records == []


def test_multi_tag_lines_with_different_values_conflict() -> None:
    merged, records = _merge_ocr_page_lines(
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
    merged, records = _merge_ocr_page_lines(
        [_native(paragraph, [40.0, 100.0, 500.0, 130.0])],
        {1: [(40.0, 102.0, 120.0, 112.0, "ACGT")]},
    )
    assert [str(b.get("text")) for b in merged] == [paragraph]
    assert records == []
