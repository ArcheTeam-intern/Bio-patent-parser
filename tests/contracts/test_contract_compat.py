"""Contract compatibility tests (plan T01 acceptance).

- legacy 1.0.0 payloads load with explicit defaults,
- legacy string entity_id moves to entity_raw without inventing linkage,
- new EvidenceSpan round-trips multi-page fragments,
- unknown fields still error, unknown MAJOR version is rejected,
- empty table cells are representable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bio_pdf.contracts import (
    EvidenceFragment,
    EvidenceSpan,
    Measurement,
    SequenceRecord,
    TableCell,
)
from bio_pdf.contracts.compat import (
    load_evidence_span_dict,
    load_measurement,
    load_records_payload,
)
from bio_pdf.config import load_workflow_config
from bio_pdf.errors import WorkflowError


def test_legacy_measurement_entity_id_moves_to_entity_raw() -> None:
    legacy = {
        "measurement_id": "m-1",
        "document_id": "sha256:x",
        "entity_id": "Benzylpenicillin",  # legacy row-label-as-entity
        "metric": "value",
        "value": {"kind": "categorical", "qualifier": "S", "raw": "S"},
        "evidence_ids": ["ev-1"],
    }
    loaded = load_measurement(legacy)
    assert loaded.entity_id is None
    assert loaded.entity_raw == "Benzylpenicillin"


def test_new_measurement_fields_round_trip() -> None:
    m = Measurement(
        measurement_id="m-2",
        document_id="sha256:x",
        entity_raw="P1",
        metric="MIC",
        metric_raw="MIC50",
        value={"kind": "scalar", "operator": ">", "number": "128", "raw": ">128"},
        unit_raw="µg/mL",
        target_raw="E. coli",
        evidence_ids=["ev"],
    )
    data = json.loads(m.model_dump_json())
    again = Measurement.model_validate(data)
    assert again.metric_raw == "MIC50"
    assert again.target_id is None


def test_legacy_span_gets_one_fragment_and_precision() -> None:
    legacy = {
        "evidence_id": "ev-1",
        "document_id": "sha256:x",
        "page_number": 3,
        "bbox": [10.0, 20.0, 100.0, 30.0],
        "raw_text": "some hit",
    }
    span = load_evidence_span_dict(legacy)
    assert len(span.fragments) == 1
    assert span.fragments[0].page_number == 3
    assert span.location_precision == "block"


def test_multipage_span_round_trip() -> None:
    span = EvidenceSpan(
        evidence_id="ev-2",
        document_id="sha256:x",
        raw_text="part one part two",
        fragments=[
            EvidenceFragment(page_number=2, bbox=(1, 2, 3, 4), char_start=0, char_end=9),
            EvidenceFragment(page_number=3, bbox=(5, 6, 7, 8), char_start=9, char_end=17),
        ],
        location_precision="block",
    )
    data = json.loads(span.model_dump_json())
    again = EvidenceSpan.model_validate(data)
    assert len(again.fragments) == 2
    assert again.fragments[1].page_number == 3


def test_empty_table_cell_is_representable() -> None:
    cell = TableCell(cell_id="c1", row_index=1, column_index=2, text_raw="")
    assert cell.text_raw == ""


def test_unknown_fields_still_rejected() -> None:
    with pytest.raises(Exception):
        SequenceRecord.model_validate(
            {
                "sequence_id": "s",
                "document_id": "sha256:x",
                "molecule_type": "protein",
                "representation": "one_letter",
                "raw_sequence": "AG",
                "normalized_sequence": "AG",
                "evidence_ids": ["e"],
                "not_a_real_field": 1,
            }
        )


def test_unknown_major_version_rejected(tmp_path: Path) -> None:
    payload = {
        "schema_version": "2.0.0",
        "sequences": [],
        "measurements": [],
    }
    path = tmp_path / "validation.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(WorkflowError, match="unsupported schema major"):
        load_records_payload(path)


def test_legacy_records_payload_loads(tmp_path: Path) -> None:
    payload = {
        "schema_version": "1.0.0",
        "sequences": [
            {
                "sequence_id": "s1",
                "document_id": "sha256:x",
                "molecule_type": "protein",
                "representation": "one_letter",
                "raw_sequence": "AGLK",
                "normalized_sequence": "AGLK",
                "evidence_ids": ["e1"],
            }
        ],
        "measurements": [
            {
                "measurement_id": "m1",
                "document_id": "sha256:x",
                "entity_id": "P1",
                "metric": "MIC",
                "value": {"kind": "scalar", "number": "8", "raw": "8"},
                "unit_raw": "ug/mL",
                "evidence_ids": ["e2"],
            }
        ],
    }
    path = tmp_path / "validation.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    sequences, measurements = load_records_payload(path)
    assert len(sequences) == 1
    assert measurements[0].entity_raw == "P1"
    assert measurements[0].entity_id is None


def test_workflow_config_from_toml_and_unknown_key_rejected(tmp_path: Path) -> None:
    cfg_file = tmp_path / "wf.toml"
    cfg_file.write_text(
        'parser = "docling_sdk"\nocr_policy = "force"\nreparse_max_regions = 5\n',
        encoding="utf-8",
    )
    cfg = load_workflow_config(cfg_file)
    assert cfg.parser == "docling_sdk"
    assert cfg.ocr_policy == "force"
    assert cfg.reparse_max_regions == 5

    bad = tmp_path / "bad.toml"
    bad.write_text("parsser = 'native'\n", encoding="utf-8")
    with pytest.raises(WorkflowError, match="unknown keys"):
        load_workflow_config(bad)
