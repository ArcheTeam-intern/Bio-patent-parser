"""Export module tests (JSON, CSV, review HTML)."""

from __future__ import annotations

import csv
import io
import json

from bio_pdf.contracts import ActivityValue, Measurement, SequenceRecord
from bio_pdf.export.csv_export import (
    export_measurements_csv,
    export_sequences_csv,
)
from bio_pdf.export.json_export import export_result_json
from bio_pdf.export.review_html import render_review_html


def _sequence(acceptance: str = "candidate") -> SequenceRecord:
    return SequenceRecord(
        sequence_id="seq-1",
        document_id="sha256:doc",
        seq_id_no="1",
        molecule_type="protein",
        representation="one_letter",
        declared_length=4,
        observed_length=4,
        raw_sequence="MKTG",
        normalized_sequence="MKTG",
        is_complete=True,
        evidence_ids=["ev-1"],
        acceptance_state=acceptance,  # type: ignore[arg-type]
    )


def _measurement(acceptance: str = "candidate") -> Measurement:
    return Measurement(
        measurement_id="meas-1",
        document_id="sha256:doc",
        entity_id="ent-1",
        metric="MIC",
        value=ActivityValue(kind="scalar", operator=">", number="128", raw=">128"),
        unit_raw="µg/mL",
        unit_normalized="ug/mL",
        target_raw="E. coli",
        evidence_kind="measured",
        evidence_ids=["ev-2"],
        acceptance_state=acceptance,  # type: ignore[arg-type]
    )


def test_result_json_accepted_only_filters() -> None:
    result = export_result_json(
        [_sequence("accepted"), _sequence("candidate")],
        [_measurement("accepted")],
        accepted_only=True,
    )
    assert result["summary"]["sequences_total"] == 2
    assert result["summary"]["sequences_accepted"] == 1
    assert len(result["sequences"]) == 1  # only accepted


def test_result_json_reports_counts_not_no_sequences() -> None:
    result = export_result_json([], [], accepted_only=True)
    assert result["summary"]["sequences_total"] == 0
    # The summary is present even with zero accepted sequences.
    assert "sequences_total" in result["summary"]


def test_sequences_csv_has_full_columns() -> None:
    text = export_sequences_csv([_sequence()])
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    assert rows[0]["raw_sequence"] == "MKTG"
    assert rows[0]["normalized_sequence"] == "MKTG"
    assert rows[0]["acceptance_state"] == "candidate"
    assert "raw_sequence" in reader.fieldnames


def test_measurements_csv_has_operator_unit_entity() -> None:
    text = export_measurements_csv([_measurement()])
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    assert rows[0]["operator"] == ">"
    assert rows[0]["unit_raw"] == "µg/mL"
    assert rows[0]["entity_id"] == "ent-1"
    assert rows[0]["metric"] == "MIC"


def test_review_html_escapes_raw_text() -> None:
    html_text = render_review_html([
        {"record_id": "<script>alert(1)</script>", "reason_codes": ["<b>bad</b>"], "evidence_ids": ["ev"]}
    ])
    assert "<script>" not in html_text
    assert "&lt;script&gt;" in html_text
    assert "&lt;b&gt;" in html_text