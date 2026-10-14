"""B05: per-mention activity judgement (post-M1 review counterexamples).

The review reproduced these against the current implementation:

- ``Figure 1 shows an IC50 of 5 nM.`` → 0 results (figure vetoed the whole
  sentence as metadata);
- ``Compound A had an IC50 of 1.2 ± 0.3 nM (mean ± SD).`` → 0 results
  (SD treated as a statistic veto);
- ``Cells were treated with 20 µM peptide A, reducing viability to 5%.`` →
  0 results (dose and the real result killed together);
- ``The MIC of peptide A was less than 10 µM.`` → unknown/claim_threshold
  (a paper's experimental upper bound must stay a result with an operator).

Also: role=undetermined produced formal ``unknown`` Measurements, and
RawMentions were plain dicts instead of the contract object.
"""

from __future__ import annotations

import pytest

from bio_pdf.contracts.extraction import RawMention
from bio_pdf.extract.activities.prose import extract_prose_measurements


def _run(text: str):
    mentions: list[RawMention] = []
    records = extract_prose_measurements(
        [{"text": text, "page_number": 1}], "sha256:x", raw_mentions=mentions
    )
    return records, mentions


def test_figure_number_is_not_a_sentence_veto() -> None:
    records, _ = _run("Figure 1 shows an IC50 of 5 nM.")
    assert len(records) == 1
    assert records[0].metric == "IC50"
    assert records[0].value.number == "5"
    assert records[0].unit_raw == "nM"


def test_mean_sd_qualifies_the_error_not_the_measurement() -> None:
    records, _ = _run("Compound A had an IC50 of 1.2 ± 0.3 nM (mean ± SD).")
    assert len(records) == 1
    v = records[0].value
    assert v.kind == "plus_minus" and v.number == "1.2" and v.uncertainty == "0.3"


def test_dose_condition_and_result_are_separated_in_one_sentence() -> None:
    records, mentions = _run(
        "Cells were treated with 20 µM peptide A, reducing viability to 5%."
    )
    # The dose is a condition mention; the viability drop is the result.
    assert any(m.role == "condition" and "20" in m.text for m in mentions)
    results = [r for r in records if r.metric == "viability"]
    assert len(results) == 1
    assert results[0].value.number == "5"
    assert results[0].unit_raw == "%"
    # Two separate mentions, each with its own evidence.
    assert records[0].evidence_ids != [
        m.evidence_ids[0] for m in mentions if m.role == "condition"
    ]


def test_experimental_upper_bound_is_a_result_not_a_claim() -> None:
    records, _ = _run("The MIC of peptide A was less than 10 µM.")
    assert len(records) == 1
    assert records[0].metric == "MIC"
    assert records[0].value.operator == "<"
    assert records[0].value.number == "10"
    assert records[0].evidence_kind != "claim_threshold"


def test_patent_claim_stays_a_claim() -> None:
    records, _ = _run("Claim 1: peptide A has MIC less than 10 µM.")
    assert len(records) == 1
    assert records[0].evidence_kind == "claim_threshold"


def test_confidence_interval_is_statistic_not_measurement() -> None:
    records, mentions = _run("The 95% confidence interval was 3 to 8 nM.")
    assert records == []
    assert any(m.role == "statistic" for m in mentions)


def test_undetermined_produces_mention_not_unknown_measurement() -> None:
    records, mentions = _run("The sample contained 20 µM compound.")
    # No recognized metric → typed mention, NOT a formal unknown Measurement.
    assert records == []
    assert any(isinstance(m, RawMention) for m in mentions)
    for mention in mentions:
        assert mention.mention_id and mention.evidence_ids and mention.role


def test_raw_mentions_are_contract_objects_with_evidence() -> None:
    _, mentions = _run("Cells were treated with 20 µM peptide A for 2 h.")
    assert mentions, "expected at least one mention"
    for mention in mentions:
        assert isinstance(mention, RawMention)
        assert mention.mention_id.startswith("rm-")
        assert mention.evidence_ids
        assert mention.reason_codes


def test_viability_percent_resolves_canonical_metric() -> None:
    records, _ = _run("peptide A reduced viability to 95% after 2 h.")
    assert len(records) == 1
    assert records[0].metric == "viability"


def test_metric_follows_entity_phrase() -> None:
    records, _ = _run("The IC50 values of the compounds were <1 nM.")
    assert len(records) == 1
    assert records[0].value.operator == "<"
