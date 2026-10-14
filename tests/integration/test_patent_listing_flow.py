"""B09: ST.25 pure continuation pages, long nucleic lines, conflicts.

Deterministic fixtures only — no external OCR needed:

- page 1: ``<210> 1 / <211> 4 / <212> PRT / <400> 1 / Ala Gly``;
  page 2 (NO tags): ``Ser Val`` → one record ``AGSV`` with evidence on
  BOTH pages, page 2's fragment being its real text;
- a 60-char continuous DNA line is a residue line, not prose;
- a zero-residue listing produces an unresolved issue, not a crash;
- the same SEQ ID listed twice with different content keeps BOTH sources
  with a conflict instead of silently picking the first.
"""

from __future__ import annotations

import pytest

from bio_pdf.extract.evidence import EvidenceIndex
from bio_pdf.extract.sequences.st25 import parse_st25_blocks


def _blocks(*items: tuple[int, str]) -> list[dict]:
    return [{"text": text, "page_number": page} for page, text in items]


def test_pure_continuation_page_without_tags() -> None:
    blocks = _blocks(
        (1, "<210> 1"),
        (1, "<211> 4"),
        (1, "<212> PRT"),
        (1, "<400> 1"),
        (1, "Ala Gly"),
        (2, "Ser Val"),  # continuation page, no tags at all
    )
    evidence = EvidenceIndex()
    records = parse_st25_blocks(blocks, "sha256:x", parser_run_id="r1", evidence_index=evidence)
    assert len(records) == 1
    record = records[0]
    assert record.normalized_sequence == "AGSV"
    assert record.observed_length == 4
    assert record.is_complete  # matches the declared length

    spans = list(evidence.spans.values())
    by_page = {s.page_number: s for s in spans}
    assert 1 in by_page and 2 in by_page, f"missing page evidence: {sorted(by_page)}"
    assert "Ala Gly" in (by_page[1].raw_text or "")
    # Page 2's fragment is its OWN text, not a copy of page 1.
    assert "Ser Val" in (by_page[2].raw_text or "")
    assert by_page[1].raw_text != by_page[2].raw_text


def test_sixty_char_dna_line_is_not_prose() -> None:
    dna = "acgtacgtacgtacgtacgtacgtacgtacgtacgtacgtacgtacgtacgtacgtacgt"  # 60
    blocks = _blocks(
        (1, "<210> 2"),
        (1, "<211> 60"),
        (1, "<212> DNA"),
        (1, "<400> 2"),
        (1, dna),
    )
    records = parse_st25_blocks(blocks, "sha256:x")
    assert len(records) == 1
    assert records[0].observed_length == 60
    assert records[0].is_complete


def test_zero_residue_listing_does_not_crash() -> None:
    blocks = _blocks(
        (1, "<210> 3"),
        (1, "<211> 10"),
        (1, "<212> PRT"),
        (1, "<400> 3"),
        (2, "<210> 4"),  # next sequence starts immediately — no residues
        (2, "<211> 2"),
    )
    issues: list[dict] = []
    records = parse_st25_blocks(blocks, "sha256:x", issues=issues)
    # The zero-residue listing must not raise; it surfaces as an explicit
    # unresolved issue — never a NonEmptyStr validation crash and never a
    # fabricated empty record (the contract forbids empty sequences).
    assert not [r for r in records if r.seq_id_no == "3"]
    zero_issues = [i for i in issues if i["reason_code"] == "ZERO_RESIDUE_LISTING"]
    assert zero_issues and "3" in zero_issues[0]["detail"]


def test_same_seq_id_twice_keeps_both_sources() -> None:
    blocks = _blocks(
        (1, "<210> 5"),
        (1, "<211> 2"),
        (1, "<212> PRT"),
        (1, "<400> 5"),
        (1, "Ala Gly"),
        (2, "<210> 5"),  # same SEQ ID, different content
        (2, "<211> 2"),
        (2, "<212> PRT"),
        (2, "<400> 5"),
        (2, "Ser Val"),
    )
    records = parse_st25_blocks(blocks, "sha256:x")
    seqs = [r for r in records if r.seq_id_no == "5"]
    assert len(seqs) == 2, "both listings must survive (conflict, not first-wins)"
    assert {r.normalized_sequence for r in seqs} == {"AG", "SV"}
