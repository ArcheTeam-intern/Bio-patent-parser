"""Compatibility readers for pre-1.1.0 domain artifacts.

Old jobs on disk carry ``schema_version="1.0.0"`` payloads without the new
fields (semantic roles, evidence fragments, entity_raw …). Compat loading
fills explicit defaults — it never invents facts:

- legacy string ``entity_id`` holding a row label → moved to ``entity_raw``
  with ``entity_id=None`` (a row label is NOT a linked entity);
- legacy EvidenceSpan without fragments → one fragment carrying the old
  page/bbox, ``location_precision`` derived from what actually exists;
- missing new optional fields → their documented defaults.

Reading an unknown MAJOR version raises ``SCHEMA_UNSUPPORTED`` — silently
ignoring unknown shapes would corrupt evidence chains.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypeVar

from pydantic import ValidationError

from bio_pdf.contracts.common import EvidenceSpan
from bio_pdf.contracts.records import Measurement, SequenceRecord
from bio_pdf.errors import WorkflowError

SUPPORTED_MAJOR = "1"

T = TypeVar("T")


def _check_version(schema_version: str | None, *, artifact: str) -> None:
    if schema_version is None:
        return  # pre-versioning payloads default to the oldest supported
    major = schema_version.split(".")[0]
    if major != SUPPORTED_MAJOR:
        raise WorkflowError(
            f"{artifact} has unsupported schema major version: {schema_version}",
            details={
                "schema_version": schema_version,
                "supported": f"{SUPPORTED_MAJOR}.x",
            },
        )


def _load_payload(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(
            f"cannot read artifact {path}: {exc}",
            details={"path": str(path)},
        ) from exc


def _span_with_fragments(data: dict[str, Any]) -> dict[str, Any]:
    """Ensure a span payload has fragments derived from legacy top-level box."""

    if data.get("fragments"):
        return data
    if data.get("page_number") is not None or data.get("bbox") is not None:
        data["fragments"] = [
            {
                "page_number": data.get("page_number") or 1,
                "bbox": data.get("bbox"),
                "char_start": data.get("char_start"),
                "char_end": data.get("char_end"),
                "raw_text": data.get("raw_text"),
            }
        ]
    precision = data.get("location_precision") or "none"
    if precision == "none":
        if data.get("bbox") is not None:
            precision = "block"
        elif data.get("page_number") is not None:
            precision = "page"
        data["location_precision"] = precision
    return data


def load_evidence_span(path: Path, index: int = 0) -> EvidenceSpan:
    """Load one EvidenceSpan from a JSON artifact with compat mapping."""

    data = _load_payload(path)
    _check_version(data.get("schema_version"), artifact=str(path))
    spans = data.get("evidence_spans") or data.get("spans") or []
    if index >= len(spans):
        raise WorkflowError(
            f"evidence span index {index} out of range in {path}",
            details={"count": len(spans)},
        )
    return load_evidence_span_dict(spans[index])


def load_evidence_span_dict(data: dict[str, Any]) -> EvidenceSpan:
    _span_with_fragments(data)
    return EvidenceSpan.model_validate(data)


def _fix_legacy_entity(record: dict[str, Any]) -> None:
    """Move a non-entity row label out of entity_id (never invent linkage)."""

    entity_id = record.get("entity_id")
    if entity_id and not str(entity_id).startswith("ent:"):
        if not record.get("entity_raw"):
            record["entity_raw"] = str(entity_id)
        record["entity_id"] = None


def load_measurement(data: dict[str, Any]) -> Measurement:
    _check_version(data.get("schema_version"), artifact="measurement record")
    data = dict(data)
    _fix_legacy_entity(data)
    return Measurement.model_validate(data)


def load_sequence(data: dict[str, Any]) -> SequenceRecord:
    _check_version(data.get("schema_version"), artifact="sequence record")
    return SequenceRecord.model_validate(data)


def load_records_payload(
    path: Path,
) -> tuple[list[SequenceRecord], list[Measurement]]:
    """Load a validation.json / extract.json-style payload with compat.

    Returns ``(sequences, measurements)``. Records that fail strict
    validation after compat mapping raise — the caller decides whether the
    artifact is unusable, defaults are never forced through.
    """

    data = _load_payload(path)
    _check_version(data.get("schema_version"), artifact=str(path))

    sequences: list[SequenceRecord] = []
    for raw in data.get("sequences", []):
        item = dict(raw)
        item.pop("review_reasons", None)
        try:
            sequences.append(load_sequence(item))
        except ValidationError as exc:
            raise WorkflowError(
                f"legacy sequence record failed compat validation in {path}",
                details={"sequence_id": str(item.get("sequence_id")), "error": str(exc)},
            ) from exc

    measurements: list[Measurement] = []
    for raw in data.get("measurements", []):
        item = dict(raw)
        item.pop("review_reasons", None)
        try:
            measurements.append(load_measurement(item))
        except ValidationError as exc:
            raise WorkflowError(
                f"legacy measurement record failed compat validation in {path}",
                details={
                    "measurement_id": str(item.get("measurement_id")),
                    "error": str(exc),
                },
            ) from exc

    return sequences, measurements


__all__ = [
    "load_evidence_span",
    "load_evidence_span_dict",
    "load_measurement",
    "load_sequence",
    "load_records_payload",
]
