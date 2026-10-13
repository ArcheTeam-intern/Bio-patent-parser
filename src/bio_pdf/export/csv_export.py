"""CSV export for sequences and measurements.

The columns keep the raw, normalised and structured fields side by side so a
reviewer can audit the parse; they are not collapsed into a two-column
``sequence,value`` dump.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from bio_pdf.contracts import Measurement, SequenceRecord


def export_sequences_csv(sequences: list[SequenceRecord]) -> str:
    """Return a CSV string for sequence records."""

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "sequence_id",
            "seq_id_no",
            "molecule_type",
            "raw_sequence",
            "normalized_sequence",
            "declared_length",
            "observed_length",
            "is_complete",
            "validation_state",
            "acceptance_state",
        ]
    )
    for s in sequences:
        writer.writerow(
            [
                s.sequence_id,
                s.seq_id_no or "",
                s.molecule_type,
                s.raw_sequence,
                s.normalized_sequence,
                s.declared_length if s.declared_length is not None else "",
                s.observed_length if s.observed_length is not None else "",
                "true" if s.is_complete else "false",
                s.validation_state,
                s.acceptance_state,
            ]
        )
    return buffer.getvalue()


def export_measurements_csv(measurements: list[Measurement]) -> str:
    """Return a CSV string for measurement records."""

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "measurement_id",
            "entity_id",
            "metric",
            "value_raw",
            "value_kind",
            "operator",
            "number",
            "unit_raw",
            "unit_normalized",
            "target_raw",
            "assay_id",
            "evidence_kind",
            "validation_state",
            "acceptance_state",
        ]
    )
    for m in measurements:
        writer.writerow(
            [
                m.measurement_id,
                m.entity_id or "",
                m.metric,
                m.value.raw,
                m.value.kind,
                m.value.operator or "",
                m.value.number or "",
                m.unit_raw or "",
                m.unit_normalized or "",
                m.target_raw or "",
                m.assay_id or "",
                m.evidence_kind,
                m.validation_state,
                m.acceptance_state,
            ]
        )
    return buffer.getvalue()


__all__ = ["export_sequences_csv", "export_measurements_csv"]