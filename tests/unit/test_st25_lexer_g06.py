"""G06: ST.25 lexical layer (margin line numbers) + digit-faithful merge.

P06: real MinerU output printed ``50 <400> 1`` (page-margin count before the
tag); the column-0 anchor missed it and SEQ 1 was lost. P01: the old merge
fingerprint deleted ALL digits, silently merging ``<210> 1`` with
``<210> 2`` and ``<211> 10`` with ``<211> 20``.
"""

from __future__ import annotations

import json

from bio_pdf.extract.sequences.st25 import _St25StateMachine
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


# ---- P01: declarations keep their digits ----------------------------------


def test_declaration_number_mismatch_is_a_recorded_conflict_not_a_merge() -> None:
    blocks = [_native("<210> 1", [10.0, 100.0, 60.0, 110.0])]
    merged, records, _ = _merge_ocr_page_lines(
        blocks, {1: [(10.0, 100.0, 60.0, 110.0, "<210> 2")]}
    )
    assert [str(b.get("text")) for b in merged] == ["<210> 1"], "native stays"
    conflicts = [r for r in records if r.get("kind") == "content_conflict"]
    assert len(conflicts) == 1
    assert conflicts[0]["native_text"] == "<210> 1"
    assert conflicts[0]["ocr_text"] == "<210> 2"


def test_declared_length_mismatch_is_not_merged() -> None:
    blocks = [_native("<211> 10", [10.0, 100.0, 60.0, 110.0])]
    _merged, records, _ = _merge_ocr_page_lines(
        blocks, {1: [(10.0, 100.0, 60.0, 110.0, "<211> 20")]}
    )
    assert [r for r in records if r.get("kind") == "content_conflict"]


def test_identical_declarations_still_merge() -> None:
    blocks = [_native("<210> 1", [10.0, 100.0, 60.0, 110.0])]
    merged, records, _ = _merge_ocr_page_lines(
        blocks, {1: [(10.0, 100.0, 60.0, 110.0, "<210> 1")]}
    )
    assert [str(b.get("text")) for b in merged] == ["<210> 1"]
    assert records == []


def test_residue_row_count_marker_difference_is_tolerated() -> None:
    """Same residues, only the margin COUNT differs (1 5 vs 1 10): still the
    same content — consumed, not a conflict. This is the dedicated rule the
    recheck asked for instead of "delete every digit"."""

    blocks = [_native("Ser Tyr Gly Val His 1 5", [10.0, 100.0, 200.0, 110.0])]
    merged, records, _ = _merge_ocr_page_lines(
        blocks, {1: [(10.0, 100.0, 200.0, 110.0, "Ser Tyr Gly Val His 1 10")]}
    )
    assert [str(b.get("text")) for b in merged] == ["Ser Tyr Gly Val His 1 5"]
    assert records == []


def test_tag_lines_never_get_count_tolerance() -> None:
    """A stray digit change inside a TAG-bearing line is content (the loose
    rule is disabled whenever a tag is present)."""

    blocks = [_native("<210> 12", [10.0, 100.0, 60.0, 110.0])]
    _merged, records, _ = _merge_ocr_page_lines(
        blocks, {1: [(10.0, 100.0, 60.0, 110.0, "<210> 13")]}
    )
    assert [r for r in records if r.get("kind") == "content_conflict"]


# ---- P06: margin line numbers before tags ---------------------------------


def _feed(lines: list[str], page: int = 30) -> _St25StateMachine:
    sm = _St25StateMachine(document_id="sha256:fixture")
    for line in lines:
        sm.feed_line(line, page_number=page)
    sm.finish()
    return sm


def test_real_margin_number_before_tag_recovers_sygvh() -> None:
    """The recheck's real line: ``50 <400> 1`` followed by the residues.
    Before the fix the state machine never entered residue state."""

    sm = _feed(["50 <210> 1", "50 <211> 5", "50 <212> PRT", "50 <400> 1", "Ser Tyr Gly Val His 1 5"])
    assert len(sm.records) == 1
    assert sm.records[0].normalized_sequence == "SYGVH"
    assert sm.records[0].declared_length == 5
    # Clean→raw mapping: every stripped line records its original.
    assert len(sm.lex_mappings) == 4
    assert sm.lex_mappings[0]["raw"] == "50 <210> 1"
    assert sm.lex_mappings[0]["clean"] == "<210> 1"
    assert sm.lex_mappings[0]["stripped_margin"] == "50"
    assert all(m["page_number"] == 30 for m in sm.lex_mappings)


def test_margin_with_multiple_tags_on_one_line() -> None:
    sm = _feed(["12 <210> 2 <211> 5 <212> PRT <400> 2", "Ser Tyr Gly Val His"])
    assert len(sm.records) == 1
    assert sm.records[0].seq_id_no == "2"
    assert sm.records[0].declared_length == 5
    assert sm.records[0].normalized_sequence == "SYGVH"


def test_prose_mentioning_a_tag_is_not_a_declaration() -> None:
    sm = _feed(["see <400> 1 for details"])
    assert sm.records == []
    assert sm.lex_mappings == []


def test_bare_number_lines_stay_residue_lines() -> None:
    """A margin number NOT followed by a tag keeps its normal path — inside
    a residue block it is just a count column and must not be treated as a
    line-number prefix."""

    sm = _feed(
        ["<210> 3", "<211> 2", "<400> 3", "Ser Tyr", "50 1 5"]
    )
    assert len(sm.records) == 1
    assert sm.records[0].normalized_sequence == "SY"
    # The count line was swallowed as residues/counts, not stripped as margin.
    assert sm.lex_mappings == []
