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

import hashlib
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
from bio_pdf.extract.entities import (
    entities_from_measurement_labels,
    entities_from_sequence_notes,
    extract_entities,
    link_measurements_to_entities,
)
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
_LAYOUT_ENGINES = {"docling", "docling_sdk", "mineru_http"}
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
            # C10: real engine dispatch — the configured engine runs its own
            # adapter. An unavailable engine fails loudly (raised at creation
            # time AND here); it is never silently substituted with Docling
            # while the manifest claims "mineru".
            if engine == "mineru_http":
                import os as _os

                from bio_pdf.parsers.mineru_adapter import MinerUAdapter

                token = _os.environ.get("MINERU_TOKEN", "")
                if not token:
                    raise WorkflowError(
                        "mineru_http requires MINERU_TOKEN; refusing to substitute another engine",
                        details={"engine": engine, "code": "ENGINE_UNAVAILABLE"},
                    )
                artifacts = MinerUAdapter(token=token).parse(
                    request, local_file=Path(context.source_path)
                )
            else:
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
    ocr_lines: dict[int, list[tuple[float, float, str]]],
    *,
    origin: str = "rapidocr_region",
    origin_aliases: dict[int, list[dict[str, str]]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Interleave OCR'd lines into each page by vertical position (C03/D02).

    Reading order: every OCR line is placed at its real y position among the
    native blocks, so a residue row between ``<400> 7`` and ``<210> 8`` stays
    exactly there.

    E5 rules (strict superset of D02), F05/P01 total-coverage:

    - Overlap is decided on BOTH axes (x AND y intervals intersect): equal
      text at DIFFERENT positions is real repeated content — both kept
      (the old adjacent-OCR dedup deleted the second copy).
    - Same physical mention + equal fingerprint → merged (OCR copy dropped).
    - Same mention + the NATIVE text fully covers the OCR content
      (``ocr_fp ⊆ native_fp``) → OCR consumed: nothing can be lost.
    - Same mention + the OCR line is MORE complete than the native fragment
      (``native_fp ⊂ ocr_fp``, proper subset) → the native fragment MUST NOT
      consume the OCR: the complete OCR line takes the mention's place in
      the extraction feed (superseding the short native block for parsing;
      the native artifact itself is untouched) and a ``coverage_supersede``
      record keeps both texts/boxes for audit. The old bidirectional
      substring rule silently deleted "Cys" here (F05 recheck repro).
    - Same mention + DIFFERENT content at comparable granularity
      (lengths within 2x) → the native text stays in the feed and a
      ``content_conflict`` record (page, 2D box, both texts) is returned —
      never silently resolved.
    - Merely overlapping but incomparable → kept, NOT a conflict.

    Records carry a ``kind`` of ``content_conflict`` or ``coverage_supersede``.

    Returns ``(merged_blocks, records, declaration_merges)``:

    - ``records`` — per-mention merge decisions for the audit pipeline
      (content conflicts, coverage supersedes, declaration merges);
    - ``declaration_merges`` — K06 only: LOGICAL declaration merges
      (``verdict`` other than ``incomparable``) with per-field provenance,
      persisted into the ``region-ocr.json`` sidecar. Untagged text is
      never listed here.
    """

    import re as _re

    #: G06/P01: ST.25 tags make digits CONTENT — ``<210> 1`` vs ``<210> 2``
    #: (and ``<211> 10`` vs ``<211> 20``) must never merge silently.
    _tag_in_text = _re.compile(r"<\s*\d{3}\s*>")
    #: Standalone position/count tokens (the 1 5 10 15… margin counters of
    #: residue rows) — the ONLY digits that may differ between two texts at
    #: the same mention and still be considered the same content.
    _standalone_int = _re.compile(r"(?<![\w])\d{1,4}(?![\w])")

    def fingerprint(text: str) -> str:
        """STRICT fingerprint: whitespace removed, digits KEPT."""

        return _re.sub(r"\s+", "", text).upper()

    def loose_fingerprint(text: str) -> str | None:
        """Count-tolerant fingerprint — only for tag-free texts.

        None means "no count tolerance for this text" (it contains an ST.25
        tag, whose numbers are content, per the recheck's P01)."""

        if _tag_in_text.search(text):
            return None
        return _re.sub(r"\s+", "", _standalone_int.sub("", text)).upper()

    def letter_len(fp: str) -> int:
        return len(_re.sub(r"\d+", "", fp))

    _tag_capture = _re.compile(r"<\s*(\d{3})\s*>\s*([^<]*)")

    def tag_declarations(text: str) -> list[dict[str, str]] | None:
        """H05+I05/P01: per-DECLARATION tag maps (a new map starts at <210>).

        A multi-sequence line must never be flattened into one tag→value
        dict (later tags would overwrite earlier ones); each <210> opens its
        own map, and tags before the first <210> form a leading anonymous
        declaration. None = the text carries no tag."""

        matches = _tag_capture.findall(text)
        if not matches:
            return None
        declarations: list[dict[str, str]] = []
        current: dict[str, str] | None = None
        for tag, value in matches:
            if tag == "210":
                current = {"210": value.strip()}
                declarations.append(current)
            else:
                if current is None:
                    current = {}
                    declarations.append(current)
                current[tag] = value.strip()
        return declarations

    def declaration_verdict(native_text: str, ocr_text: str) -> str:
        """Compare two tag-bearing texts FIELD BY FIELD.

        Returns 'equal' | 'native_superset' | 'ocr_superset' | 'addendum' |
        'conflict' | 'incomparable'. Only value differences on a SHARED tag
        (or a declaration-count mismatch) are conflicts — a compatible
        ADDITION such as native ``<210> 1`` vs OCR ``<210> 1 <211> 5`` is
        mergeable and must not be swallowed as a conflict."""

        na = tag_declarations(native_text)
        nb = tag_declarations(ocr_text)
        if na is None or nb is None:
            return "incomparable"
        if len(na) != len(nb):
            return "conflict"
        native_only = False
        ocr_only = False
        for decl_a, decl_b in zip(na, nb):
            shared = set(decl_a) & set(decl_b)
            if any(decl_a[tag] != decl_b[tag] for tag in shared):
                return "conflict"
            if set(decl_a) - set(decl_b):
                native_only = True
            if set(decl_b) - set(decl_a):
                ocr_only = True
        if not native_only and not ocr_only:
            return "equal"
        if ocr_only and not native_only:
            return "ocr_superset"
        if native_only and not ocr_only:
            return "native_superset"
        return "addendum"

    def _intervals_overlap(a0: float, a1: float, b0: float, b1: float) -> bool:
        return a0 - 2.0 <= b1 and b0 - 2.0 <= a1

    def _same_mention(n_box, o_box) -> bool:
        n_x0, n_y0, n_x1, n_y1 = n_box
        o_x0, o_y0, o_x1, o_y1 = o_box
        return _intervals_overlap(n_x0, n_x1, o_x0, o_x1) and _intervals_overlap(
            n_y0, n_y1, o_y0, o_y1
        )

    def _n_tagged_for(natives_list, idx) -> bool:
        """Whether the native at ``idx`` is itself a tagged declaration
        (textual co-location evidence for the page-level fallback)."""

        for ent in natives_list:
            if ent[0] != idx:
                continue
            _ni, _nbox, _nfp, _nl, n_tagged, _nt, _b = ent
            return bool(n_tagged)
        return False

    output: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    # K06: declaration_merge records collected across every page so the
    # caller (region-ocr.json sidecar) can carry per-field provenance.
    declaration_merges: list[dict[str, Any]] = []
    # Uniform item shape: (y, order, kind, payload) where kind is
    # "block" (payload = block dict) or "ocr" (payload = 5-tuple).
    page_items: dict[int, list[tuple[float, int, str, Any]]] = {}
    for index, block in enumerate(blocks):
        page = int(block.get("page_number", 1))
        bbox = block.get("bbox")
        y0 = float(bbox[1]) if bbox else 0.0
        page_items.setdefault(page, []).append((y0, index, "block", block))

    #: block indexes whose native fragment was superseded by a MORE
    #: complete OCR line at the same mention (dropped from the feed only —
    #: the native artifact keeps every byte).
    superseded: set[int] = set()

    from bio_pdf.extract.sequences.declaration_merge import merge_group

    for page, lines in ocr_lines.items():
        natives = [
            (
                item_index,
                (
                    (
                        float(b["bbox"][0]),
                        float(b["bbox"][1]),
                        float(b["bbox"][2]),
                        float(b["bbox"][3]),
                    )
                    if b.get("bbox") and len(b["bbox"]) >= 4
                    else None
                ),
                fingerprint(str(b.get("text") or "")),
                loose_fingerprint(str(b.get("text") or "")),
                tag_declarations(str(b.get("text") or "")) is not None,
                str(b.get("text") or ""),
                str(b.get("block_id") or ""),
            )
            for _y, item_index, kind, b in page_items.get(page, [])
            if kind == "block"
        ]
        kept: list[tuple[float, int, str, Any]] = []
        # K05: pre-group OCR lines by the native mention they overlap.
        # Two OCR lines that fall on the same native block now form a
        # SINGLE declaration instead of restarting the ST.25 parser
        # twice and producing duplicate <210> entries.
        #: item_index (or -1 for no-overlap) -> list of (order, x0,y0,x1,y1, text)
        ocr_groups: dict[int, list[tuple[int, float, float, float, float, str]]] = {}
        #: also keep order index -> bbox/text for the loose-fingerprint
        #: path (tag-free OCR lines that don't share a native mention).
        ungrouped_ocr: list[tuple[int, float, float, float, float, str]] = []
        for order, (x0, y0, x1, y1, text) in enumerate(lines):
            fp = fingerprint(text)
            if letter_len(fp) < 2:
                continue
            # K06: a native with no bbox cannot prove the OCR line
            # belongs to the same physical mention. We fall back to
            # PAGE-level grouping ONLY when BOTH sides carry tagged
            # ST.25 declarations (textual evidence of co-location);
            # the no-bbox native's origin records
            # bbox_verified=False / location_precision="page" so a UI
            # never claims character precision it cannot prove.
            overlapping_indexes = [
                item_index
                for item_index, n_box, n_fp, _nl, _nt, _t, _b in natives
                if n_fp
                and (
                    (n_box is not None and _same_mention(n_box, (x0, y0, x1, y1)))
                    or (
                        n_box is None
                        and tag_declarations(text) is not None
                        and _n_tagged_for(natives, item_index)
                    )
                )
            ]
            if overlapping_indexes:
                key = overlapping_indexes[0]
                ocr_groups.setdefault(key, []).append((order, x0, y0, x1, y1, text))
            else:
                ungrouped_ocr.append((order, x0, y0, x1, y1, text))
        # Process each group once: a single declaration merge drives
        # BOTH the kept line and the audit record.
        for item_index, group_lines in ocr_groups.items():
            # Resolve the native block: first entry of natives with that index.
            native_match = next(
                (n for n in natives if n[0] == item_index), None
            )
            if native_match is None:
                continue
            _ni, _nbox, _nfp, _nl, _ntagged, n_text, n_block_id = native_match
            ocr_texts = [t for _o, _x0, _y0, _x1, _y1, t in group_lines]
            # K05: collate native + ALL OCR lines for this mention into
            # ONE logical declaration. Verdict / conflict flag / field
            # provenance come from the same module — never duplicated.
            group = merge_group(
                native_text=n_text,
                native_block_id=n_block_id,
                ocr_texts=ocr_texts,
            )
            # x/y bbox = union of the native box and the OCR boxes (we
            # keep the FIRST OCR bbox for the audit; subsequent OCR
            # lines are documented in ``ocr_texts``).
            ox0, oy0, ox1, oy1, _t = (
                group_lines[0][1],
                group_lines[0][2],
                group_lines[0][3],
                group_lines[0][4],
                group_lines[0][5],
            )
            # K06: enrich each field origin with the immutable artifact
            # identity (source_parser / source_artifact_id) and the
            # native block bbox so the sidecar can carry field-level
            # provenance that survives a re-open of the artifact.
            enriched_fields = [
                {
                    "field": f.tag,
                    "value": f.value,
                    "origin": f.origin,
                    "block_id": f.block_id,
                    "source_parser": "native" if f.origin == "native" else "rapidocr_region",
                    "source_artifact_id": (
                        "blocks" if f.origin == "native" else "region-ocr.json"
                    ),
                    "page_number": page,
                    "bbox": (
                        list(_nbox)
                        if f.origin == "native" and _nbox is not None
                        else None
                    ),
                    # K06: bbox_verified is True ONLY when the field
                    # came from a native block with a real bbox — an
                    # OCR origin or a no-bbox native falls back to
                    # page-level granularity (location_precision="page").
                    "bbox_verified": bool(
                        f.origin == "native" and _nbox is not None
                    ),
                    "location_precision": (
                        "block"
                        if f.origin == "native" and _nbox is not None
                        else "page"
                    ),
                    "raw_text": n_text if f.origin == "native" else None,
                }
                for f in group.fields
            ]
            # Tag-free text (no ST.25 tags on either side): fall
            # through to the legacy containment / equality / conflict
            # paths via the ungrouped loop. ``merge_group`` only models
            # tagged declarations; ordinary paragraph containment is
            # still decided by the legacy fingerprint rules below.
            if group.verdict == "incomparable":
                ungrouped_ocr.extend(group_lines)
                continue
            # K06: the sidecar lists LOGICAL DECLARATION merges only. An
            # untagged mention is ordinary merged text (the legacy paths
            # below decide it) and must not masquerade as a declaration
            # with zero fields.
            group_record = group.to_record(
                page_number=page, bbox=(ox0, oy0, ox1, oy1)
            )
            group_record["fields"] = enriched_fields
            group_record["native_block_id"] = n_block_id
            declaration_merges.append(group_record)
            # First OCR line's fingerprint drives the early-out below.
            if group.verdict in ("equal", "native_superset"):
                continue  # retained native already carries every field
            if group.verdict == "ocr_superset" and len(ocr_texts) == 1:
                # Single-line case keeps the old coverage_supersede path
                # so downstream record consumers see the same shape.
                superseded.add(item_index)
                records.append(
                    {
                        "kind": "coverage_supersede",
                        "page_number": page,
                        "x0": float(round(ox0, 1)),
                        "y_top": float(round(oy0, 1)),
                        "x1": float(round(ox1, 1)),
                        "y_bottom": float(round(oy1, 1)),
                        "native_text": n_text,
                        "ocr_text": ocr_texts[0],
                        "native_block_id": n_block_id,
                        "reason": (
                            "OCR superset: the OCR line carries every native "
                            "tag plus additional fields; the complete line "
                            "replaces the short fragment in the feed"
                        ),
                    }
                )
                kept.append(
                    (oy0, group_lines[0][0], "ocr",
                     (ox0, oy0, ox1, oy1, ocr_texts[0],
                      [{"text": n_text, "block_id": n_block_id,
                        "fingerprint": fingerprint(n_text)}]))
                )
                continue
            if group.conflict:
                # K05: a conflict inside the group keeps the native line
                # in the feed (no false supersede) and emits ONE
                # content_conflict record per OCR line that disagrees
                # on a shared tag value — the SOURCE_CONFLICT pipeline
                # depends on this exact kind so existing audits keep
                # working unchanged. ``conflicting_ocr`` lists only the
                # offending OCR lines (not the equal ones).
                for ocr_line in group.conflicting_ocr:
                    records.append(
                        {
                            "kind": "content_conflict",
                            "page_number": page,
                            "x0": float(round(ox0, 1)),
                            "y_top": float(round(oy0, 1)),
                            "x1": float(round(ox1, 1)),
                            "y_bottom": float(round(oy1, 1)),
                            "native_text": n_text,
                            "ocr_text": ocr_line,
                        }
                    )
                continue
            # Addendum / mixed verdict with no conflict: ONE merged
            # declaration line replaces the native + all OCR lines.
            superseded.add(item_index)
            records.append(
                group.to_record(page_number=page, bbox=(ox0, oy0, ox1, oy1))
            )
            merge_aliases = (
                [{"kind": "declaration_merge_native", "text": n_text}]
                + [
                    {"kind": "declaration_merge_ocr", "text": t}
                    for t in ocr_texts
                ]
            )
            kept.append(
                (oy0, group_lines[0][0], "ocr",
                 (ox0, oy0, ox1, oy1, group.merged_text, merge_aliases))
            )
        # Process the ungrouped (no-overlap) OCR lines through the
        # legacy paths so tag-free prose + equals + conflicts still
        # work for lines that have no native mention.
        for order, x0, y0, x1, y1, text in ungrouped_ocr:
            fp = fingerprint(text)
            ocr_loose = loose_fingerprint(text)
            ocr_tagged = tag_declarations(text) is not None
            if letter_len(fp) < 2:
                continue
            overlapping = [
                (n_item_index, n_fp, n_loose, n_tagged, n_text, n_block_id)
                for n_item_index, n_box, n_fp, n_loose, n_tagged, n_text, n_block_id in natives
                if n_fp and n_box is not None and _same_mention(n_box, (x0, y0, x1, y1))
            ]
            merge_equal = any(n_fp == fp for _i, n_fp, _nl, _nt, _t, _b in overlapping)
            if ocr_tagged:
                # I05/P01: declaration lines are compared FIELD BY FIELD.
                # A compatible addition (native <210>1 vs OCR <210>1 <211>5)
                # is merged with its extra fields preserved; only shared-tag
                # VALUE conflicts (1 vs 12) block completion. Multi-sequence
                # lines are compared declaration by declaration.
                verdicts = [
                    (item_index, declaration_verdict(n_text, text), n_text, n_block_id)
                    for item_index, _nfp, _nl, _nt, n_text, n_block_id in overlapping
                    if tag_declarations(n_text) is not None
                ]
                if any(verdict in ("equal", "native_superset") for _i, verdict, _t, _b in verdicts):
                    continue  # retained native already carries every field
                ocr_superior = [v for v in verdicts if v[1] == "ocr_superset"]
                if ocr_superior:
                    superseded_native = []
                    for item_index, _verdict, n_text, n_block_id in ocr_superior:
                        superseded.add(item_index)
                        superseded_native.append(
                            {"text": n_text, "block_id": n_block_id, "fingerprint": fingerprint(n_text)}
                        )
                        records.append(
                            {
                                "kind": "coverage_supersede",
                                "page_number": page,
                                "x0": float(round(x0, 1)),
                                "y_top": float(round(y0, 1)),
                                "x1": float(round(x1, 1)),
                                "y_bottom": float(round(y1, 1)),
                                "native_text": n_text,
                                "ocr_text": text,
                                "native_block_id": n_block_id,
                                "reason": (
                                    "compatible field ADDITION: the OCR declaration carries "
                                    "the same shared field values plus additional fields; the "
                                    "complete line replaces the short fragment in the feed "
                                    "(native artifact untouched)"
                                ),
                            }
                        )
                    kept.append((y0, order, "ocr", (x0, y0, x1, y1, text, superseded_native)))
                    continue
                if any(verdict == "addendum" for _i, verdict, _t, _b in verdicts):
                    # J04: disjoint extra fields on both sides are merged
                    # into ONE logical declaration line. Two separate lines
                    # would make the ST.25 parser start a SECOND declaration
                    # (losing the first line's fields and producing a
                    # pseudo-duplicate inventory entry). Field-level
                    # provenance travels in the aliases; the native raw line
                    # is consumed (its bytes remain in its own artifact).
                    merged_declarations: list[str] = []
                    field_provenance: list[dict[str, str]] = []
                    addendum_native: list[str] = []
                    for item_index, verdict, n_text, n_block_id in verdicts:
                        native_fields = tag_declarations(n_text) or []
                        ocr_fields = tag_declarations(text) or []
                        if verdict != "addendum":
                            continue
                        addendum_native.append(n_text)
                        superseded.add(item_index)
                        for pair_index in range(max(len(native_fields), len(ocr_fields))):
                            n_map = native_fields[pair_index] if pair_index < len(native_fields) else {}
                            o_map = ocr_fields[pair_index] if pair_index < len(ocr_fields) else {}
                            merged_map: dict[str, str] = dict(n_map)
                            for tag, value in o_map.items():
                                merged_map.setdefault(tag, value)
                            parts = []
                            for tag, value in merged_map.items():
                                origin = "native" if n_map.get(tag) is not None else "ocr"
                                field_provenance.append(
                                    {
                                        "kind": "declaration_field",
                                        "field": tag,
                                        "value": value,
                                        "origin": origin,
                                        "block_id": n_block_id if origin == "native" else "",
                                    }
                                )
                                parts.append(f"<{tag}> {value}".strip())
                            merged_declarations.append(" ".join(parts))
                    merged_text = " ".join(merged_declarations)
                    records.append(
                        {
                            "kind": "declaration_merge",
                            "page_number": page,
                            "x0": float(round(x0, 1)),
                            "y_top": float(round(y0, 1)),
                            "x1": float(round(x1, 1)),
                            "y_bottom": float(round(y1, 1)),
                            "native_text": addendum_native,
                            "ocr_text": text,
                            "merged_text": merged_text,
                            "fields": [
                                dict(entry) for entry in field_provenance if entry["kind"] == "declaration_field"
                            ][:64],
                            "reason": (
                                "compatible disjoint fields from both channels merged into ONE "
                                "logical declaration (field provenance kept; native artifact "
                                "untouched)"
                            ),
                        }
                    )
                    merge_aliases = [
                        {"kind": "declaration_merge_native", "text": t}
                        for t in addendum_native
                    ] + [{"kind": "declaration_merge_ocr", "text": text}]
                    kept.append(
                        (y0, order, "ocr", (x0, y0, x1, y1, merged_text, merge_aliases))
                    )
                    continue
                # conflict / incomparable among tagged pairs → generic path
                # (conflict record when comparable; containment disabled).
            # G06/P01: count-marker tolerance applies ONLY to tag-free texts
            # and ONLY when the difference is standalone integer tokens (the
            # 1 5 10… margin counters of residue rows). ``<210> 1`` vs
            # ``<210> 2`` never reaches this branch (loose is None for tags).
            if not merge_equal and ocr_loose is not None:
                merge_equal = any(
                    n_loose is not None and n_loose == ocr_loose
                    for _i, n_fp, n_loose, _nt, _t, _b in overlapping
                )
            # F05 + H05: the substring cover/supersede route is for
            # tag-FREE content only — ``<210> 1`` ⊂ ``<210> 12`` is digit
            # coincidence, not coverage.
            containment_allowed = not ocr_tagged and all(
                not n_tagged for _i, _nfp, _nl, n_tagged, _t, _b in overlapping
            )
            covered = containment_allowed and any(
                n_fp and n_fp != fp and fp in n_fp
                for _i, n_fp, _nl, _nt, _t, _b in overlapping
            )
            if merge_equal or covered:
                continue  # nothing beyond the retained text could be lost
            supersedes = [
                (item_index, n_fp, n_text, n_block_id)
                for item_index, n_fp, _n_loose, _n_tagged, n_text, n_block_id in overlapping
                if containment_allowed and n_fp and n_fp != fp and n_fp in fp
            ]
            if supersedes:
                superseded_native = []
                for item_index, n_fp, n_text, n_block_id in supersedes:
                    superseded.add(item_index)
                    superseded_native.append(
                        {"text": n_text, "block_id": n_block_id, "fingerprint": n_fp}
                    )
                    records.append(
                        {
                            "kind": "coverage_supersede",
                            "page_number": page,
                            "x0": float(round(x0, 1)),
                            "y_top": float(round(y0, 1)),
                            "x1": float(round(x1, 1)),
                            "y_bottom": float(round(y1, 1)),
                            "native_text": n_text,
                            "ocr_text": text,
                            "native_block_id": n_block_id,
                            "reason": (
                                "the OCR line covers the native fragment at this "
                                "mention; the complete OCR text replaces it in the "
                                "extraction feed (native artifact untouched)"
                            ),
                        }
                    )
                # Keep the MORE complete OCR line, tagged with what it
                # superseded so evidence links survive.
                kept.append(
                    (y0, order, "ocr", (x0, y0, x1, y1, text, superseded_native))
                )
                continue
            # Comparability on the LETTER content (digits no longer dilute
            # the length comparison): <210> 1 vs <210> 2 stays comparable →
            # recorded as a content conflict, never merged.
            disagree = [
                (n_fp, n_text)
                for _i, n_fp, _nl, _nt, n_text, _b in overlapping
                if max(letter_len(fp), letter_len(n_fp))
                <= 2 * max(min(letter_len(fp), letter_len(n_fp)), 1)
            ]
            if disagree:
                records.append(
                    {
                        "kind": "content_conflict",
                        "page_number": page,
                        "x0": float(round(x0, 1)),
                        "y_top": float(round(y0, 1)),
                        "x1": float(round(x1, 1)),
                        "y_bottom": float(round(y1, 1)),
                        "native_text": disagree[0][1],
                        "ocr_text": text,
                    }
                )
                continue  # native stays in the feed; conflict is recorded
            # Different position OR incomparable granularity: keep the OCR
            # line — content is never dropped for resembling something else.
            kept.append((y0, order, "ocr", (x0, y0, x1, y1, text, None)))
        page_items.setdefault(page, []).extend(kept)

    for page in sorted(page_items):
        items = page_items[page]
        # Stable sort: equal y keeps native-first order (registered first).
        items.sort(key=lambda item: item[0])
        for _y, _order, kind, payload in items:
            if kind == "block":
                item_index = _order
                if item_index in superseded:
                    continue  # superseded by a more complete OCR line
                output.append(payload)
            else:
                x0, y_top, x1, y_bottom, text, superseded_native = payload
                aliases = []
                for sup in superseded_native or []:
                    # J04: merged-declaration aliases carry their own kind and
                    # may lack a block_id — keep every provided key.
                    entry = {
                        "kind": str(sup.get("kind") or "superseded_native"),
                        "text": str(sup.get("text") or ""),
                    }
                    if sup.get("block_id"):
                        entry["block_id"] = str(sup["block_id"])
                    aliases.append(entry)
                # F09: adopted-candidate lines carry their candidate identity
                # on the block, so evidence written from it traces back to
                # the exact adopted artifact.
                for alias in (origin_aliases or {}).get(page, []):
                    aliases.append({str(k): str(v) for k, v in alias.items()})
                output.append(
                    {
                        "text": text,
                        "kind": "text",
                        "semantic_role": "unknown",
                        "page_number": page,
                        # E5: keep the real 2D box so later passes can tell
                        # columns and paragraphs apart.
                        "bbox": [x0, y_top, x1, y_bottom],
                        "block_id": f"ocr-region:{page}:{int(_y)}:{_order}",
                        "source_refs": [],
                        "source_aliases": aliases,
                        "source_parser": origin,
                        "source_artifact_id": None,
                    }
                )
    return output, records, declaration_merges


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
                    # D03: page dimensions travel with every block so the
                    # running-material filter can reason about margins
                    # (header/footer zones, left line-number columns)
                    # instead of deleting by mere repetition.
                    "page_width": page.width_pt,
                    "page_height": page.height_pt,
                    "block_id": block.block_id,
                    "source_refs": list(block.source_refs),
                    "source_aliases": list(block.source_aliases),
                    "source_parser": parser,
                    "source_artifact_id": source_artifact_id,
                    # G09/P05: the coordinate-frame trust travels with the
                    # block so evidence can degrade to page level.
                    "bbox_verified": getattr(block, "bbox_verified", None),
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


def _table_context_conditions(table: Any, document: NormalizedDocument) -> list[dict[str, str]]:
    """G07/P02: conditions from the table's caption and its referenced notes.

    Scans the caption text plus the blocks the table references as caption/
    footnotes (that is where incubation times, doses and method phrases
    really live). Every condition keeps the source text and the block id it
    came from; nothing is invented from proximity alone.
    """

    from bio_pdf.extract.activities.tables import conditions_from_text, merge_conditions

    blocks_by_id: dict[str, Any] = {}
    for page in document.pages:
        for block in page.blocks:
            blocks_by_id[block.block_id] = block

    found: list[dict[str, str]] = []
    caption = getattr(table, "caption", None)
    if caption:
        found.extend(conditions_from_text(str(caption), source="caption"))
    for ref in list(getattr(table, "caption_refs", []) or []) + list(
        getattr(table, "footnote_refs", []) or []
    ):
        block = blocks_by_id.get(str(ref))
        if block is None:
            continue
        found.extend(
            conditions_from_text(
                str(getattr(block, "text_raw", "") or ""),
                source="footnote",
                block_id=str(ref),
            )
        )
    return merge_conditions(found)


class ExtractStage(_FileStage):
    name = "extract"
    implementation = "v7-d04-roles"

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

        def _issue(
            scope: str,
            reason_code: str,
            source_ids: list[str],
            detail: str,
            *,
            page: int | None = None,
            region: dict[str, Any] | None = None,
        ) -> None:
            """Record a parse issue (D09: carries the REAL page/region when
            the caller knows it — position is never re-derived from id
            shapes afterwards)."""

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
                    "page_number": page,
                    "region": region,
                }
            )

        # E9/F08: artifact refs contributed during the sequence pass (region
        # OCR + adopted-candidate sidecar) are published alongside extract.json.
        extract_extra_refs: list[Any] = []
        # F09: what adopted candidates contributed to this extraction (the
        # audit chain candidate → feed blocks → evidence).
        adopted_records_out: list[dict[str, Any]] = []
        #: K06: declaration_merge records from the adopted-candidate
        #: pass; declared outside the ``if adopted_lines`` block so the
        #: region-ocr.json writer (which always reads it) can build
        #: without a NameError when no candidate was consumed.
        adopted_declaration_merges: list[dict[str, Any]] = []

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
            conflict_pages: set[int] = set()
            ocr_text: dict[int, list[tuple[float, float, float, float, str]]] = {}
            ocr_conflicts: list[dict[str, Any]] = []
            coverage_supersedes: list[dict[str, Any]] = []
            #: K06: pre-declared so the sidecar builder (which always
            #: reads it) doesn't NameError when no OCR ran.
            declaration_merge_records: list[dict[str, Any]] = []
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
                    feed_blocks, merge_records, declaration_merge_records = _merge_ocr_page_lines(
                        blocks, ocr_text
                    )
                    ocr_pages_used = sorted(ocr_text)
                    # F05/P01: split the merge records — content conflicts are
                    # recorded as SOURCE_CONFLICT issues; coverage supersedes
                    # (a complete OCR line replaced a short native fragment)
                    # travel into the audit payload and diagnostics so the
                    # replacement is traceable, never silent.
                    ocr_conflicts = [
                        r for r in merge_records if r.get("kind") == "content_conflict"
                    ]
                    coverage_supersedes = [
                        r for r in merge_records if r.get("kind") == "coverage_supersede"
                    ]
                    diagnostics.append(
                        {
                            "code": "REGION_OCR_PAGES",
                            "detail": (
                                "residue regions OCR'd on pages "
                                + ", ".join(str(p) for p in ocr_pages_used)
                            ),
                        }
                    )
                    # D02: same-region native/OCR disagreements are recorded
                    # — never silently resolved to one side. Each conflict
                    # keeps both texts and the real position.
                    for conflict in ocr_conflicts:
                        _issue(
                            "page",
                            "SOURCE_CONFLICT",
                            [
                                f"p{conflict['page_number']}:y{int(conflict['y_top'])}",
                            ],
                            (
                                f"page {conflict['page_number']} y={conflict['y_top']}-{conflict['y_bottom']}: "
                                f"native text {conflict['native_text']!r} differs from OCR "
                                f"{conflict['ocr_text']!r} at the same position"
                            ),
                            page=int(conflict["page_number"]),
                            region={
                                "y_top": conflict["y_top"],
                                "y_bottom": conflict["y_bottom"],
                            },
                        )
                    if ocr_conflicts:
                        diagnostics.append(
                            {
                                "code": "SOURCE_CONFLICT_COUNT",
                                "detail": str(len(ocr_conflicts)),
                            }
                        )
                        conflict_pages = {
                            int(c["page_number"]) for c in ocr_conflicts
                        }
                    else:
                        conflict_pages = set()
                    # F05/P01: a complete OCR line replaced a short native
                    # fragment at the same mention — nothing was dropped
                    # (the native bytes stay in its own artifact) and the
                    # replacement is listed page by page.
                    if coverage_supersedes:
                        diagnostics.append(
                            {
                                "code": "NATIVE_SUPERSEDED_BY_OCR_COUNT",
                                "detail": (
                                    f"{len(coverage_supersedes)} native fragment(s) superseded by "
                                    "more complete OCR lines on pages "
                                    + ", ".join(
                                        str(p)
                                        for p in sorted(
                                            {int(s["page_number"]) for s in coverage_supersedes}
                                        )
                                    )
                                ),
                            }
                        )

            # ---- F09: adopted candidates are CONSUMED as OCR sources ------
            # The content was hash-verified when copied into this job; here
            # it enters the extraction feed through the SAME merge rules as
            # region OCR, tagged with its candidate identity so evidence
            # written from these lines traces back to the adopted artifact.
            adopted_specs = [
                spec
                for spec in (_workflow_options(context).get("adopted_candidates") or [])
                if isinstance(spec, dict)
            ]
            adopted_sidecar: dict[int, list[dict[str, Any]]] = {}
            if adopted_specs:
                import hashlib as _hashlib

                from pathlib import Path as _Path2

                adopted_lines: dict[int, list[tuple[float, float, float, float, str]]] = {}
                adopted_aliases: dict[int, list[dict[str, str]]] = {}
                for spec in adopted_specs:
                    candidate_id = str(spec.get("candidate_id") or "")
                    artifact_name = str(spec.get("artifact_name") or "candidate.txt")
                    page_no = int(spec.get("page_number") or 0)
                    if page_no <= 0 or (in_scope is not None and page_no not in in_scope):
                        # H02/S02: a MANDATORY adopted input that this run
                        # cannot consume is a structural failure — skipping
                        # it would publish a "success" that never adopted
                        # anything.
                        raise WorkflowError(
                            f"adopted candidate {candidate_id} targets page {page_no}, "
                            "outside this run's requested pages",
                            details={
                                "candidate_id": candidate_id,
                                "candidate_page": page_no,
                                "run_pages": list(context.page_numbers),
                                "code": "ADOPTED_CANDIDATE_OUT_OF_SCOPE",
                            },
                        )
                    artifact_path = (
                        _Path2(context.output_dir) / "adopted" / candidate_id / artifact_name
                    )
                    if not artifact_path.is_file():
                        raise WorkflowError(
                            f"adopted candidate artifact missing for {candidate_id}",
                            details={"candidate_id": candidate_id, "path": str(artifact_path)},
                        )
                    # G01/S01: the FULL input tuple is hash-locked — content,
                    # meta (line boxes/text), source identity and coverage
                    # policy. Every check fails loudly BEFORE any line is
                    # consumed; nothing ever falls back to unverified bytes.
                    policy = str(spec.get("coverage_policy") or "merge-with-evidence")
                    if policy != "merge-with-evidence":
                        raise WorkflowError(
                            f"adopted candidate {candidate_id} declares unsupported coverage_policy {policy!r}",
                            details={"candidate_id": candidate_id, "coverage_policy": policy},
                        )
                    payload_bytes = artifact_path.read_bytes()
                    digest = _hashlib.sha256(payload_bytes).hexdigest()
                    expected = str(spec.get("content_sha256") or "")
                    if not expected or digest != expected:
                        raise WorkflowError(
                            f"adopted candidate content hash mismatch for {candidate_id}",
                            details={
                                "candidate_id": candidate_id,
                                "expected_sha256": expected,
                                "actual_sha256": digest,
                            },
                        )
                    text = payload_bytes.decode("utf-8", errors="replace")
                    spec_source = str(spec.get("source_sha256") or "")
                    context_source = str(getattr(context, "document_sha256", "") or "")
                    if spec_source and context_source:
                        if spec_source.replace("sha256:", "") != context_source.replace("sha256:", ""):
                            raise WorkflowError(
                                f"adopted candidate {candidate_id} was produced from a different source document",
                                details={
                                    "candidate_id": candidate_id,
                                    "candidate_source_sha256": spec_source,
                                    "job_source_sha256": context_source,
                                },
                            )
                    meta_path = artifact_path.with_name("candidate.meta.json")
                    expected_meta_sha = str(spec.get("meta_sha256") or "")
                    if not expected_meta_sha:
                        raise WorkflowError(
                            f"adopted candidate {candidate_id} has no hash-locked metadata — "
                            "it cannot be consumed (adopt candidates through the reparse flow)",
                            details={"candidate_id": candidate_id, "code": "ADOPT_META_UNLOCKED"},
                        )
                    if not meta_path.is_file():
                        raise WorkflowError(
                            f"adopted candidate metadata missing for {candidate_id}",
                            details={"candidate_id": candidate_id, "path": str(meta_path)},
                        )
                    meta_bytes = meta_path.read_bytes()
                    meta_digest = _hashlib.sha256(meta_bytes).hexdigest()
                    if meta_digest != expected_meta_sha:
                        raise WorkflowError(
                            f"adopted candidate metadata hash mismatch for {candidate_id}",
                            details={
                                "candidate_id": candidate_id,
                                "expected_sha256": expected_meta_sha,
                                "actual_sha256": meta_digest,
                            },
                        )
                    try:
                        meta = json.loads(meta_bytes.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise WorkflowError(
                            f"adopted candidate metadata is unreadable for {candidate_id}",
                            details={"candidate_id": candidate_id, "error": str(exc)},
                        ) from exc
                    lines_meta = meta.get("lines")
                    boxes = [ln for ln in lines_meta if isinstance(ln, dict)] if isinstance(lines_meta, list) else []
                    if not boxes:
                        raise WorkflowError(
                            f"adopted candidate {candidate_id} metadata carries no lines — "
                            "content cannot be positioned (never consumed blindly)",
                            details={"candidate_id": candidate_id},
                        )
                    joined = "\n".join(str(ln.get("text") or "") for ln in boxes)
                    if joined != text:
                        raise WorkflowError(
                            f"adopted candidate {candidate_id} text does not match its ordered line texts",
                            details={
                                "candidate_id": candidate_id,
                                "meta_text_sha256_first16": _hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16],
                                "content_sha256_first16": digest[:16],
                            },
                        )
                    meta_page = int(meta.get("page_number") or 0)
                    if meta_page and meta_page != page_no:
                        raise WorkflowError(
                            f"adopted candidate {candidate_id} targets page {meta_page}, config says {page_no}",
                            details={"candidate_id": candidate_id, "meta_page": meta_page, "configured_page": page_no},
                        )
                    if boxes:
                        for ln in boxes:
                            adopted_lines.setdefault(page_no, []).append(
                                (
                                    float(ln.get("x0") or 0.0),
                                    float(ln.get("y0") or 0.0),
                                    float(ln.get("x1") or 0.0),
                                    float(ln.get("y1") or 0.0),
                                    str(ln.get("text") or ""),
                                )
                            )
                    else:
                        # Legacy candidates kept only joined text: place it at
                        # the issue region (or a page-leading strip) as ONE
                        # line — honestly coarse, never silently mis-positioned.
                        region = spec.get("region") if isinstance(spec.get("region"), dict) else None
                        y_top = float(region.get("y_top") or 0.0) if region else 0.0
                        y_bottom = (
                            float(region.get("y_bottom") or (y_top + 10.0)) if region else 10.0
                        )
                        adopted_lines.setdefault(page_no, []).append(
                            (0.0, y_top, 612.0, y_bottom, text)
                        )
                    adopted_aliases.setdefault(page_no, []).append(
                        {
                            "kind": "adopted_candidate",
                            "candidate_id": candidate_id,
                            "content_sha256": digest,
                            "baseline_id": str(spec.get("baseline_id") or ""),
                            "origin_job_id": str(spec.get("origin_job_id") or ""),
                        }
                    )
                    adopted_records_out.append(
                        {
                            "candidate_id": candidate_id,
                            "content_sha256": digest,
                            "source_sha256": spec.get("source_sha256"),
                            "origin_baseline_id": spec.get("baseline_id"),
                            "origin_job_id": spec.get("origin_job_id"),
                            "origin_issue_id": spec.get("origin_issue_id"),
                            "page_number": page_no,
                            "line_count": len(adopted_lines.get(page_no, [])),
                            "coverage_policy": spec.get("coverage_policy") or "merge-with-evidence",
                        }
                    )
                if adopted_lines:
                    feed_blocks, adopted_merge_records, _adopted_decl = _merge_ocr_page_lines(
                        feed_blocks,
                        adopted_lines,
                        origin="adopted_candidate",
                        origin_aliases=adopted_aliases,
                    )
                    adopted_declaration_merges.extend(_adopted_decl)
                    # Records from consuming a candidate: coverage supersedes
                    # and content conflicts are explicit (full payload below).
                    used_by_page: dict[int, list[str]] = {}
                    for block in feed_blocks:
                        if str(block.get("source_parser")) == "adopted_candidate":
                            used_by_page.setdefault(int(block.get("page_number") or 0), []).append(
                                str(block.get("block_id") or "")
                            )
                    for record in adopted_records_out:
                        page_no = int(record["page_number"])
                        record["merged_block_ids"] = used_by_page.get(page_no, [])
                        record["merge_records"] = [
                            r
                            for r in adopted_merge_records
                            if int(r.get("page_number") or -1) == page_no
                        ]
                    for page_no, lines in adopted_lines.items():
                        adopted_sidecar[page_no] = [
                            {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "text": t}
                            for (x0, y0, x1, y1, t) in lines
                        ]
                    diagnostics.append(
                        {
                            "code": "ADOPTED_CANDIDATES_CONSUMED",
                            "detail": (
                                ", ".join(
                                    f"{r['candidate_id']}@{r['content_sha256'][:16]}"
                                    for r in adopted_records_out
                                )
                                + " merged into the extraction feed on pages "
                                + ", ".join(str(p) for p in sorted(adopted_lines))
                            ),
                        }
                    )

            # E9/P05 + F08/P05: ONE auditable sidecar for everything that
            # entered the feed from outside the native text layer (region
            # OCR + adopted candidates), written and published with the
            # extract outcome.
            if ocr_text or adopted_sidecar:
                from bio_pdf.storage.artifacts import atomic_write_json as _awj

                # K06: union of the per-call declaration_merge records
                # for the region-OCR pass + the adopted-candidate pass.
                # Each entry carries per-field provenance so the sidecar
                # can resolve a field back to its raw page block / OCR
                # line WITHOUT re-running the handler.
                region_declaration_merges = list(declaration_merge_records) + list(
                    adopted_declaration_merges
                )
                region_payload = {
                    "schema_version": "1.0",
                    "engine": "rapidocr_region",
                    "dpi": 300,
                    "policy": "st25-residue-fallback",
                    "pages": {
                        str(page): [
                            {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "text": t}
                            for (x0, y0, x1, y1, t) in lines
                        ]
                        for page, lines in sorted(ocr_text.items())
                    },
                    "content_conflicts": ocr_conflicts,
                    "native_superseded": coverage_supersedes,
                    # K06: per-field provenance for the logical
                    # declarations formed by ``merge_group``. Each
                    # entry carries the source channel and source
                    # artifact id so an external consumer can resolve
                    # a field back to its raw page block / region OCR
                    # line WITHOUT re-running the handler.
                    "declaration_merges": region_declaration_merges,
                    "adopted_candidates": {
                        str(page): lines for page, lines in sorted(adopted_sidecar.items())
                    },
                }
                region_ref = _awj(Path(context.stage_dir()), "region-ocr.json", region_payload)
                extract_extra_refs.append(
                    _stage_ref(context, "region-ocr.json", region_ref)
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
            # D02 + G06/P01: a page-level native/OCR content conflict blocks
            # the "complete" claim for declarations whose residues run
            # through that page — completeness is unverified while two
            # sources disagree about the same region. A conflict ON the
            # declaration page (e.g. ``<210> 1`` vs ``<210> 2``) counts
            # too: the identifier itself is in dispute.
            if conflict_pages:
                for entry in sequence_inventory["entries"]:
                    if entry["status"] != "complete":
                        continue
                    declaration_page = int(entry.get("declaration_page") or 0)
                    affected_pages = set(entry.get("residue_pages") or [])
                    if declaration_page:
                        affected_pages.add(declaration_page)
                    if conflict_pages & affected_pages:
                        entry["status"] = "conflict"
                        entry["reasons"].append(
                            "native/OCR content conflict on this page's residue region "
                            "or declaration; completeness unverified"
                        )
                summary = sequence_inventory["summary"]
                summary["complete"] = sum(
                    1 for e in sequence_inventory["entries"] if e["status"] == "complete"
                )
                summary["conflict"] = sum(
                    1 for e in sequence_inventory["entries"] if e["status"] == "conflict"
                )
                summary["located"] = (
                    summary["complete"] + summary["partial"] + summary["conflict"]
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
                        page=int(entry["declaration_page"]),
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
                        page=int(entry["declaration_page"]),
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
                        # G09/P05: an unverified table frame yields page-level
                        # cell evidence — the box is withheld, never claimed.
                        bbox=(
                            None
                            if getattr(table, "bbox_verified", None) is False
                            else cell.bbox
                        ),
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
                        page=int(table.page_number),
                        region={"table_id": table.table_id},
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
                        page=int(table.page_number),
                        region={"table_id": table.table_id},
                    )
                    continue

                fatal_structure = [
                    str(issue)
                    for issue in table.structure_issues
                    if str(issue).startswith(
                        ("GRID_SIZE_SUSPECT", "RAGGED_ROWS_SEVERE", "HEADER_SEMANTICS_BROKEN")
                    )
                ]
                if fatal_structure:
                    # G11/P07: a structurally distorted grid must not be
                    # silently converted into activities — explicit issue,
                    # no extraction, region re-parse or human check.
                    _issue(
                        "table",
                        "TABLE_STRUCTURE_NO_EXTRACTION",
                        [table.table_id],
                        (
                            f"page {table.page_number}: distorted grid structure — "
                            + "; ".join(fatal_structure)
                            + "; no activity was extracted from this table"
                        ),
                        page=int(table.page_number),
                        region={"table_id": table.table_id},
                    )
                    diagnostics.append(
                        {
                            "code": "TABLE_STRUCTURE_NO_EXTRACTION",
                            "table_id": table.table_id,
                            "page": str(table.page_number),
                            "detail": "; ".join(fatal_structure)[:300],
                        }
                    )
                    continue
                for structure_issue in table.structure_issues:
                    _issue(
                        "table",
                        "TABLE_STRUCTURE",
                        [table.table_id],
                        structure_issue,
                        page=int(table.page_number),
                        region={"table_id": table.table_id},
                    )
                measurements.extend(
                    extract_table_measurements(
                        table,
                        document_id=doc_id,
                        evidences=cell_evidence,
                        evidence_kind="undetermined",
                        # G07/P02: caption/note-referenced conditions resolved
                        # against THIS document's blocks (evidence preserved).
                        context_conditions=_table_context_conditions(table, document),
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
                        bbox_verified=block.get("bbox_verified"),
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

        # B07/C04: explicit entities from prose/table labels + ST.25 feature
        # notes (#134 constructs, chains, CDRs, W317A variants). Entity and
        # relation evidence is a REAL registered span — resolvable through
        # the evidence API. Measurements link ONLY on an exact name match
        # (a non-match keeps entity_id None and carries a review reason —
        # no closest-match guessing).
        entities = extract_entities(
            blocks, doc_id, evidence_index=evidence, parser_run_id=parser_run_id
        )
        note_entities, note_links = entities_from_sequence_notes(
            sequences,
            feed_blocks,
            doc_id,
            evidence_index=evidence,
            parser_run_id=parser_run_id,
        )
        # D04: explicit assay-table labels (antibiotics, compounds) are real
        # non-sequence entities — they get activity results and no sequence
        # by design; registering them is what clears "no linked entity".
        label_entities = entities_from_measurement_labels(
            measurements,
            doc_id,
            evidence_index=evidence,
            parser_run_id=parser_run_id,
        )
        by_entity_id = {e.entity_id: e for e in entities}
        for extra_entities in (note_entities, label_entities):
            for extra in extra_entities:
                existing = by_entity_id.get(extra.entity_id)
                if existing is None:
                    by_entity_id[extra.entity_id] = extra
                else:
                    for eid in extra.evidence_ids:
                        if eid not in existing.evidence_ids:
                            existing.evidence_ids.append(eid)
                    for chain, seq_no in extra.chain_map.items():
                        existing.chain_map.setdefault(chain, seq_no)
        entities = sorted(by_entity_id.values(), key=lambda e: e.name_raw)

        measurements, entity_reasons = link_measurements_to_entities(measurements, entities)

        inventory_status_by_seq: dict[str, str] = {}
        for entry in (sequence_inventory or {}).get("entries", []):
            seq_key = entry.get("seq_id_no")
            if seq_key is not None:
                inventory_status_by_seq.setdefault(
                    str(seq_key), str(entry.get("status"))
                )

        relation_result = link_explicit_relations(
            entities,
            sequences,
            measurements,
            [],
            note_links=note_links,
            inventory_status_by_seq=inventory_status_by_seq,
        )

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
            # F09: adopted candidates actually consumed by this run (with
            # their merged feed block ids) — empty when none were adopted.
            "adopted_candidates": adopted_records_out,
            "diagnostics": diagnostics,
            "issues": issues,
        }
        out_dir = Path(context.stage_dir())
        ref = atomic_write_json(out_dir, "extract.json", payload)
        return StageOutcome(
            # F08/P05: the region-OCR sidecar rides WITH the extract outcome,
            # so it enters the published manifest (hash-checked, auditable) —
            # it was written to disk but never published before.
            artifact_refs=[_stage_ref(context, "extract.json", ref), *extract_extra_refs],
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
    implementation = "v4-baseline"

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

        # E1/S01: the baseline identity is fixed HERE (content-addressed
        # over the artifacts being published, independent of the manifest's
        # wall-clock timestamp) and baked into every review id and into the
        # publish transaction.
        baseline_digest = hashlib.sha256()
        for ref_key in sorted(context.inputs):
            ref = context.inputs[ref_key]
            baseline_digest.update(f"{ref_key}={ref.sha256}".encode("utf-8"))
            baseline_digest.update(b"\n")
        baseline_id = baseline_digest.hexdigest()[:16]

        items = build_review_plan(review_records, budget=0, baseline_id=baseline_id)

        # B03: the validate stage produces the review PLAN only. Review
        # items are registered by the runner inside the lease-verified
        # publish transaction — a fenced worker must not write the shared
        # review queue through a side connection.
        payload = {
            "schema_version": "1.1.0",
            "sequences": sequences,
            "measurements": measurements,
            "baseline_id": baseline_id,
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
