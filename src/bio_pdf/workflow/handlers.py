"""Production stage handlers for the complete domain workflow.

This module registers the full pipeline (review finding 2): source check →
page classification → parse (native and/or layout) → normalize → domain
extraction → validation + review queue → export. Handlers exchange state
*through artifacts on disk* (never in-memory), which is what makes resume
and hash verification meaningful.

Artifact layout under ``artifact_root/jobs/<job_id>/``::

    ingest/inspection.json      DocumentInspection
    inspect/quality.json        per-page signals + route decisions
    parse/<engine>/...          adapter artifacts (normalized/raw/manifest)
    normalized.json             canonical NormalizedDocument for the job
    extract/extract.json        sequences, measurements, relations, evidence
    validate/validation.json    checks written back + review plan summary
    export/result.json          the domain export (JSON)

Every handler returns artifact refs with paths relative to ``artifact_root``
so the runner can register and later hash-verify them on resume.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bio_pdf.contracts import (
    ArtifactRef,
    DocumentInspection,
    EvidenceSpan,
    Measurement,
    NormalizedDocument,
    SequenceRecord,
    StageOutcome,
)
from bio_pdf.errors import WorkflowError
from bio_pdf.extract.activities.prose import extract_prose_measurements
from bio_pdf.extract.activities.tables import extract_table_measurements
from bio_pdf.extract.relations import link_explicit_relations
from bio_pdf.extract.sequences.inline import extract_inline_sequences
from bio_pdf.extract.sequences.references import extract_sequence_references
from bio_pdf.extract.sequences.st25 import parse_st25_blocks
from bio_pdf.ingest.inspector import inspect_pdf
from bio_pdf.pdf.quality import classify_page, inspect_page_signals
from bio_pdf.storage.artifacts import (
    atomic_write_bytes,
    atomic_write_json,
    make_cache_key,
)
from bio_pdf.validation.acceptance import decide_acceptance
from bio_pdf.validation.activities import validate_measurement
from bio_pdf.validation.evidence import validate_evidence
from bio_pdf.validation.sequences import validate_sequence
from bio_pdf.workflow.stages import StageContext, StageHandler

#: Option keys read from ``context.options``.
OPT_ENGINE = "engine"
OPT_TARGETS = "targets"
OPT_DOCUMENT_TYPE = "document_type_hint"


def _job_rel(context: StageContext, *parts: str) -> str:
    """Relative path (from artifact root) of a job artifact."""

    return "jobs/" + context.job_id + "/" + "/".join(parts)


class _FileStage:
    """Shared plumbing: cache key from stage inputs."""

    name: str = "unset"

    def cache_key(self, context: StageContext) -> str:
        source = Path(context.source_path)
        try:
            stat = source.stat()
            source_sig = f"{source}:{stat.st_size}:{stat.st_mtime_ns}"
        except OSError:
            source_sig = str(source)
        return make_cache_key(
            {
                "stage": self.name,
                "source": source_sig,
                "pages": list(context.page_numbers),
                "options": context.options,
            }
        )


# ---------------------------------------------------------------------------
# ingest — signature/geometry inspection of the source PDF
# ---------------------------------------------------------------------------


class IngestStage(_FileStage):
    name = "ingest"

    def run(self, context: StageContext) -> StageOutcome:
        inspection = inspect_pdf(Path(context.source_path))
        out_dir = Path(context.output_dir) / "ingest"
        ref = atomic_write_json(out_dir, "inspection.json", json.loads(inspection.model_dump_json()))
        return StageOutcome(
            artifact_refs=[
                type(ref)(
                    artifact_id=ref.artifact_id,
                    sha256=ref.sha256,
                    relative_path=_job_rel(context, "ingest", "inspection.json"),
                    media_type="application/json",
                )
            ]
        )


# ---------------------------------------------------------------------------
# inspect — per-page quality signals and route decisions
# ---------------------------------------------------------------------------


class InspectStage(_FileStage):
    name = "inspect"

    def run(self, context: StageContext) -> StageOutcome:
        import pypdfium2 as pdfium

        from bio_pdf.contracts import QualityConfig

        inspection_payload = json.loads(
            (Path(context.output_dir) / "ingest" / "inspection.json").read_text(encoding="utf-8")
        )
        inspection = DocumentInspection.model_validate(inspection_payload)

        requested = list(context.page_numbers) or list(
            range(1, inspection.page_count + 1)
        )
        config = QualityConfig()
        pages_out: list[dict[str, Any]] = []

        pdf = pdfium.PdfDocument(context.source_path)
        try:
            for page_number in requested:
                if page_number < 1 or page_number > len(pdf):
                    continue
                page = pdf[page_number - 1]
                width_pt, height_pt = page.get_size()
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_bounded() or ""
                finally:
                    textpage.close()
                # Image coverage: sum object areas (bounded to 1.0 downstream).
                image_area = 0.0
                try:
                    for obj in page.get_objects(max_depth=2):
                        if obj.type == 3:  # image
                            pos = obj.get_pos()
                            image_area += abs((pos[2] - pos[0]) * (pos[3] - pos[1]))
                except Exception:  # noqa: BLE001 - signals are best-effort
                    image_area = 0.0
                signals = inspect_page_signals(
                    page_number=page_number,
                    text_chars=text,
                    image_area_pt2=image_area,
                    page_area_pt2=float(width_pt * height_pt),
                )
                decision = classify_page(signals, config)
                pages_out.append(
                    {
                        "signals": json.loads(signals.model_dump_json()),
                        "route": json.loads(decision.model_dump_json()),
                    }
                )
        finally:
            pdf.close()

        payload = {"schema_version": "1.0.0", "pages": pages_out}
        out_dir = Path(context.output_dir) / "inspect"
        ref = atomic_write_json(out_dir, "quality.json", payload)
        return StageOutcome(
            artifact_refs=[
                type(ref)(
                    artifact_id=ref.artifact_id,
                    sha256=ref.sha256,
                    relative_path=_job_rel(context, "inspect", "quality.json"),
                    media_type="application/json",
                )
            ]
        )


# ---------------------------------------------------------------------------
# native_parse / layout_parse — thin wrappers over the adapters
# ---------------------------------------------------------------------------


class NativeParseStage(_FileStage):
    name = "native_parse"

    def cache_key(self, context: StageContext) -> str:
        # Always part of the pipeline: native parse feeds the extract stage
        # even when a layout engine provides the primary structure.
        return make_cache_key(
            {
                "stage": self.name,
                "source": _source_sig(context),
                "pages": list(context.page_numbers),
            }
        )

    def run(self, context: StageContext) -> StageOutcome:
        from bio_pdf.parsers.base import ParseRequest
        from bio_pdf.parsers.native_adapter import NativeAdapter

        out_dir = Path(context.output_dir) / "parse" / "native"
        request = ParseRequest(
            job_id=context.job_id,
            document_id=context.document_id,
            source_path=Path(context.source_path),
            page_numbers=tuple(context.page_numbers),
            profile="native",
            options_ref="configs/demo.toml",
            output_dir=out_dir,
        )
        artifacts = NativeAdapter().parse(request)
        return _refs_from_parse_artifacts(artifacts, out_dir, context)


class LayoutParseStage(_FileStage):
    name = "layout_parse"

    def run(self, context: StageContext) -> StageOutcome:
        engine = str(context.options.get(OPT_ENGINE, "native"))
        if engine != "docling":
            # Explicitly nothing to do for a native-only run. The runner
            # records this stage as skipped; it is not a silent failure.
            return StageOutcome(artifact_refs=[], review_required=0)

        from bio_pdf.parsers.base import ParseRequest

        try:
            from bio_pdf.parsers.docling_adapter import DoclingAdapter
        except WorkflowError:
            return StageOutcome(artifact_refs=[], review_required=0)

        out_dir = Path(context.output_dir) / "parse" / "docling"
        request = ParseRequest(
            job_id=context.job_id,
            document_id=context.document_id,
            source_path=Path(context.source_path),
            page_numbers=tuple(context.page_numbers),
            profile="layout_native",
            options_ref="configs/demo.toml",
            output_dir=out_dir,
        )
        artifacts = DoclingAdapter().parse(request)
        return _refs_from_parse_artifacts(artifacts, out_dir, context)


def _source_sig(context: StageContext) -> str:
    source = Path(context.source_path)
    try:
        stat = source.stat()
        return f"{source}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        return str(source)


def _refs_from_parse_artifacts(artifacts: Any, out_dir: Path, context: StageContext) -> StageOutcome:
    from bio_pdf.contracts import ArtifactRef
    from bio_pdf.storage.artifacts import compute_sha256_bytes

    refs: list[ArtifactRef] = []
    for rel in ("normalized.json", "raw_response.json", "manifest.json"):
        path = out_dir / rel
        if not path.is_file():
            continue
        sha = compute_sha256_bytes(path.read_bytes())
        refs.append(
            ArtifactRef(
                artifact_id=f"sha256:{sha}",
                sha256=sha,
                relative_path=_job_rel(context, "parse", out_dir.name, rel),
                media_type="application/json",
            )
        )
    return StageOutcome(artifact_refs=refs, review_required=0)


# ---------------------------------------------------------------------------
# normalize — canonical job-level NormalizedDocument
# ---------------------------------------------------------------------------


class NormalizeStage(_FileStage):
    name = "normalize"

    def run(self, context: StageContext) -> StageOutcome:
        out_root = Path(context.output_dir)
        docling_norm = out_root / "parse" / "docling" / "normalized.json"
        native_norm = out_root / "parse" / "native" / "normalized.json"

        source_path = docling_norm if docling_norm.is_file() else native_norm
        if not source_path.is_file():
            raise WorkflowError(
                "no parse artifacts to normalize (both docling and native missing)",
                details={"docling": str(docling_norm), "native": str(native_norm)},
            )

        # Strict contract validation: the on-disk file must be a valid
        # NormalizedDocument (review finding 3).
        document = NormalizedDocument.model_validate_json(
            source_path.read_text(encoding="utf-8")
        )
        if not document.pages:
            raise WorkflowError(
                "normalized document has no pages",
                details={"source": str(source_path)},
            )

        out_dir = out_root
        ref = atomic_write_json(
            out_dir, "normalized.json", json.loads(document.model_dump_json())
        )
        canonical = type(ref)(
            artifact_id=ref.artifact_id,
            sha256=ref.sha256,
            relative_path=_job_rel(context, "normalized.json"),
            media_type="application/json",
        )
        return StageOutcome(artifact_refs=[canonical], review_required=0)


# ---------------------------------------------------------------------------
# extract — sequences, activities, relations and the evidence index
# ---------------------------------------------------------------------------


def _page_blocks(document: NormalizedDocument) -> list[dict[str, Any]]:
    """Flatten a NormalizedDocument into the block dicts extractors accept."""

    blocks: list[dict[str, Any]] = []
    for page in document.pages:
        for block in page.blocks:
            blocks.append(
                {
                    "text": block.text_raw,
                    "kind": block.kind,
                    "page_number": page.page_number,
                    "bbox": list(block.bbox) if block.bbox else None,
                    "block_id": block.block_id,
                }
            )
    return blocks


def _table_rows(document: NormalizedDocument) -> list[tuple[Any, dict[str, str]]]:
    """Yield (table, cell_id→evidence_id) pairs for every table."""

    out: list[tuple[Any, dict[str, str]]] = []
    for page in document.pages:
        for table in page.tables:
            evidences = {
                cell.cell_id: f"{document.document_id}:cell:{cell.cell_id}"
                for cell in table.cells
            }
            out.append((table, evidences))
    return out


class ExtractStage(_FileStage):
    name = "extract"

    def run(self, context: StageContext) -> StageOutcome:
        targets = set(context.options.get(OPT_TARGETS, ("sequences", "activities")))
        out_root = Path(context.output_dir)
        document = NormalizedDocument.model_validate_json(
            (out_root / "normalized.json").read_text(encoding="utf-8")
        )
        doc_id = document.document_id
        blocks = _page_blocks(document)

        evidence_spans: list[dict[str, Any]] = []

        # ---- sequences ----------------------------------------------------
        sequences: list[SequenceRecord] = []
        if "sequences" in targets:
            # ST.25 listings: feed blocks with text + page numbers.
            sequences.extend(parse_st25_blocks(blocks, doc_id))
            # Conservative inline candidates (skip ST.25-tagged text to
            # avoid double extraction).
            st25_pages = {
                b["page_number"]
                for b in blocks
                if "<210>" in b["text"] or "<400>" in b["text"]
            }
            non_st25 = [b for b in blocks if b["page_number"] not in st25_pages]
            sequences.extend(extract_inline_sequences(non_st25, doc_id))

        # ---- activities ---------------------------------------------------
        measurements: list[Measurement] = []
        if "activities" in targets:
            prose = extract_prose_measurements(blocks, doc_id)
            measurements.extend(prose)
            for block_idx, block in enumerate(blocks):
                if not block.get("bbox"):
                    continue
                prefix = f"{doc_id}:p{block['page_number']}:prose:{block_idx}:"
                for meas in prose:
                    for eid in meas.evidence_ids:
                        if eid.startswith(prefix):
                            evidence_spans.append(
                                json.loads(
                                    EvidenceSpan(
                                        evidence_id=eid,
                                        document_id=doc_id,
                                        page_number=block["page_number"],
                                        bbox=tuple(block["bbox"]),
                                        raw_text=block["text"][:200],
                                    ).model_dump_json()
                                )
                            )
            for table, evidences in _table_rows(document):
                measurements.extend(
                    extract_table_measurements(
                        table,
                        document_id=doc_id,
                        evidences=evidences,
                        evidence_kind="measured",
                    )
                )
                for cell in table.cells:
                    if not cell.text_raw:
                        continue
                    span = EvidenceSpan(
                        evidence_id=evidences[cell.cell_id],
                        document_id=doc_id,
                        page_number=table.page_number,
                        bbox=cell.bbox,
                        raw_text=cell.text_raw[:200],
                    )
                    evidence_spans.append(json.loads(span.model_dump_json()))

        # ---- references and relations --------------------------------------
        references = []
        for block in blocks:
            references.extend(
                ref
                for ref in extract_sequence_references(
                    block["text"], document_id=doc_id
                )
            )
        resolved_ids = {r.seq_id_no for r in sequences if r.seq_id_no is not None}
        unresolved = [r for r in references if r.seq_id_no not in resolved_ids]
        for ref in unresolved:
            evidence_spans.append(
                json.loads(
                    EvidenceSpan(
                        evidence_id=ref.evidence_ids[0],
                        document_id=doc_id,
                        raw_text=ref.mention_raw,
                    ).model_dump_json()
                )
            )

        relation_result = link_explicit_relations([], sequences, measurements, [])

        # Page-level spans for every sequence record (residue listing anchor).
        for seq in sequences:
            page_number = 1
            if seq.evidence_ids:
                head = seq.evidence_ids[0]
                if ":p" in head:
                    try:
                        page_number = int(head.split(":p")[1].split(":")[0])
                    except (IndexError, ValueError):
                        page_number = 1
            evidence_spans.append(
                json.loads(
                    EvidenceSpan(
                        evidence_id=seq.evidence_ids[0],
                        document_id=doc_id,
                        page_number=page_number,
                        raw_text=seq.raw_sequence[:200] or None,
                    ).model_dump_json()
                )
            )

        # Prose measurements without a bbox still need page-level spans.
        have_ids = {s["evidence_id"] for s in evidence_spans}
        for meas in measurements:
            for eid in meas.evidence_ids:
                if eid in have_ids:
                    continue
                parts = eid.split(":p")
                page_number = 1
                if len(parts) > 1:
                    try:
                        page_number = int(parts[1].split(":")[0])
                    except (IndexError, ValueError):
                        page_number = 1
                evidence_spans.append(
                    json.loads(
                        EvidenceSpan(
                            evidence_id=eid,
                            document_id=doc_id,
                            page_number=page_number,
                        ).model_dump_json()
                    )
                )

        payload = {
            "schema_version": "1.0.0",
            "document_id": doc_id,
            "sequences": [json.loads(s.model_dump_json()) for s in sequences],
            "measurements": [json.loads(m.model_dump_json()) for m in measurements],
            "relations": [json.loads(r.model_dump_json()) for r in relation_result.relations],
            "unresolved_references": [
                json.loads(r.model_dump_json()) for r in relation_result.unresolved_references
            ] + [json.loads(r.model_dump_json()) for r in unresolved],
            "review_reasons": list(relation_result.review_reasons),
            "evidence_spans": evidence_spans,
        }
        out_dir = out_root / "extract"
        ref = atomic_write_json(out_dir, "extract.json", payload)
        return StageOutcome(
            artifact_refs=[
                type(ref)(
                    artifact_id=ref.artifact_id,
                    sha256=ref.sha256,
                    relative_path=_job_rel(context, "extract", "extract.json"),
                    media_type="application/json",
                )
            ],
            review_required=0,
        )


# ---------------------------------------------------------------------------
# validate — write structured checks back and build the review queue
# ---------------------------------------------------------------------------


class ValidateStage(_FileStage):
    name = "validate"

    def run(self, context: StageContext) -> StageOutcome:
        from bio_pdf.review.plan import build_review_plan
        from bio_pdf.storage.repository import Repository

        out_root = Path(context.output_dir)
        extract = json.loads((out_root / "extract" / "extract.json").read_text(encoding="utf-8"))

        evidence_index = {
            span["evidence_id"]: span for span in extract.get("evidence_spans", [])
        }

        sequences: list[dict[str, Any]] = []
        for seq_dict in extract.get("sequences", []):
            record = SequenceRecord.model_validate(seq_dict)
            result = validate_sequence(record)
            result.checks.extend(
                validate_evidence(record.evidence_ids, evidence_index).checks
            )
            has_fail = any(c.outcome == "fail" for c in result.checks)
            record.checks = list(result.checks)
            record.validation_state = (
                "conflict" if has_fail else result.validation_state
            )
            decision = decide_acceptance(record.model_dump())
            record.acceptance_state = decision.acceptance_state
            sequences.append(_record_with_reasons(record, result.review_reasons))

        measurements: list[dict[str, Any]] = []
        for meas_dict in extract.get("measurements", []):
            record = Measurement.model_validate(meas_dict)
            result = validate_measurement(record)
            result.checks.extend(
                validate_evidence(record.evidence_ids, evidence_index).checks
            )
            has_fail = any(c.outcome == "fail" for c in result.checks)
            record.checks = list(result.checks)
            record.validation_state = (
                "conflict" if has_fail else result.validation_state
            )
            decision = decide_acceptance(record.model_dump())
            record.acceptance_state = decision.acceptance_state
            measurements.append(_record_with_reasons(record, result.review_reasons))

        # Review queue items persisted for the UI.
        review_records: list[dict[str, Any]] = []
        for seq in sequences:
            review_records.append(
                {
                    "record_id": seq["sequence_id"],
                    "job_id": context.job_id,
                    "validation_state": seq["validation_state"],
                    "reason_codes": seq.get("review_reasons", []),
                    "evidence_ids": seq.get("evidence_ids", []),
                }
            )
        for meas in measurements:
            review_records.append(
                {
                    "record_id": meas["measurement_id"],
                    "job_id": context.job_id,
                    "validation_state": meas["validation_state"],
                    "reason_codes": meas.get("review_reasons", []),
                    "evidence_ids": meas.get("evidence_ids", []),
                }
            )

        items = build_review_plan(review_records, budget=0)
        repo = Repository(_repo_path(context))
        try:
            for item in items:
                repo.upsert_review_item(item)
        finally:
            repo.close()

        payload = {
            "schema_version": "1.0.0",
            "sequences": sequences,
            "measurements": measurements,
            "review_count": len(items),
            "review_reasons": extract.get("review_reasons", []),
        }
        out_dir = out_root / "validate"
        ref = atomic_write_json(out_dir, "validation.json", payload)
        return StageOutcome(
            artifact_refs=[
                type(ref)(
                    artifact_id=ref.artifact_id,
                    sha256=ref.sha256,
                    relative_path=_job_rel(context, "validate", "validation.json"),
                    media_type="application/json",
                )
            ],
            review_required=len(items),
        )


def _record_with_reasons(record: Any, reasons: list[str]) -> dict[str, Any]:
    data = json.loads(record.model_dump_json())
    data["review_reasons"] = reasons
    return data


def _repo_path(context: StageContext) -> str:
    """Repository database path passed through stage options (required)."""

    repo_path = context.options.get("repository_path")
    if not repo_path:
        raise WorkflowError(
            "validate stage requires the 'repository_path' option",
            details={"stage": "validate"},
        )
    return str(repo_path)


# ---------------------------------------------------------------------------
# export — the domain result artifact
# ---------------------------------------------------------------------------


class ExportStage(_FileStage):
    name = "export"

    def run(self, context: StageContext) -> StageOutcome:
        from bio_pdf.contracts import Measurement, SequenceRecord
        from bio_pdf.export.csv_export import export_measurements_csv, export_sequences_csv
        from bio_pdf.export.json_export import export_result_json
        from bio_pdf.storage.artifacts import atomic_write_bytes

        out_root = Path(context.output_dir)
        validation = json.loads(
            (out_root / "validate" / "validation.json").read_text(encoding="utf-8")
        )
        # review_reasons is validator metadata carried alongside each record;
        # the strict record contracts forbid unknown keys, so strip it here.
        seq_payloads = [
            {k: v for k, v in s.items() if k != "review_reasons"}
            for s in validation["sequences"]
        ]
        meas_payloads = [
            {k: v for k, v in m.items() if k != "review_reasons"}
            for m in validation["measurements"]
        ]
        sequences = [SequenceRecord.model_validate(s) for s in seq_payloads]
        measurements = [Measurement.model_validate(m) for m in meas_payloads]

        result = export_result_json(sequences, measurements)
        result["review_count"] = validation.get("review_count", 0)
        result["job_id"] = context.job_id
        result["document_id"] = context.document_id
        result["requested_pages"] = list(context.page_numbers)

        refs = []
        ref = atomic_write_json(
            out_root / "export", "result.json", result
        )
        refs.append(
            type(ref)(
                artifact_id=ref.artifact_id,
                sha256=ref.sha256,
                relative_path=_job_rel(context, "export", "result.json"),
                media_type="application/json",
            )
        )
        csv_seqs = export_sequences_csv(sequences).encode("utf-8")
        ref = atomic_write_bytes(out_root / "export", "sequences.csv", csv_seqs)
        refs.append(
            type(ref)(
                artifact_id=ref.artifact_id,
                sha256=ref.sha256,
                relative_path=_job_rel(context, "export", "sequences.csv"),
                media_type="text/csv",
            )
        )
        csv_meas = export_measurements_csv(measurements).encode("utf-8")
        ref = atomic_write_bytes(out_root / "export", "measurements.csv", csv_meas)
        refs.append(
            type(ref)(
                artifact_id=ref.artifact_id,
                sha256=ref.sha256,
                relative_path=_job_rel(context, "export", "measurements.csv"),
                media_type="text/csv",
            )
        )
        return StageOutcome(artifact_refs=refs, review_required=0)


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def build_production_handlers() -> list[StageHandler]:
    """Return the complete production stage list in pipeline order."""

    return [
        IngestStage(),
        InspectStage(),
        NativeParseStage(),
        LayoutParseStage(),
        NormalizeStage(),
        ExtractStage(),
        ValidateStage(),
        ExportStage(),
    ]


__all__ = ["build_production_handlers"]
