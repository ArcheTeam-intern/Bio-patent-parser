"""Workflow configuration: the single source of effective job settings.

``WorkflowConfig`` is loaded from a TOML file (strictly validated), hashed,
and persisted with every job as ``effective_config`` so a rerun can be
reproduced byte-for-byte. The legacy ``options_ref`` string remains as a
pointer for humans; the persisted config is what stages actually read.

Unknown TOML keys are rejected (typos must fail loudly). Every knob here is
a *policy* pending validation — none of them claims accuracy.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from bio_pdf.contracts.common import _StrictModel
from bio_pdf.errors import WorkflowError

#: Bump when default semantics change so effective_config hashes move.
CONFIG_VERSION = "3"

OcrPolicy = Literal["auto", "force", "disabled"]
DocumentTypeHint = Literal["auto", "paper", "patent"]


class RuleVersions(_StrictModel):
    """Versions of the deterministic rule packs that shape results."""

    quality_rules: str = "v0"
    sequence_rules: str = "v1"
    activity_rules: str = "v1"
    table_rules: str = "v1"
    acceptance_policy: str = "v0"


class ParserOptions(_StrictModel):
    """Engine-specific knobs, all treated as hints pending validation."""

    tableformer_mode: Literal["fast", "accurate"] | None = None
    do_table_structure: bool = True
    ocr_lang: str = "en"
    ocr_engine: Literal["auto", "tesseract", "rapidocr"] = "auto"
    engine_timeout_seconds: float = Field(default=900.0, gt=0)


class AdoptedCandidate(_StrictModel):
    """F09/G01: an explicitly ADOPTED reparse candidate.

    Adoption is not a rename of "reparse": the new job's inputs record the
    immutable candidate identity (id + content hash + META hash), its
    source SHA, the ORIGIN baseline (audit trail), the target
    page/region and the coverage policy — and the extract stage actually
    CONSUMES the candidate content as an OCR source for that page. The
    content travels under
    ``jobs/<job_id>/adopted/<candidate_id>/<artifact_name>``.

    G01/S01: ``meta_sha256`` locks the metadata file that carries the
    line boxes and text actually fed to the parser. Without it the worker
    trusted an unverified meta whose tampering changed the final residues
    while every content hash still checked out.
    """

    candidate_id: str
    content_sha256: str
    artifact_name: str = "candidate.txt"
    meta_sha256: str | None = None
    source_sha256: str | None = None
    baseline_id: str | None = None
    origin_job_id: str | None = None
    origin_issue_id: str | None = None
    page_number: int
    region: dict[str, Any] | None = None
    coverage_policy: Literal["merge-with-evidence"] = "merge-with-evidence"


class WorkflowConfig(_StrictModel):
    """The complete effective configuration of one job."""

    config_version: str = CONFIG_VERSION
    parser: Literal["native", "docling_sdk", "docling_serve", "mineru_http"] = "native"
    ocr_policy: OcrPolicy = "auto"
    pages: list[int] | None = None  # None = all pages
    targets: tuple[Literal["sequences", "activities"], ...] = (
        "sequences",
        "activities",
    )
    document_type_hint: DocumentTypeHint = "auto"

    rule_versions: RuleVersions = Field(default_factory=RuleVersions)
    parser_options: ParserOptions = Field(default_factory=ParserOptions)

    #: Bounded re-parse budget (T16); enforced before any secondary engine runs.
    reparse_max_regions: int = Field(default=10, ge=0)
    reparse_max_attempts_per_region: int = Field(default=2, ge=1)
    reparse_timeout_seconds: float = Field(default=120.0, gt=0)

    #: F09: explicitly adopted reparse candidates consumed by extract.
    adopted_candidates: list[AdoptedCandidate] = Field(default_factory=list)


def load_workflow_config(path: Path | str | None = None) -> WorkflowConfig:
    """Load a :class:`WorkflowConfig` from TOML, or return defaults.

    Unknown keys raise ``CONFIG_INVALID`` — silent typo tolerance would let
    a stale config quietly change behaviour.
    """

    if path is None:
        return WorkflowConfig()
    file = Path(path)
    if not file.is_file():
        raise WorkflowError(
            f"config file not found: {file}",
            details={"path": str(file)},
        )
    try:
        raw = tomllib.loads(file.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise WorkflowError(
            f"config file is not valid TOML: {exc}",
            details={"path": str(file)},
        ) from exc

    allowed = WorkflowConfig.model_fields.keys()
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise WorkflowError(
            f"config has unknown keys: {unknown}",
            details={"unknown": unknown, "allowed": sorted(allowed)},
        )
    try:
        return WorkflowConfig.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - surface pydantic as domain error
        raise WorkflowError(
            f"config failed validation: {exc}",
            details={"path": str(file)},
        ) from exc


def config_hash(config: WorkflowConfig) -> str:
    """Stable short hash of the effective config (content-addressed)."""

    import json

    from bio_pdf.storage.artifacts import compute_sha256_bytes

    payload = json.dumps(
        config.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
    )
    return compute_sha256_bytes(payload.encode("utf-8"))[:16]


def effective_options_for_stage(
    config: WorkflowConfig, stage_name: str
) -> dict[str, Any]:
    """Project the job config into per-stage effective options.

    Stages must not read the whole config; they get their slice so cache
    keys stay minimal and stage semantics stay explicit.
    """

    if stage_name in ("inspect",):
        return {"ocr_policy": config.ocr_policy}
    if stage_name in ("native_parse", "layout_parse"):
        return {
            "engine": config.parser,
            "ocr_policy": config.ocr_policy,
            "document_type_hint": config.document_type_hint,
            "parser_options": config.parser_options.model_dump(),
        }
    if stage_name == "normalize":
        return {}
    if stage_name == "extract":
        return {
            "targets": list(config.targets),
            "rule_versions": config.rule_versions.model_dump(),
            "document_type_hint": config.document_type_hint,
        }
    if stage_name == "validate":
        return {
            "rule_versions": config.rule_versions.model_dump(),
        }
    if stage_name == "export":
        return {"targets": list(config.targets)}
    return {}


__all__ = [
    "WorkflowConfig",
    "RuleVersions",
    "ParserOptions",
    "load_workflow_config",
    "config_hash",
    "effective_options_for_stage",
    "CONFIG_VERSION",
]
