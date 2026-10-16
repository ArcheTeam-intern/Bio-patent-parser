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
from bio_pdf.extract.entities import extract_entities, link_measurements_to_entities
from bio_pdf.extract.relations import link_explicit_relations
from bio_pdf.extract.sequences.inline import extract_inline_sequences
from bio_pdf.extract.sequences.references import extract_sequence_references
from bio_pdf.ingest.inspector import inspect_pdf
from bio_pdf.pdf.quality import classify_page, inspect_page_signals, st25_residue_gap
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

#: Engine names that provide layout parsing (the canonical config name is
#: ``docling_sdk``; ``docling`` is the legacy CLI spelling).
_LAYOUT_ENGINES = {"docling", "docling_sdk"}
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
    implementation = "v4-st25-ocr"

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
                # C03: an ST.25 page whose <400> blocks carry no text-layer
                # residues must join the OCR plan — the residue region is
                # rendered (image/outline), and character count alone would
                # route it to plain native text.
                if (
                    st25_residue_gap(text)
                    and decision.route in ("native", "mixed", "unknown")
                ):
                    from bio_pdf.contracts import RouteDecision

                    decision = RouteDecision(
                        route="st25_ocr",
                        reasons=[
                            "ST.25 <400> present without residue text; "
                            "residue region appears rendered (image/outline)"
                        ],
                        rule_version="v1",
                    )
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
        # T04: turn the signals into an executable parse plan (OCR policy
        # enforced here, not silently ignored downstream).
        from bio_pdf.workflow.parse_plan import build_parse_plan

        engine = str(context.options.get(OPT_ENGINE, "native"))
        ocr_policy = str(
            _workflow_options(context).get("ocr_policy")
            or context.options.get("ocr_policy")
            or "auto"
        )
        plan = build_parse_plan(
            pages_out,
            page_numbers=tuple(context.page_numbers),
            engine=engine,
            ocr_policy=ocr_policy,
        )
        payload["parse_plan"] = plan.to_payload()

        out_dir = Path(context.stage_dir())
        ref = atomic_write_json(out_dir, "quality.json", payload)
        return StageOutcome(
            artifact_refs=[_stage_ref(context, "quality.json", ref)],
            warnings=list(plan.warnings),
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
    # v6: the child now forwards docling `pictures` and the normaliser emits
    # figure regions (bbox + resolved caption) so charts can be exported.
    # v7: figure filter (logos/table-covered/text-covered regions dropped).
    implementation = "v7-figure-filter"

    def provider_version(self, context: StageContext) -> str:
        engine = str(context.options.get(OPT_ENGINE, "native"))
        if engine not in _LAYOUT_ENGINES:
            return ""
        from bio_pdf.parsers.ocr import ocr_availability, ocr_versions

        return json.dumps(
            {"versions": ocr_versions(), "ocr_available": ocr_availability()}, sort_keys=True
        )

    def run(self, context: StageContext) -> StageOutcome:
        engine = str(context.options.get(OPT_ENGINE, "native"))
        if engine not in _LAYOUT_ENGINES:
            # Explicitly nothing to do for a native-only run. The runner
            # records this stage as skipped; it is not a silent failure.
            return StageOutcome(artifact_refs=[], review_required=0)

        from bio_pdf.parsers.base import ParseRequest

        try:
            from bio_pdf.parsers.docling_adapter import DoclingAdapter
        except WorkflowError:
            return StageOutcome(artifact_refs=[], review_required=0)

        # B04: the inspect-stage plan is EXECUTED, not merely read. Pages
        # are grouped into (profile, contiguous pages) runs: scan/OCR pages
        # really run the OCR profile, text pages run the native-layout
        # profile, and the per-run documents are merged by source page
        # number. A mixed document no longer runs one layout_native pass
        # and pretends the scan page was parsed.
        from bio_pdf.workflow.parse_plan import (
            ExecutionRun,
            build_parse_plan,
            group_execution_runs,
        )

        pages = list(context.page_numbers)
        workflow_cfg = _workflow_options(context)
        runs: list[ExecutionRun] = []
        try:
            quality = _read_input_json(
                context, "inspect.quality.json", "inspect", "quality.json"
            )
            plan_payload = quality.get("parse_plan") or {}
            if plan_payload:
                from bio_pdf.workflow.parse_plan import ParsePlan, PageTask

                plan = ParsePlan(
                    requested_pages=plan_payload.get("requested_pages") or pages,
                    tasks=[
                        PageTask(
                            page_number=int(t["page_number"]),
                            kind=t["kind"],
                            reasons=list(t.get("reasons") or []),
                            route=str(t.get("route") or "unknown"),
                        )
                        for t in plan_payload.get("tasks", [])
                    ],
                    ocr_policy=str(plan_payload.get("ocr_policy") or "auto"),
                    engine=str(plan_payload.get("engine") or context.options.get(OPT_ENGINE, "native")),
                )
                runs = group_execution_runs(plan)
        except WorkflowError:
            pass  # no plan (legacy job): single default run below

        if not runs:
            runs = [ExecutionRun(kind="layout_native", pages=tuple(pages))]

        out_dir = Path(context.stage_dir())
        merged_pages: dict[int, dict[str, Any]] = {}
        planned_vs_actual: list[dict[str, Any]] = []
        run_summaries: list[dict[str, Any]] = []

        for run_index, run in enumerate(runs):
            # layout_ocr → OCR profile; native/layout_native → layout profile.
            profile = "layout_ocr" if run.kind == "layout_ocr" else "layout_native"
            run_dir = out_dir / f"run-{run_index}-{run.kind.replace('_', '-')}"
            request = ParseRequest(
                job_id=context.job_id,
                document_id=context.document_id,
                source_path=Path(context.source_path),
                page_numbers=run.pages,
                profile=profile,  # type: ignore[arg-type]
                options_ref="configs/demo.toml",
                output_dir=run_dir,
                parser_options=dict(workflow_cfg.get("parser_options") or {}),
            )
            artifacts = DoclingAdapter().parse(request)

            produced = json.loads(artifacts.normalized_path.read_text(encoding="utf-8"))
            for page in produced.get("pages", []):
                number = int(page.get("page_number"))
                if number not in run.pages:
                    # The engine returned a page outside the run (or the
                    # child's merge filter failed): keep the real number and
                    # record the mismatch — never silently relabel.
                    planned_vs_actual.append(
                        {
                            "page": number,
                            "planned_kind": run.kind,
                            "actual": "out_of_run_page",
                        }
                    )
                    continue
                merged_pages[number] = page
            for number in run.pages:
                planned_vs_actual.append(
                    {
                        "page": number,
                        "planned_kind": run.kind,
                        "actual_profile": profile,
                        "produced": number in {int(p.get("page_number")) for p in produced.get("pages", [])},
                    }
                )
            run_summaries.append(
                {
                    "kind": run.kind,
                    "profile": profile,
                    "pages": list(run.pages),
                    "normalized": str(artifacts.normalized_path.name),
                    "raw_response": str(artifacts.raw_response_path.name),
                    "ocr_runtime": json.loads(
                        artifacts.manifest_path.read_text(encoding="utf-8")
                    ).get("extra", {}).get("ocr_runtime"),
                }
            )

        missing_pages = sorted(set(pages) - set(merged_pages))
        # Blank pages produce no engine run by design: synthesize an empty
        # page with the REAL geometry from the inspection so the document
        # covers the requested set honestly ("已检查而非漏页").
        if missing_pages:
            try:
                inspection = _read_input_json(
                    context, "ingest.inspection.json", "ingest", "inspection.json"
                )
                geometry = {
                    int(p["page_number"]): p for p in inspection.get("pages", [])
                }
            except WorkflowError:
                geometry = {}
            for number in list(missing_pages):
                geom = geometry.get(number)
                if geom is None:
                    continue
                merged_pages[number] = {
                    "page_number": number,
                    "width_pt": float(geom.get("width_pt") or 0),
                    "height_pt": float(geom.get("height_pt") or 0),
                    "route": "blank",
                    "blocks": [],
                    "tables": [],
                }
            missing_pages = sorted(set(pages) - set(merged_pages))

        if missing_pages:
            from bio_pdf.errors import PageCoverageMismatchError

            raise PageCoverageMismatchError(
                f"layout runs did not produce pages: {missing_pages}",
                details={"missing": missing_pages, "runs": run_summaries},
            )

        merged_doc = {
            "schema_version": "1.1.0",
            "document_id": context.document_id,
            "parser_run_id": f"{context.job_id}:docling",
            "pages": [merged_pages[n] for n in sorted(merged_pages)],
            "errors": [],
        }
        from bio_pdf.storage.artifacts import atomic_write_json

        ref = atomic_write_json(out_dir, "normalized.json", merged_doc)
        manifest = {
            "schema_version": "1.1.0",
            "engine": "docling",
            "runs": run_summaries,
            "planned_vs_actual": planned_vs_actual,
            "parser_options": dict(workflow_cfg.get("parser_options") or {}),
        }
        atomic_write_json(out_dir, "manifest.json", manifest)

        from bio_pdf.storage.artifacts import compute_sha256_bytes

        refs = []
        for rel_name in ("normalized.json", "manifest.json"):
            path = out_dir / rel_name
            sha = compute_sha256_bytes(path.read_bytes())
            refs.append(
                _stage_ref(
                    context,
                    rel_name,
                    ArtifactRef(
                        artifact_id=f"sha256:{sha}",
                        sha256=sha,
                        relative_path=rel_name,
                        media_type="application/json",
                    ),
                )
            )
        return StageOutcome(artifact_refs=refs, review_required=0)


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
    # v5: reconstructs parser-missed call matrices from block geometry
    # (susceptibility heat tables without grid lines). Bumping invalidates
    # cached normalize/extract chains so existing jobs can resume into the
    # new behaviour.
    # v6: re-filters figure regions against reconstructed tables.
    # v7: grid reconstruction seeds from the native glyph layer and anchors
    # rows on the left-margin labels (empty rows kept, labels never shift).
    implementation = "v7-native-call-seeds"

    def run(self, context: StageContext) -> StageOutcome:
        # Primary structure: layout parse when the engine produced one,
        # else the native parse. The native channel, when present alongside
        # layout, is fused in by spatial overlap (plan T05) rather than
        # concatenated.
        primary_payload: dict[str, Any] | None = None
        native_payload: dict[str, Any] | None = None
        for logical, target in (
            ("layout_parse.normalized.json", "primary"),
            ("native_parse.normalized.json", "native"),
        ):
            if logical in context.inputs:
                payload = _read_input_json(context, logical)
                if target == "primary" and payload:
                    primary_payload = payload
                elif target == "native" and payload:
                    native_payload = payload
        if primary_payload is None and native_payload is not None:
            # Native-only job: the native parse IS the primary structure.
            primary_payload = native_payload
            native_payload = None
        if primary_payload is None:
            # Legacy job: fixed parse paths.
            docling_norm = Path(context.output_dir) / "parse" / "docling" / "normalized.json"
            native_norm = Path(context.output_dir) / "parse" / "native" / "normalized.json"
            source_path = docling_norm if docling_norm.is_file() else native_norm
            if not source_path.is_file():
                raise WorkflowError(
                    "no parse artifacts to normalize (both docling and native missing)",
                    details={"docling": str(docling_norm), "native": str(native_norm)},
                )
            primary_payload = json.loads(source_path.read_text(encoding="utf-8"))
            if docling_norm.is_file() and native_norm.is_file():
                native_payload = json.loads(native_norm.read_text(encoding="utf-8"))

        document = NormalizedDocument.model_validate(primary_payload)
        if not document.pages:
            raise WorkflowError(
                "normalized document has no pages",
                details={"job_id": context.job_id},
            )

        fusion_warnings: list[str] = []
        native_doc = None
        if native_payload is not None and native_payload != primary_payload:
            try:
                native_doc = NormalizedDocument.model_validate(native_payload)
            except Exception:  # noqa: BLE001 - fusion is best-effort enrichment
                native_doc = None
        if native_doc is not None:
            from bio_pdf.parsers.fusion import fuse_documents

            fused = fuse_documents(document, native_doc)
            document = fused.document
            fusion_warnings.extend(fused.warnings)
            if fused.conflicts:
                document = document.model_copy(
                    update={
                        "errors": list(document.errors)
                        + [f"TEXT_CONFLICT page={c['page']} block={c['block_id']}" for c in fused.conflicts[:10]]
                    }
                )

        # Parser-missed call matrices (susceptibility heat tables without
        # drawn grid lines): rebuild the structure purely from block
        # geometry, marked geometry_reconstructed — a candidate structure
        # for human review, never parser truth. Figure regions covered by
        # a (re)constructed table lose their "picture" status: their
        # content is data now, so 图表 only shows what is not text/table.
        from bio_pdf.parsers.grid_matrix import reconstruct_call_matrices
        from bio_pdf.parsers.normalize_docling import filter_exportable_figures

        native_pages = (
            {p.page_number: p for p in native_doc.pages} if native_doc is not None else {}
        )
        for page in document.pages:
            occupied = [tuple(t.bbox) for t in page.tables if t.bbox]
            # The native glyph layer is the seed source when available: the
            # layout channel may merge adjacent letters into one text item,
            # and a real page lost half its call glyphs that way.
            native_page = native_pages.get(page.page_number)
            extra = reconstruct_call_matrices(
                [b.model_dump(mode="json") for b in page.blocks],
                page_number=page.page_number,
                occupied_regions=occupied,
                native_blocks=(
                    [b.model_dump(mode="json") for b in native_page.blocks]
                    if native_page is not None
                    else None
                ),
            )
            if extra:
                document = document.model_copy(
                    update={
                        "pages": [
                            p.model_copy(
                                update={
                                    "tables": list(p.tables) + extra,
                                    "figures": filter_exportable_figures(
                                        list(p.figures),
                                        list(p.tables) + extra,
                                        p.blocks,
                                        width_pt=p.width_pt,
                                        height_pt=p.height_pt,
                                    ),
                                }
                            )
                            if p.page_number == page.page_number
                            else p
                            for p in document.pages
                        ]
                    }
                )
                fusion_warnings.append(
                    f"GRID_RECONSTRUCTED page={page.page_number} tables={len(extra)}"
                )

        # Coverage: every requested page must be present. A partial parse
        # fails here instead of faking completion downstream.
        requested = set(context.page_numbers)
        produced = {p.page_number for p in document.pages}
        missing = sorted(requested - produced)
        if requested and missing:
            raise PageCoverageMismatchError(
                f"normalized document is missing requested pages: {missing}",
                details={
                    "missing": missing,
                    "requested": sorted(requested),
                    "document_errors": list(document.errors)[:5],
                },
            )

        out_dir = Path(context.stage_dir())
        ref = atomic_write_json(
            out_dir, "normalized.json", json.loads(document.model_dump_json())
        )
        return StageOutcome(
            artifact_refs=[_stage_ref(context, "normalized.json", ref)],
            review_required=0,
            warnings=fusion_warnings,
        )


# ---------------------------------------------------------------------------
# extract — sequences, activities, relations and the evidence index
# ---------------------------------------------------------------------------


def _source_parser_from_run(parser_run_id: str) -> str:
    """Adapter metadata → source parser name (never guessed from ids)."""

    lowered = parser_run_id.lower()
    if "docling" in lowered:
        return "layout"
    if "mineru" in lowered:
        return "layout"
    if "native" in lowered:
        return "native"
    return "unknown"


def _merge_ocr_page_lines(
    blocks: list[dict[str, Any]],
    ocr_lines: dict[int, list[tuple[float, str]]],
) -> list[dict[str, Any]]:
    """Interleave OCR'd lines into each page by vertical position (C03).

    Appending a whole page's OCR text after the page's last block loses
    reading order (page 32's DNA rows were attributed to SEQ ID 8 because
    they landed after ``<400> 8``). Instead every OCR line is placed at its
    real y position among the native blocks, so a residue row between
    ``<400> 7`` and ``<210> 8`` stays exactly there.

    Fuzzy dedup: OCR re-reads text that is already in the native layer
    (e.g. a page whose residues partly made it through the layout engine
    as text) — near-identical lines are dropped to avoid double-counting
    residues.
    """

    import difflib
    import re as _re

    def fingerprint(text: str) -> str:
        return _re.sub(r"[\s\d]+", "", text).casefold()

    output: list[dict[str, Any]] = []
    page_items: dict[int, list[tuple[float, str, Any]]] = {}
    for index, block in enumerate(blocks):
        page = int(block.get("page_number", 1))
        bbox = block.get("bbox")
        y = float(bbox[1]) if bbox else 0.0
        page_items.setdefault(page, []).append((y, "block", (index, block)))

    for page, lines in ocr_lines.items():
        page_items.setdefault(page, [])
        native_fps = [
            fingerprint(str(b.get("text") or ""))
            for _y, kind, (_i, b) in page_items[page]
            if kind == "block"
        ]
        kept: list[tuple[float, str, Any]] = []
        for y, text in lines:
            fp = fingerprint(text)
            if len(fp) < 2:
                continue
            if kept and fingerprint(kept[-1][1]) == fp:
                continue
            duplicate = False
            for native_text in native_fps:
                if not native_text:
                    continue
                if fp in native_text or native_text in fp:
                    duplicate = True
                    break
                if difflib.SequenceMatcher(None, fp, native_text).ratio() >= 0.6:
                    duplicate = True
                    break
            if not duplicate:
                kept.append((y, "ocr", text))
        page_items[page].extend(kept)

    for page in sorted(page_items):
        items = page_items[page]
        # Stable sort: equal y keeps original order (native blocks first).
        items.sort(key=lambda item: item[0])
        for _y, kind, payload in items:
            if kind == "block":
                output.append(payload[1])
            else:
                output.append(
                    {
                        "text": payload,
                        "kind": "text",
                        "semantic_role": "unknown",
                        "page_number": page,
                        "bbox": None,
                        "block_id": f"ocr-region:{page}:{int(_y)}",
                        "source_refs": [],
                        "source_aliases": [],
                        "source_parser": "rapidocr_region",
                        "source_artifact_id": None,
                    }
                )
    return output


def _page_blocks(
    document: NormalizedDocument, *, source_artifact_id: str | None = None
) -> list[dict[str, Any]]:
    """Flatten a NormalizedDocument into the block dicts extractors accept.

    ``source_parser`` and ``source_artifact_id`` travel with each block so
    evidence records the real producer (adapter metadata), not a guess
    derived from the block-id string (plan B02).
    """

    parser = _source_parser_from_run(document.parser_run_id)
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
                    "source_refs": list(block.source_refs),
                    "source_aliases": list(block.source_aliases),
                    "source_parser": parser,
                    "source_artifact_id": source_artifact_id,
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
    implementation = "v5-region-ocr"

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
        parser_run_id = document.parser_run_id
        normalized_ref = context.inputs.get("normalize.normalized.json")
        blocks = _page_blocks(
            document,
            source_artifact_id=(
                normalized_ref.relative_path if normalized_ref is not None else None
            ),
        )

        # T06: evidence is constructed at extraction time from real match
        # offsets — never reconstructed from id strings later.
        from bio_pdf.extract.evidence import (
            MAX_EVIDENCE_EXCERPT,
            BlockRef,
            EvidenceIndex,
            span_from_block_match,
            span_from_cell,
            span_page_only,
            span_unlocated,
        )

        evidence = EvidenceIndex()
        diagnostics: list[dict[str, str]] = []
        raw_mentions: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []

        def _issue(scope: str, reason_code: str, source_ids: list[str], detail: str) -> None:
            import hashlib as _hashlib

            digest = _hashlib.sha256(
                f"{context.job_id}|{scope}|{reason_code}|{'|'.join(source_ids)}".encode("utf-8")
            ).hexdigest()[:16]
            issues.append(
                {
                    "issue_id": f"iss-{digest}",
                    "job_id": context.job_id,
                    "scope": scope,
                    "reason_code": reason_code,
                    "source_ids": source_ids,
                    "detail": detail,
                    "state": "open",
                }
            )

        # ---- sequences ----------------------------------------------------
        sequences: list[SequenceRecord] = []
        sequence_inventory: dict[str, Any] | None = None
        if "sequences" in targets:
            from bio_pdf.extract.sequences.inventory import (
                build_sequence_inventory,
                extract_declared_total,
            )
            from bio_pdf.extract.sequences.st25 import parse_st25_blocks_with_scan

            declared_total = extract_declared_total(blocks)

            # C02/C03 two-pass flow: the first pass scans declarations ONLY
            # (no evidence registration yet). Pages whose residue regions
            # stayed empty then get a targeted OCR pass; the final parse —
            # with evidence — runs once over the augmented feed, so no
            # evidence ids are registered twice.
            _, st25_scan = parse_st25_blocks_with_scan(blocks, doc_id)
            document_page_count: int | None = None
            try:
                inspection = _read_input_json(
                    context, "ingest.inspection.json", "ingest", "inspection.json"
                )
                document_page_count = int(inspection.get("page_count") or 0) or None
            except WorkflowError:
                document_page_count = None

            in_scope = set(context.page_numbers) if context.page_numbers else None
            missing_pages = sorted(
                {
                    int(entry["declaration_page"])
                    for entry in build_sequence_inventory(
                        st25_scan, requested_pages=context.page_numbers
                    )["entries"]
                    if entry["status"] in ("missing", "declared")
                    and entry["saw_residue_region"]
                }
            )
            ocr_candidates = [
                p for p in missing_pages if in_scope is None or p in in_scope
            ]
            budget = int(
                _workflow_options(context).get("reparse_max_regions") or 10
            )
            ocr_candidates = ocr_candidates[:budget]

            feed_blocks = blocks
            ocr_pages_used: list[int] = []
            if ocr_candidates:
                from bio_pdf.extract.sequences.region_ocr import ocr_residue_pages
                from pathlib import Path as _Path

                try:
                    ocr_text = ocr_residue_pages(
                        _Path(context.source_path), ocr_candidates
                    )
                except Exception as exc:  # noqa: BLE001 — best-effort path
                    diagnostics.append(
                        {
                            "code": "REGION_OCR_FAILED",
                            "detail": str(exc)[:200],
                        }
                    )
                    ocr_text = {}
                if ocr_text:
                    feed_blocks = _merge_ocr_page_lines(blocks, ocr_text)
                    ocr_pages_used = sorted(ocr_text)
                    diagnostics.append(
                        {
                            "code": "REGION_OCR_PAGES",
                            "detail": (
                                "residue regions OCR'd on pages "
                                + ", ".join(str(p) for p in ocr_pages_used)
                            ),
                        }
                    )

            st25_sequences, st25_scan = parse_st25_blocks_with_scan(
                feed_blocks,
                doc_id,
                parser_run_id=parser_run_id,
                evidence_index=evidence,
            )
            sequences.extend(st25_sequences)

            # C02: the inventory is built from the declaration scan (every
            # <210>), so "declared count" is reportable independently of
            # how many residues were recovered. Full-document coverage is
            # reported honestly: a partial page window never claims the
            # whole listing.
            sequence_inventory = build_sequence_inventory(
                st25_scan,
                requested_pages=context.page_numbers,
                document_page_count=document_page_count,
                declared_total=declared_total,
            )
            for entry in sequence_inventory["entries"]:
                if entry["status"] == "missing":
                    _issue(
                        "sequence",
                        "SEQ_INVENTORY_MISSING",
                        [entry.get("record_id") or f"st25:{entry.get('seq_id_no') or '?'}"],
                        (
                            f"SEQ ID {entry.get('seq_id_no')} (page {entry['declaration_page']}): "
                            "residue region reached but no recoverable residues — needs region OCR"
                        ),
                    )
                elif entry["status"] == "conflict":
                    _issue(
                        "sequence",
                        "SEQ_INVENTORY_CONFLICT",
                        [entry.get("record_id") or f"st25:{entry.get('seq_id_no') or '?'}"],
                        (
                            f"SEQ ID {entry.get('seq_id_no')} (page {entry['declaration_page']}): "
                            + "; ".join(entry["reasons"])
                        ),
                    )

            st25_pages = {
                b["page_number"]
                for b in feed_blocks
                if "<210>" in b["text"] or "<400>" in b["text"]
            }
            non_st25 = [b for b in blocks if b["page_number"] not in st25_pages]
            sequences.extend(
                extract_inline_sequences(
                    non_st25,
                    doc_id,
                    parser_run_id=parser_run_id,
                    evidence_index=evidence,
                    diagnostics=diagnostics,
                    raw_mentions=raw_mentions,
                )
            )

        # ---- activities ---------------------------------------------------
        measurements: list[Measurement] = []
        if "activities" in targets:
            measurements.extend(
                extract_prose_measurements(
                    blocks,
                    doc_id,
                    parser_run_id=parser_run_id,
                    evidence_index=evidence,
                    raw_mentions=raw_mentions,
                )
            )
            for table, _legacy_evidences in _table_rows(document):
                from bio_pdf.extract.activities.table_classifier import (
                    TableKind,
                    classify_table,
                )

                # B06: register REAL cell spans first, then hand the
                # cell_id → evidence_id map to the extractor — record
                # field evidence therefore references loadable span ids,
                # not synthetic ':cell:' strings.
                cell_evidence: dict[str, str] = {}
                for cell in table.cells:
                    if not cell.text_raw:
                        continue
                    span = span_from_cell(
                        document_id=doc_id,
                        parser_run_id=parser_run_id,
                        page_number=table.page_number,
                        table_id=table.table_id,
                        cell_id=cell.cell_id,
                        text_raw=cell.text_raw,
                        bbox=cell.bbox,
                    )
                    evidence.add(span)
                    cell_evidence[cell.cell_id] = span.evidence_id

                kind = classify_table(table)
                if getattr(table, "source", "parser") == "geometry_reconstructed":
                    # The structure itself is a candidate (parser missed the
                    # table entirely): flag it for human review regardless
                    # of which extraction path the classification chose.
                    _issue(
                        "table",
                        "TABLE_GEOMETRY_RECONSTRUCTED",
                        [table.table_id],
                        f"page {table.page_number}: call matrix rebuilt from text geometry; structure and axes need human review",
                    )
                if kind in (
                    TableKind.INVENTORY,
                    TableKind.SCORING,
                    TableKind.UNCLASSIFIED,
                    TableKind.SEQUENCE,
                    TableKind.ASSAY_CONDITIONS,
                ):
                    # Structure preserved; no fabricated measurements. The
                    # decision is recorded per table with its reason.
                    diagnostics.append(
                        {
                            "code": f"TABLE_{kind.value.upper()}",
                            "table_id": table.table_id,
                            "page": str(table.page_number),
                            "detail": "classified without bioactivity extraction",
                        }
                    )
                    _issue(
                        "table",
                        f"TABLE_{kind.value.upper()}",
                        [table.table_id],
                        f"page {table.page_number}: classified {kind.value}; structure preserved",
                    )
                    continue

                for structure_issue in table.structure_issues:
                    _issue("table", "TABLE_STRUCTURE", [table.table_id], structure_issue)
                measurements.extend(
                    extract_table_measurements(
                        table,
                        document_id=doc_id,
                        evidences=cell_evidence,
                        evidence_kind="undetermined",
                    )
                )

        # ---- references and relations --------------------------------------
        # Each mention gets its own evidence span with REAL offsets in its
        # block — "SEQ ID No.1" and "SEQ ID NO: 1" are different mentions
        # and must not share an id (plan B02/B06).
        references: list[tuple[Any, int, int, int]] = []  # (ref, block_idx, start, end)
        for block_idx, block in enumerate(blocks):
            block_text = str(block.get("text") or "")
            search_from = 0
            for ref in extract_sequence_references(block_text, document_id=doc_id):
                match_at = block_text.find(ref.mention_raw, search_from)
                if match_at < 0:
                    match_at = search_from
                search_from = match_at + len(ref.mention_raw)
                references.append((ref, block_idx, match_at, match_at + len(ref.mention_raw)))
        resolved_ids = {r.seq_id_no for r, _b, _s, _e in references}
        unresolved = [
            (ref, block_idx, start, end)
            for ref, block_idx, start, end in references
            if ref.seq_id_no not in resolved_ids
        ]
        for ref, block_idx, start, end in unresolved:
            block = blocks[block_idx]
            try:
                span = span_from_block_match(
                    BlockRef(
                        document_id=doc_id,
                        parser_run_id=parser_run_id,
                        block_id=str(block.get("block_id") or f"block:{block_idx}"),
                        page_number=int(block.get("page_number", 1)),
                        bbox=tuple(block["bbox"]) if block.get("bbox") else None,
                        source_parser=str(block.get("source_parser") or "unknown"),
                        source_artifact_id=block.get("source_artifact_id"),
                    ),
                    text_raw=ref.mention_raw,
                    char_start=start,
                    char_end=end,
                    full_text=str(block.get("text") or ""),
                )
                evidence.add(span)
            except WorkflowError:
                # Offsets unreproducible (block text changed upstream):
                # fall back to an unlocated span, never a wrong location.
                evidence.add(
                    span_unlocated(
                        document_id=doc_id,
                        parser_run_id=parser_run_id,
                        raw_text=ref.mention_raw,
                        source_locator=f"ref:{ref.seq_id_no}",
                        source_parser="unknown",
                    )
                )

        # B07: explicit entities from prose/table labels; measurements link
        # ONLY on an exact name match (a non-match keeps entity_id None and
        # carries a review reason — no closest-match guessing).
        entities = extract_entities(blocks, doc_id)
        measurements, entity_reasons = link_measurements_to_entities(measurements, entities)

        relation_result = link_explicit_relations(entities, sequences, measurements, [])

        # Any record id the extractors did not register resolves to an
        # UNLOCATED span — never a fabricated page 1 (plan B02). Validation
        # reports these as unknown; the UI shows '缺少位置证据'.
        for seq in sequences:
            for eid in seq.evidence_ids:
                if evidence.spans.get(eid) is None:
                    evidence.add(
                        span_unlocated(
                            document_id=doc_id,
                            parser_run_id=parser_run_id,
                            raw_text=seq.raw_sequence[:MAX_EVIDENCE_EXCERPT] or None,
                            source_locator=eid,
                        )
                    )
        for meas in measurements:
            for eid in meas.evidence_ids:
                if evidence.spans.get(eid) is None:
                    evidence.add(
                        span_unlocated(
                            document_id=doc_id,
                            parser_run_id=parser_run_id,
                            raw_text=None,
                            source_locator=eid,
                        )
                    )

        for mention in raw_mentions:
            m_role = getattr(mention, "role", mention.get("role") if isinstance(mention, dict) else None)
            m_reasons = getattr(mention, "reason_codes", mention.get("reason_codes") if isinstance(mention, dict) else [])
            if m_role == "result" and any("metric" in str(r) for r in (m_reasons or [])):
                m_evidence = getattr(mention, "evidence_ids", mention.get("evidence_ids") if isinstance(mention, dict) else [])
                _issue(
                    "mention",
                    "RESULT_WITHOUT_METRIC",
                    list(m_evidence or []),
                    "result-language mention with no resolvable registered metric",
                )

        payload = {
            "schema_version": "1.1.0",
            "document_id": doc_id,
            "parser_run_id": parser_run_id,
            "sequences": [json.loads(s.model_dump_json()) for s in sequences],
            "measurements": [json.loads(m.model_dump_json()) for m in measurements],
            "entities": [json.loads(e.model_dump_json()) for e in entities],
            "relations": [json.loads(r.model_dump_json()) for r in relation_result.relations],
            "unresolved_references": [
                json.loads(r.model_dump_json()) for r in relation_result.unresolved_references
            ]
            + [json.loads(r.model_dump_json()) for r in unresolved],
            "review_reasons": list(relation_result.review_reasons) + list(entity_reasons),
            "evidence_spans": evidence.to_payload(),
            "raw_mentions": [
                m.model_dump(mode="json") if hasattr(m, "model_dump") else m
                for m in raw_mentions
            ],
            # C02: declaration-level inventory (every <210>, recovered or
            # not). None when sequences are not a target of this run.
            "sequence_inventory": sequence_inventory,
            "diagnostics": diagnostics,
            "issues": issues,
        }
        out_dir = Path(context.stage_dir())
        ref = atomic_write_json(out_dir, "extract.json", payload)
        return StageOutcome(
            artifact_refs=[_stage_ref(context, "extract.json", ref)],
            review_required=0,
            warnings=[d["code"] for d in diagnostics[:5]],
        )


def _page_from_legacy_id(evidence_id: str) -> int:
    """Last-resort page extraction for legacy id shapes.

    Only used to anchor evidence that predates the T06 extractors; new
    evidence never needs this (ids are opaque).
    """

    if ":p" in evidence_id:
        try:
            return int(evidence_id.split(":p")[1].split(":")[0])
        except (IndexError, ValueError):
            return 1
    return 1


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

        # B03: the validate stage produces the review PLAN only. Review
        # items are registered by the runner inside the lease-verified
        # publish transaction — a fenced worker must not write the shared
        # review queue through a side connection.
        payload = {
            "schema_version": "1.1.0",
            "sequences": sequences,
            "measurements": measurements,
            "review_count": len(items),
            "review_plan": [json.loads(item.model_dump_json()) for item in items],
            "issue_plan": list(extract.get("issues", [])),
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
