"""Production stage handlers for the complete domain workflow.

T02 conventions:

- Handlers write ONLY into ``context.stage_dir()``
  (``jobs/<job>/stage_runs/<stage_run_id>/``) so attempts never overwrite
  each other.
- Inputs are consumed through ``context.inputs`` — the
  ``<stage>.<logical_name>`` → :class:`ArtifactRef` map the runner passes
  in — with a legacy fixed-path fallback so pre-T02 jobs stay readable.
- Every stage declares ``implementation_version`` (code semantics) and
  ``provider_version`` (external engine), which feed the content cache key.

Artifact layout under ``artifact_root/jobs/<job_id>/``::

    stage_runs/<run>/inspection.json   DocumentInspection
    stage_runs/<run>/quality.json      per-page signals + routes
    stage_runs/<run>/normalized.json   adapter outputs (parse stages)
    stage_runs/<run>/extract.json      domain extraction
    stage_runs/<run>/validation.json   checks + review plan
    stage_runs/<run>/result.json       domain export
    published/result-manifest.json     runner-published baseline
"""

from __future__ import annotations

import json
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
from bio_pdf.errors import PageCoverageMismatchError, WorkflowError
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


def _stage_ref(
    context: StageContext,
    filename: str,
    ref: ArtifactRef,
    media_type: str = "application/json",
) -> ArtifactRef:
    """Wrap an artifact written into the stage dir as a root-relative ref."""

    return ArtifactRef(
        artifact_id=ref.artifact_id,
        sha256=ref.sha256,
        relative_path=_job_rel(
            context, "stage_runs", context.stage_run_id, filename
        ),
        media_type=ref.media_type or media_type,
        producer_stage_run_id=context.stage_run_id,
    )


def _read_input_json(
    context: StageContext, logical_name: str, *legacy_parts: str
) -> dict[str, Any]:
    """Read an upstream artifact via its ref, falling back to legacy paths.

    The fallback keeps pre-T02 jobs resumable: their artifacts live at
    ``jobs/<job>/<stage>/<file>`` instead of stage-run directories.
    """

    ref = context.inputs.get(logical_name)
    if ref is not None:
        path = _resolve_artifact(context, ref)
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    legacy = Path(context.output_dir).joinpath(*legacy_parts)
    if legacy.is_file():
        return json.loads(legacy.read_text(encoding="utf-8"))
    raise WorkflowError(
        f"required input {logical_name!r} is missing",
        details={"logical_name": logical_name, "job_id": context.job_id},
    )


def _resolve_artifact(context: StageContext, ref: ArtifactRef) -> Path:
    """Resolve a ref to an absolute path from the artifact root.

    The runner's refs are root-relative (``jobs/...``). Legacy adapter refs
    may carry bare filenames relative to their stage dir.
    """

    root = _artifact_root(context)
    candidate = root / ref.relative_path
    if candidate.is_file():
        return candidate
    return Path(context.stage_dir()) / Path(ref.relative_path).name


def _artifact_root(context: StageContext) -> Path:
    # output_dir = <artifact_root>/jobs/<job_id>
    return Path(context.output_dir).parent.parent


class _FileStage:
    """Shared plumbing: input-hash based cache key and version hooks.

    The runner builds the authoritative content cache key itself; the
    handler-side ``cache_key`` remains for protocol compatibility.
    """

    name: str = "unset"
    implementation: str = "v3"

    def implementation_version(self) -> str:
        return self.implementation

    def provider_version(self, context: StageContext) -> str:
        return ""

    def cache_key(self, context: StageContext) -> str:
        return make_cache_key(
            {
                "stage": self.name,
                "document_sha256": context.document_sha256,
                "pages": sorted(context.page_numbers),
            }
        )


def _workflow_options(context: StageContext) -> dict[str, Any]:
    """Effective WorkflowConfig dict handed down by the JobService."""

    cfg = context.options.get("workflow_config")
    return cfg if isinstance(cfg, dict) else {}


# ---------------------------------------------------------------------------
# ingest — signature/geometry inspection of the source PDF
# ---------------------------------------------------------------------------


class IngestStage(_FileStage):
    name = "ingest"
    implementation = "v3"

    def run(self, context: StageContext) -> StageOutcome:
        inspection = inspect_pdf(Path(context.source_path))
        out_dir = Path(context.stage_dir())
        ref = atomic_write_json(
            out_dir, "inspection.json", json.loads(inspection.model_dump_json())
        )
        return StageOutcome(
            artifact_refs=[
                _stage_ref(context, "inspection.json", ref),
            ]
        )


# ---------------------------------------------------------------------------
# inspect — per-page quality signals and route decisions
# ---------------------------------------------------------------------------


class InspectStage(_FileStage):
    name = "inspect"
    implementation = "v3"

    def run(self, context: StageContext) -> StageOutcome:
        import pypdfium2 as pdfium

        from bio_pdf.contracts import QualityConfig

        inspection = DocumentInspection.model_validate(
            _read_input_json(
                context, "ingest.inspection.json", "ingest", "inspection.json"
            )
        )

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
                # Image coverage: clip each image bbox to the visible page
                # and take the union area (clamped) — a naive sum overcounts
                # overlapping images.
                image_area = 0.0
                try:
                    rects: list[tuple[float, float, float, float]] = []
                    for obj in page.get_objects(max_depth=2):
                        if obj.type == 3:  # image
                            x0, y0, x1, y1 = obj.get_pos()
                            x0, y0 = max(x0, 0.0), max(y0, 0.0)
                            x1, y1 = min(x1, width_pt), min(y1, height_pt)
                            if x1 > x0 and y1 > y0:
                                rects.append((x0, y0, x1, y1))
                    image_area = _union_area(rects)
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

        payload = {
            "schema_version": "1.1.0",
            "pages": pages_out,
            "warnings": [],
        }
        out_dir = Path(context.stage_dir())
        ref = atomic_write_json(out_dir, "quality.json", payload)
        return StageOutcome(
            artifact_refs=[_stage_ref(context, "quality.json", ref)]
        )


def _union_area(rects: list[tuple[float, float, float, float]]) -> float:
    """Area of the union of axis-aligned rectangles (scan-line sweep)."""

    if not rects:
        return 0.0
    events: list[tuple[float, int, float, float]] = []
    for x0, y0, x1, y1 in rects:
        events.append((x0, 1, y0, y1))
        events.append((x1, -1, y0, y1))
    events.sort()
    active: list[tuple[float, float]] = []
    area = 0.0
    prev_x = events[0][0]
    for x, kind, y0, y1 in events:
        if active and x > prev_x:
            # Union of y-intervals at this slab.
            merged: list[list[float]] = []
            for a, b in sorted(active):
                if merged and a <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], b)
                else:
                    merged.append([a, b])
            height = sum(b - a for a, b in merged)
            area += height * (x - prev_x)
        if kind == 1:
            active.append((y0, y1))
        else:
            active.remove((y0, y1))
        prev_x = x
    return area


# ---------------------------------------------------------------------------
# native_parse / layout_parse — thin wrappers over the adapters
# ---------------------------------------------------------------------------


class NativeParseStage(_FileStage):
    name = "native_parse"
    implementation = "v3"

    def run(self, context: StageContext) -> StageOutcome:
        from bio_pdf.parsers.base import ParseRequest
        from bio_pdf.parsers.native_adapter import NativeAdapter

        out_dir = Path(context.stage_dir())
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
    implementation = "v3"

    def provider_version(self, context: StageContext) -> str:
        engine = str(context.options.get(OPT_ENGINE, "native"))
        if engine != "docling":
            return ""
        try:
            import docling

            return getattr(docling, "__version__", "unknown")
        except Exception:  # noqa: BLE001 - version probe is best-effort
            return "unknown"

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

        out_dir = Path(context.stage_dir())
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


def _refs_from_parse_artifacts(artifacts: Any, out_dir: Path, context: StageContext) -> StageOutcome:
    from bio_pdf.storage.artifacts import compute_sha256_bytes

    refs: list[ArtifactRef] = []
    for rel in ("normalized.json", "raw_response.json", "manifest.json"):
        path = out_dir / rel
        if not path.is_file():
            continue
        sha = compute_sha256_bytes(path.read_bytes())
        refs.append(
            _stage_ref(
                context,
                rel,
                ArtifactRef(
                    artifact_id=f"sha256:{sha}",
                    sha256=sha,
                    relative_path=rel,
                    media_type="application/json",
                ),
            )
        )
    return StageOutcome(artifact_refs=refs, review_required=0)


# ---------------------------------------------------------------------------
# normalize — canonical job-level NormalizedDocument
# ---------------------------------------------------------------------------


class NormalizeStage(_FileStage):
    name = "normalize"
    implementation = "v3"

    def run(self, context: StageContext) -> StageOutcome:
        # Primary structure: layout parse when the engine produced one,
        # else the native parse. Legacy jobs keep their fixed paths.
        payload: dict[str, Any] | None = None
        for logical in ("layout_parse.normalized.json", "native_parse.normalized.json"):
            if logical in context.inputs:
                payload = _read_input_json(context, logical)
                if payload:
                    break
        if payload is None:
            docling_norm = Path(context.output_dir) / "parse" / "docling" / "normalized.json"
            native_norm = Path(context.output_dir) / "parse" / "native" / "normalized.json"
            source_path = docling_norm if docling_norm.is_file() else native_norm
            if not source_path.is_file():
                raise WorkflowError(
                    "no parse artifacts to normalize (both docling and native missing)",
                    details={"docling": str(docling_norm), "native": str(native_norm)},
                )
            payload = json.loads(source_path.read_text(encoding="utf-8"))

        # Strict contract validation: the on-disk file must be a valid
        # NormalizedDocument.
        document = NormalizedDocument.model_validate(payload)
        if not document.pages:
            raise WorkflowError(
                "normalized document has no pages",
                details={"job_id": context.job_id},
            )

        # Coverage: every requested page must be present. A partial parse
        # fails here instead of faking completion downstream.
        requested = set(context.page_numbers)
        produced = {p.page_number for p in document.pages}
        missing = sorted(requested - produced)
        if requested and missing:
            raise PageCoverageMismatchError(
                f"normalized document is missing requested pages: {missing}",
                details={"missing": missing, "requested": sorted(requested)},
            )

        out_dir = Path(context.stage_dir())
        ref = atomic_write_json(
            out_dir, "normalized.json", json.loads(document.model_dump_json())
        )
        return StageOutcome(
            artifact_refs=[_stage_ref(context, "normalized.json", ref)],
            review_required=0,
        )


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
                    "semantic_role": block.semantic_role,
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
    implementation = "v3"

    def run(self, context: StageContext) -> StageOutcome:
        targets = set(
            _workflow_options(context).get("targets")
            or context.options.get(OPT_TARGETS)
            or ("sequences", "activities")
        )
        document = NormalizedDocument.model_validate(
            _read_input_json(context, "normalize.normalized.json", "normalized.json")
        )
        doc_id = document.document_id
        blocks = _page_blocks(document)

        evidence_spans: list[dict[str, Any]] = []

        # ---- sequences ----------------------------------------------------
        sequences: list[SequenceRecord] = []
        if "sequences" in targets:
            sequences.extend(parse_st25_blocks(blocks, doc_id))
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
                # Table guardrail (plan T08/M1): the header must carry a
                # known metric before any cell becomes a Measurement;
                # unclassified tables stay in diagnostics, never forced
                # `measured`.
                from bio_pdf.extract.activities.metrics import classify_table_metric

                metric = classify_table_metric(table)
                if metric is None:
                    continue
                table_measurements = extract_table_measurements(
                    table,
                    document_id=doc_id,
                    evidences=evidences,
                    evidence_kind="undetermined",
                    metric=metric,
                )
                measurements.extend(table_measurements)
                for cell in table.cells:
                    if not cell.text_raw:
                        continue
                    span = EvidenceSpan(
                        evidence_id=evidences[cell.cell_id],
                        document_id=doc_id,
                        page_number=table.page_number,
                        bbox=cell.bbox,
                        raw_text=cell.text_raw,
                        location_precision="cell",
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
            "schema_version": "1.1.0",
            "document_id": doc_id,
            "sequences": [json.loads(s.model_dump_json()) for s in sequences],
            "measurements": [json.loads(m.model_dump_json()) for m in measurements],
            "relations": [json.loads(r.model_dump_json()) for r in relation_result.relations],
            "unresolved_references": [
                json.loads(r.model_dump_json()) for r in relation_result.unresolved_references
            ]
            + [json.loads(r.model_dump_json()) for r in unresolved],
            "review_reasons": list(relation_result.review_reasons),
            "evidence_spans": evidence_spans,
        }
        out_dir = Path(context.stage_dir())
        ref = atomic_write_json(out_dir, "extract.json", payload)
        return StageOutcome(
            artifact_refs=[_stage_ref(context, "extract.json", ref)],
            review_required=0,
        )


# ---------------------------------------------------------------------------
# validate — write structured checks back and build the review queue
# ---------------------------------------------------------------------------


class ValidateStage(_FileStage):
    name = "validate"
    implementation = "v3"

    def run(self, context: StageContext) -> StageOutcome:
        from bio_pdf.review.plan import build_review_plan
        from bio_pdf.storage.repository import Repository

        extract = _read_input_json(
            context, "extract.extract.json", "extract", "extract.json"
        )

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
            reasons = list(result.review_reasons) + list(decision.reason_codes)
            sequences.append(_record_with_reasons(record, reasons))

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
            reasons = list(result.review_reasons) + list(decision.reason_codes)
            measurements.append(_record_with_reasons(record, reasons))

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
            "schema_version": "1.1.0",
            "sequences": sequences,
            "measurements": measurements,
            "review_count": len(items),
            "review_reasons": extract.get("review_reasons", []),
        }
        out_dir = Path(context.stage_dir())
        ref = atomic_write_json(out_dir, "validation.json", payload)
        return StageOutcome(
            artifact_refs=[_stage_ref(context, "validation.json", ref)],
            review_required=len(items),
        )


def _record_with_reasons(record: Any, reasons: list[str]) -> dict[str, Any]:
    data = json.loads(record.model_dump_json())
    data["review_reasons"] = reasons
    return data


def _repo_path(context: StageContext) -> str:
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
    implementation = "v3"

    def run(self, context: StageContext) -> StageOutcome:
        from bio_pdf.contracts import Measurement, SequenceRecord
        from bio_pdf.export.csv_export import export_measurements_csv, export_sequences_csv
        from bio_pdf.export.json_export import export_result_json

        validation = _read_input_json(
            context, "validate.validation.json", "validate", "validation.json"
        )
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

        out_dir = Path(context.stage_dir())
        refs = []
        ref = atomic_write_json(out_dir, "result.json", result)
        refs.append(_stage_ref(context, "result.json", ref))
        csv_seqs = export_sequences_csv(sequences).encode("utf-8")
        ref = atomic_write_bytes(out_dir, "sequences.csv", csv_seqs)
        refs.append(_stage_ref(context, "sequences.csv", ref, media_type="text/csv"))
        csv_meas = export_measurements_csv(measurements).encode("utf-8")
        ref = atomic_write_bytes(out_dir, "measurements.csv", csv_meas)
        refs.append(
            _stage_ref(context, "measurements.csv", ref, media_type="text/csv")
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
