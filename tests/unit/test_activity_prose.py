"""Fixed regression tables from plan T07/T08 (inline sequences, prose roles).

These cases come verbatim from the optimization plan's acceptance tables and
must not be weakened to preserve old (wrong) behaviour.
"""

from __future__ import annotations

import pytest

from bio_pdf.extract.activities.prose import extract_prose_measurements
from bio_pdf.extract.sequences.inline import extract_inline_sequences


def _block(text: str, *, role: str = "body", kind: str = "text", page: int = 1) -> dict:
    return {"text": text, "kind": kind, "semantic_role": role, "page_number": page}


# ---- T07: sequence candidate rules ----------------------------------------

def test_t07_author_is_not_a_sequence() -> None:
    diagnostics: list[dict[str, str]] = []
    records = extract_inline_sequences(
        [_block("Christian Widmann", role="author")],
        "doc",
        diagnostics=diagnostics,
    )
    assert records == []
    assert any(d["code"] == "NON_SEQUENCE_ROLE" for d in diagnostics) or diagnostics == []


def test_t07_drug_and_strain_names_are_not_sequences() -> None:
    records = extract_inline_sequences(
        [
            _block("Benzylpenicillin", kind="table", page=8),
            _block("Cefuroxime axetil", kind="table", page=8),
            _block("Staphylococcus capitis", kind="table", page=8),
        ],
        "doc",
    )
    assert records == []


def test_t07_prose_short_peptides_with_context_are_candidates() -> None:
    records = extract_inline_sequences(
        [
            _block("we used the HIV TAT protein (RRRQRRKKRG) in this study.", page=2),
            _block("human RasGAP protein (DTRLNTVWMW) was examined.", page=9),
        ],
        "doc",
    )
    sequences = {r.normalized_sequence for r in records}
    assert "RRRQRRKKRG" in sequences
    assert "DTRLNTVWMW" in sequences
    for r in records:
        assert r.molecule_type == "protein"
        assert r.sequence_scope == "fragment"
        assert not r.is_complete  # a fragment is never silently complete


def test_t07_dna_with_hint_keeps_raw_case_but_normalizes_upper() -> None:
    records = extract_inline_sequences(
        [_block("The DNA sequence acgtacgtac was synthesized.")], "doc"
    )
    assert len(records) == 1
    assert records[0].molecule_type == "DNA"
    assert records[0].normalized_sequence == "ACGTACGTAC"


def test_t07_context_free_run_stays_a_mention() -> None:
    diagnostics: list[dict[str, str]] = []
    mentions: list[dict] = []
    records = extract_inline_sequences(
        [_block("AAAAACCCCCGGGGGTTTTT")],
        "doc",
        diagnostics=diagnostics,
        raw_mentions=mentions,
    )
    assert records == []
    assert any(d["code"] == "NO_SEQUENCE_CONTEXT" for d in diagnostics)
    assert mentions and mentions[0].role == "unclassified_letters"


def test_t07_digit_in_run_is_an_unresolved_position() -> None:
    records = extract_inline_sequences(
        [_block("The peptide (RRRQRRK1RG) was tested.")], "doc"
    )
    assert len(records) == 1
    record = records[0]
    assert record.normalized_sequence == "RRRQRRK?RG"  # 1 kept as a position
    assert record.observed_length == 10
    assert any(r["token"] == "1" and r["resolved"] == "false" for r in record.residues)


def test_t07_seq_id_reference_is_not_residues() -> None:
    records = extract_inline_sequences(
        [_block("at least 90% sequence identity to SEQ ID NO:7")], "doc"
    )
    assert records == []


# ---- T08: prose measurement roles -----------------------------------------

def _prose(text: str) -> tuple[list, list[dict]]:
    mentions: list[dict] = []
    records = extract_prose_measurements(
        [{"text": text, "page_number": 1}], "sha256:x", raw_mentions=mentions
    )
    return records, mentions


def test_t08_date_is_not_a_measurement() -> None:
    records, mentions = _prose("Accepted: 17 May 2017")
    assert records == []
    assert mentions == [] or all(m["role"] != "result" for m in mentions)


def test_t08_ic50_with_operator_full_tuple() -> None:
    records, _ = _prose("compound A had an IC50 of >128 ug/mL against target B")
    assert len(records) == 1
    v = records[0].value
    assert (v.kind, v.operator, v.number) == ("scalar", ">", "128")
    assert records[0].unit_raw == "ug/mL"
    assert records[0].metric == "IC50"


def test_t08_range_is_not_truncated() -> None:
    records, _ = _prose("MIC was 1-10 nM")
    assert len(records) == 1
    v = records[0].value
    assert v.kind == "range" and v.lower == "1" and v.upper == "10"


def test_t08_plus_minus_is_not_truncated() -> None:
    records, _ = _prose("IC50 = 1.2 ± 0.3 nM")
    assert len(records) == 1
    v = records[0].value
    assert v.kind == "plus_minus" and v.number == "1.2" and v.uncertainty == "0.3"


def test_t08_treatment_dose_is_a_condition_not_a_result() -> None:
    records, mentions = _prose("cells were treated with 20 uM peptide A for 2 h")
    assert records == []
    assert any(m.role == "condition" for m in mentions)


def test_t08_confidence_interval_is_a_statistic() -> None:
    records, mentions = _prose("The 95% confidence interval was computed.")
    assert records == []
    assert any(m.role == "statistic" for m in mentions)


def test_t08_viability_result_is_kept() -> None:
    records, _ = _prose("peptide A reduced viability to 95% after 2 h")
    assert len(records) == 1  # a viability RESULT

def test_t08_claim_threshold_stays_a_claim() -> None:
    records, _ = _prose("Claim 1: peptide A has MIC less than 10 uM")
    assert len(records) == 1
    assert records[0].evidence_kind == "claim_threshold"


def test_t08_bare_M_does_not_match_words() -> None:
    records, mentions = _prose("The Mouse Model was used in May")
    assert records == []
