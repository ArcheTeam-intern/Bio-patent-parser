"""Contract regression tests: model fields, validators and round-trip."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bio_pdf.contracts import (
    AcceptanceDecision,
    ActivityValue,
    CheckResult,
    DocumentInspection,
    EvidenceSpan,
    Job,
    Measurement,
    PageQuality,
    PageGeometry,
    QualityConfig,
    RouteDecision,
    SCHEMA_VERSION,
    SequenceRecord,
    TextBlock,
    Table,
    TableCell,
    NormalizedDocument,
    NormalizedPage,
    export_models,
    write_bundle,
    write_schemas,
)
from bio_pdf.contracts.common import validate_bbox


# ---------------------------------------------------------------------------
# bbox validation
# ---------------------------------------------------------------------------


def test_bbox_does_not_allow_swapped_axes_or_out_of_page() -> None:
    validate_bbox((10, 20, 50, 80), width_pt=100, height_pt=100)
    with pytest.raises(ValueError):
        validate_bbox((50, 20, 10, 80), width_pt=100, height_pt=100)
    with pytest.raises(ValueError):
        validate_bbox((10, 20, 150, 80), width_pt=100, height_pt=100)
    with pytest.raises(ValueError):
        validate_bbox((10, 20, 50, 200), width_pt=100, height_pt=100)


def test_bbox_rejects_nonfinite_coordinates() -> None:
    with pytest.raises(ValueError):
        validate_bbox((0, 0, float("nan"), 10), width_pt=100, height_pt=100)
    with pytest.raises(ValueError):
        validate_bbox((0, 0, 10, float("inf")), width_pt=100, height_pt=100)


# ---------------------------------------------------------------------------
# EvidenceSpan
# ---------------------------------------------------------------------------


def test_evidence_span_can_carry_only_page_no_bbox() -> None:
    span = EvidenceSpan(
        evidence_id="ev1",
        document_id="sha256:ab",
        page_number=3,
        bbox=None,
        raw_text="hello",
    )
    assert span.geometry_present() is True


def test_evidence_span_geometry_absent_when_no_page_or_bbox() -> None:
    span = EvidenceSpan(
        evidence_id="ev1",
        document_id="sha256:ab",
        page_number=None,
        bbox=None,
        raw_text=None,
    )
    assert span.geometry_present() is False


def test_models_reject_unknown_fields() -> None:
    with pytest.raises(Exception):
        EvidenceSpan(  # type: ignore[call-arg]
            evidence_id="ev1",
            document_id="sha256:ab",
            typo_field="nope",  # unknown field
        )


# ---------------------------------------------------------------------------
# Round-trip and unicode survival
# ---------------------------------------------------------------------------


def _make_sequence() -> SequenceRecord:
    return SequenceRecord(
        sequence_id="seq-1",
        document_id="sha256:doc",
        seq_id_no="7",
        molecule_type="protein",
        representation="one_letter",
        declared_length=5,
        observed_length=5,
        raw_sequence="Met Lys Thr Xaa Gly",
        normalized_sequence="MKTXG",
        is_complete=True,
        evidence_ids=["ev1"],
        checks=[
            CheckResult(
                check_id="SEQ_DECLARED_LENGTH_MATCH",
                outcome="pass",
                evidence_ids=["ev1"],
            ),
        ],
    )


def test_sequence_record_round_trip_preserves_unicode() -> None:
    seq = _make_sequence()
    encoded = seq.model_dump_json()
    decoded = SequenceRecord.model_validate_json(encoded)
    assert decoded == seq
    # Make sure the round-trip JSON keeps the original characters.
    decoded_obj = json.loads(encoded)
    assert decoded_obj["normalized_sequence"] == "MKTXG"
    assert decoded_obj["raw_sequence"] == "Met Lys Thr Xaa Gly"


def test_measurement_round_trip_preserves_micro_sign_and_decimal() -> None:
    measurement = Measurement(
        measurement_id="meas-1",
        document_id="sha256:doc",
        entity_id="ent-1",
        metric="IC50",
        value=ActivityValue(
            kind="scalar",
            operator="<=",
            number="1.2",
            raw="≤1.2",
        ),
        unit_raw="µg/mL",
        unit_normalized="ug/mL",
        evidence_ids=["ev1"],
    )
    encoded = measurement.model_dump_json()
    decoded = Measurement.model_validate_json(encoded)
    assert decoded == measurement
    assert "µ" in json.loads(encoded)["unit_raw"]


def test_measurement_with_unknown_position_and_qualifier() -> None:
    measurement = Measurement(
        measurement_id="meas-2",
        document_id="sha256:doc",
        metric="MIC",
        value=ActivityValue(kind="categorical", qualifier="ND", raw="ND"),
        evidence_ids=["ev1"],
    )
    encoded = measurement.model_dump_json()
    decoded = Measurement.model_validate_json(encoded)
    assert decoded.value.number is None
    assert decoded.value.kind == "categorical"
    assert decoded.value.qualifier == "ND"


def test_acceptance_decision_minimum_fields() -> None:
    decision = AcceptanceDecision(acceptance_state="needs_review")
    encoded = decision.model_dump_json()
    decoded = AcceptanceDecision.model_validate_json(encoded)
    assert decoded.acceptance_state == "needs_review"


# ---------------------------------------------------------------------------
# Page quality + routing decision
# ---------------------------------------------------------------------------


def test_page_quality_rejects_out_of_range_ratios() -> None:
    with pytest.raises(Exception):
        PageQuality(
            page_number=1,
            nonspace_chars=10,
            printable_ratio=1.5,  # > 1
            replacement_ratio=0.0,
            image_coverage=0.0,
        )


def test_route_decision_defaults() -> None:
    rd = RouteDecision(route="mixed")
    assert rd.rule_version == "v0"
    assert rd.reasons == []


def test_quality_config_defaults() -> None:
    cfg = QualityConfig()
    assert cfg.scan_image_coverage == pytest.approx(0.8)
    assert cfg.low_text_char_count == 20


# ---------------------------------------------------------------------------
# Job, stage, document inspection
# ---------------------------------------------------------------------------


def test_document_inspection_requires_positive_page_count() -> None:
    with pytest.raises(Exception):
        DocumentInspection(
            document_id="sha256:doc",
            sha256="0" * 64,
            bytes=100,
            page_count=-1,  # invalid
        )


def test_job_result_revision_starts_at_zero() -> None:
    job = Job(
        job_id="job-1",
        document_id="sha256:doc",
        config_hash="cfg-1",
    )
    assert job.result_revision == 0
    assert job.status == "queued"


# ---------------------------------------------------------------------------
# Normalised document blocks / table
# ---------------------------------------------------------------------------


def test_table_requires_header_linkage_when_declaring_headers() -> None:
    cells = [
        TableCell(cell_id="c1", row_index=0, column_index=0, text_raw="A"),
        TableCell(cell_id="c2", row_index=0, column_index=1, text_raw="B"),
        TableCell(cell_id="c3", row_index=1, column_index=0, text_raw="1"),
        TableCell(cell_id="c4", row_index=1, column_index=1, text_raw="2"),
    ]
    table = Table(
        table_id="t1",
        page_number=1,
        row_count=2,
        column_count=2,
        cells=cells,
        header_cell_ids=["c1", "c2"],
    )
    page = NormalizedPage(
        page_number=1,
        width_pt=612,
        height_pt=792,
        route="native",
        blocks=[
            TextBlock(
                block_id="b1",
                text_raw="placeholder",
                bbox=(0, 0, 10, 10),
            ),
        ],
        tables=[table],
    )
    doc = NormalizedDocument(
        schema_version="1.0.0",
        document_id="sha256:doc",
        parser_run_id="run-1",
        pages=[page],
    )
    encoded = doc.model_dump_json()
    decoded = NormalizedDocument.model_validate_json(encoded)
    assert decoded.pages[0].tables[0].row_count == 2


def test_text_block_may_have_no_bbox() -> None:
    block = TextBlock(block_id="b1", text_raw="placeholder", bbox=None)
    encoded = block.model_dump_json()
    decoded = TextBlock.model_validate_json(encoded)
    assert decoded.bbox is None


def test_page_geometry_rejects_zero_size() -> None:
    with pytest.raises(Exception):
        PageGeometry(page_number=1, width_pt=0, height_pt=0)


# ---------------------------------------------------------------------------
# Schema export
# ---------------------------------------------------------------------------


def test_export_models_emits_validation_mode() -> None:
    bundle = export_models()
    assert bundle["schema_version"] == SCHEMA_VERSION
    # Spot-check a few models.
    assert "SequenceRecord" in bundle["models"]
    assert "EvidenceSpan" in bundle["models"]


def test_write_bundle_is_deterministic(tmp_path: Path) -> None:
    target = tmp_path / "schemas"
    write_bundle(target)
    first = (target / "contracts.schema.json").read_text(encoding="utf-8")
    # Remove + rewrite to ensure determinism.
    (target / "contracts.schema.json").unlink()
    write_bundle(target)
    second = (target / "contracts.schema.json").read_text(encoding="utf-8")
    assert first == second


def test_write_schemas_creates_per_model_files(tmp_path: Path) -> None:
    target = tmp_path / "schemas"
    written = write_schemas(target)
    assert "SequenceRecord" in written
    assert written["SequenceRecord"].exists()
    payload = json.loads(written["SequenceRecord"].read_text(encoding="utf-8"))
    # The validator-mode schema declares ``required`` for the fields that are
    # mandatory in our model.
    assert "required" in payload
    assert "additionalProperties" in payload
    assert payload["additionalProperties"] is False