"""Deterministic JSON Schema export for the contract package.

The schemas are the stable handoff for Go/TS implementations (Task 17).
Output ordering is important: ``sorted_keys=True`` and consistent list
serialisation keep diffs readable across releases.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from bio_pdf.contracts.common import (
    SCHEMA_VERSION,
    EvidenceSpan,
)
from bio_pdf.contracts.document import (
    DocumentInspection,
    EvidenceImage,
    NormalizedDocument,
    NormalizedPage,
    PageGeometry,
    PageQuality,
    Provenance,
    QualityConfig,
    RouteDecision,
    Table,
    TableCell,
    TextBlock,
)
from bio_pdf.contracts.job import (
    AcceptanceDecision,
    ArtifactRef,
    Job,
    ReviewDecision,
    Stage,
    StageOutcome,
    ValidationResult,
)
from bio_pdf.contracts.records import (
    ActivityValue,
    CheckResult,
    EvidenceBundle,
    EvidenceRef,
    Measurement,
    SequenceRecord,
    SequenceReference,
    SequenceRelation,
)

# Explicit list so the export order is deterministic across runs (independent
# of module import order or future additions).
EXPORTED_MODELS: tuple[type[BaseModel], ...] = (
    # common
    EvidenceSpan,
    # document
    PageGeometry,
    DocumentInspection,
    QualityConfig,
    PageQuality,
    RouteDecision,
    Provenance,
    TextBlock,
    TableCell,
    Table,
    NormalizedPage,
    NormalizedDocument,
    EvidenceImage,
    # records
    CheckResult,
    EvidenceRef,
    SequenceRecord,
    SequenceReference,
    SequenceRelation,
    ActivityValue,
    Measurement,
    EvidenceBundle,
    # job
    ArtifactRef,
    Stage,
    Job,
    StageOutcome,
    ValidationResult,
    AcceptanceDecision,
    ReviewDecision,
)


def export_models() -> dict[str, Any]:
    """Return a deterministic mapping of model name → JSON Schema dict.

    The schemas are produced via :meth:`BaseModel.model_json_schema` with
    ``mode="validation"`` so required/nullable constraints are explicit.
    """

    out: dict[str, Any] = {}
    for model in EXPORTED_MODELS:
        schema = model.model_json_schema(mode="validation")
        # Force stable ordering of property keys for diff-friendly output.
        schema = json.loads(json.dumps(schema, sort_keys=True))
        out[model.__name__] = schema

    return {
        "schema_version": SCHEMA_VERSION,
        "models": out,
    }


def write_schemas(target_dir: Path) -> dict[str, Path]:
    """Write each model schema to ``target_dir/<Model>.schema.json``.

    Returns a mapping of model name → written path. The function creates
    ``target_dir`` if missing.
    """

    target_dir.mkdir(parents=True, exist_ok=True)
    exported = export_models()
    written: dict[str, Path] = {}
    for name, schema in sorted(exported["models"].items()):
        path = target_dir / f"{name}.schema.json"
        path.write_text(
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        written[name] = path
    return written


def write_bundle(target_dir: Path) -> Path:
    """Write the full bundle as ``target_dir/contracts.schema.json``."""

    target_dir.mkdir(parents=True, exist_ok=True)
    bundle = export_models()
    path = target_dir / "contracts.schema.json"
    path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path