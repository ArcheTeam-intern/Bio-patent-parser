"""B10: evaluation framework regressions (plan's mandatory negative matrix).

- confirmed negative scope: 0 gold + 5 false candidates → FP=5,
  precision=0, recall=None (never "100%" or "n/a both");
- unannotated manifest → not_evaluated;
- 2 inline gold + 0 candidates → FN=2;
- the SAME source position appearing twice (dual-channel duplicate) →
  one TP, the extra candidate is FP;
- right number, wrong entity → FP/FN each 1;
- wrong sequence under the same SEQ number → exact FP+FN, detected still
  counted.
"""

from __future__ import annotations

import json
from pathlib import Path

from bio_pdf.contracts import ActivityValue, Measurement, SequenceRecord
from bio_pdf.evaluation.annotation import Annotation
from bio_pdf.evaluation.corpus import CorpusScope, evaluation_status, load_corpus_scope
from bio_pdf.evaluation.matching import match_measurements, match_sequences
from bio_pdf.evaluation.metrics import safe_ratio


def _seq_gold(seq: str, *, seq_id: str | None = None, page: int = 1, verified: bool = True) -> Annotation:
    return Annotation(
        annotation_id=f"g-{seq}-{seq_id}-{page}",
        document_id="doc",
        page_number=page,
        kind="sequence",
        seq_id_no=seq_id,
        sequence=seq,
        human_verified=verified,
        status="verified" if verified else "draft",
    )


def _seq_candidate(seq: str, *, seq_id: str | None = None, page: int = 1) -> SequenceRecord:
    return _record_with_page(seq, seq_id=seq_id, page=page)


def _record_with_page(
    seq: str, *, seq_id: str | None, page: int
) -> SequenceRecord:
    from bio_pdf.contracts import EvidenceFragment, EvidenceSpan

    span = EvidenceSpan(
        evidence_id=f"ev-{seq}-{seq_id}-{page}",
        document_id="doc",
        page_number=page,
        fragments=[EvidenceFragment(page_number=page)],
        geometry_precision="page",
    )
    return SequenceRecord(
        sequence_id=f"doc:seq:{seq_id or seq}:{page}",
        document_id="doc",
        seq_id_no=seq_id,
        molecule_type="protein",
        representation="one_letter",
        raw_sequence=seq,
        normalized_sequence=seq,
        evidence_ids=[span.evidence_id],
    )


def _candidate_with_page(
    seq: str, page: int, *, suffix: str = ""
) -> tuple[SequenceRecord, dict[str, dict]]:
    """An inline candidate plus its evidence index entry (page anchor)."""

    evidence_id = f"ev-page-{seq}-{page}{suffix}"
    record = SequenceRecord(
        sequence_id=f"doc:seq-page:{seq}:{page}{suffix}",
        document_id="doc",
        seq_id_no=None,
        molecule_type="protein",
        representation="one_letter",
        raw_sequence=seq,
        normalized_sequence=seq,
        evidence_ids=[evidence_id],
    )
    index = {
        evidence_id: {
            "page_number": page,
            "fragments": [{"page_number": page}],
        }
    }
    return record, index


def test_confirmed_negative_scope_counts_every_candidate_as_fp() -> None:
    gold: list[Annotation] = []  # reviewer confirmed: no sequences here
    candidates = [_seq_candidate("AAAA", seq_id=str(i)) for i in range(5)]
    result = match_sequences(gold, candidates, scope=["doc"])
    assert result.total_gold == 0
    assert result.false_positives == 5
    assert safe_ratio(result.exact_matches, 5) == 0.0  # precision
    assert safe_ratio(result.exact_matches, result.total_gold) is None  # recall unknown


def test_unannotated_manifest_is_not_evaluated(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"schema_version": "v2", "scopes": [], "annotations": []}),
        encoding="utf-8",
    )
    scopes, annotations = load_corpus_scope(manifest)
    assert evaluation_status(scopes, annotations) == "not_evaluated"


def test_verified_negative_scope_is_evaluated() -> None:
    scopes = [
        CorpusScope(document_id="doc", pages=[1, 2], scope_verified=True, no_sequence_region=True)
    ]
    assert evaluation_status(scopes, []) == "evaluated"


def test_two_inline_gold_zero_candidates_is_fn2() -> None:
    gold = [_seq_gold("RRRQRRKKRG", page=9), _seq_gold("DTRLNTVWMW", page=9)]
    result = match_sequences(gold, [], scope=["doc"])
    assert result.missed == 2
    assert result.exact_matches == 0


def test_inline_gold_matches_dual_channel_candidate_once() -> None:
    gold = [_seq_gold("RRRQRRKKRG", page=9)]
    first, index_a = _candidate_with_page("RRRQRRKKRG", 9)
    # A duplicate from the other channel at the same position.
    duplicate, index_b = _candidate_with_page("RRRQRRKKRG", 9, suffix="-dup")
    result = match_sequences(
        gold, [first, duplicate], scope=["doc"], evidence_index={**index_a, **index_b}
    )
    assert result.exact_matches == 1  # one TP
    assert result.false_positives == 1  # the duplicate is an extra FP


def test_inline_gold_wrong_page_does_not_match() -> None:
    gold = [_seq_gold("RRRQRRKKRG", page=9)]
    other_page, index = _candidate_with_page("RRRQRRKKRG", 3)
    result = match_sequences(gold, [other_page], scope=["doc"], evidence_index=index)
    assert result.missed == 1
    assert result.false_positives == 1


def test_author_false_positive_participates_in_fp() -> None:
    """Candidates the rules did NOT identify (author names) still count."""

    gold = [_seq_gold("AGLK", seq_id="1", page=1)]
    good = _seq_candidate("AGLK", seq_id="1")
    author = _seq_candidate("CHRISTIANWIDMANN", seq_id=None, page=1)
    result = match_sequences(gold, [good, author], scope=["doc"])
    assert result.exact_matches == 1
    assert result.false_positives == 1  # the author name


def test_wrong_sequence_same_number_exact_fp_and_fn() -> None:
    gold = [_seq_gold("AGLK", seq_id="7")]
    wrong = _seq_candidate("AGLA", seq_id="7")
    result = match_sequences(gold, [wrong], scope=["doc"])
    assert result.exact_matches == 0
    assert result.detected == 1  # number found
    assert result.missed == 1  # content wrong → exact FN
    assert result.false_positives == 1  # the candidate never became exact TP


def test_measurement_wrong_entity_is_fp_and_fn() -> None:
    gold = [
        Annotation(
            annotation_id="g1",
            document_id="doc",
            page_number=1,
            kind="measurement",
            metric="MIC",
            entity_raw="P1",
            target_raw="E. coli",
            value_raw="8",
            unit_raw="ug/mL",
            human_verified=True,
            status="verified",
        )
    ]
    candidate = Measurement(
        measurement_id="m1",
        document_id="doc",
        entity_raw="P2",  # right value, wrong entity
        metric="MIC",
        value=ActivityValue(kind="scalar", number="8", raw="8"),
        unit_raw="ug/mL",
        target_raw="E. coli",
        evidence_ids=["ev"],
    )
    result = match_measurements(gold, [candidate], scope=["doc"])
    assert result.correct == 0
    assert result.missed == 1
    assert result.false_positives == 1
    assert result.wrong_value == 0  # it was a key miss, not a value mismatch
