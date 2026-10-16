"""F10: layered per-sequence diagnosis + pending human-annotation queue.

The F-round recheck demanded that every still-missing declaration be
explained LAYER BY LAYER — declaration, residue region, whether the page
entered region OCR, the OCR text actually produced, what the fusion kept,
and why the residue parse rejected it — instead of blaming "budget" for
everything. It also required the character-level annotation pipeline
(data structures + pending list + integration) so a human can verify the
recovered residues; without verified gold the pipeline still reports
``not_evaluated``, never an accuracy number.

Both builders read a job's PUBLISHED artifacts (hash-verified through
:class:`ResultReader`), so they describe exactly what was published.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _result_json(reader: Any, job_id: str, name: str) -> dict[str, Any] | None:
    from bio_pdf.errors import WorkflowError

    try:
        return reader.read_json(job_id, name)
    except WorkflowError:
        return None


def diagnose_sequences(
    artifact_root: Path,
    job_id: str,
    *,
    seq_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Per-sequence layered diagnosis for a published job.

    Returns ``{job_id, baseline_id, entries: [...]}`` where each entry has::

        declared              seq_id, declaration_page, declared_length,
                              molecule_type, record_id
        residue_region        pages the declaration's residues span
        region_ocr            entered (whether its pages were OCR'd),
                              pages, line_count, text (joined),
                              conflicts / superseded records on those pages
        fused_feed            per source (native / rapidocr_region /
                              adopted_candidate): line count + first lines
                              retained for the residue pages
        parse_outcome         status / observed_length / reasons
        diagnosis             a human-readable reason chain for the status
    """

    from bio_pdf.storage.result_reader import ResultReader

    reader = ResultReader(artifact_root)
    extract = _result_json(reader, job_id, "extract")
    if extract is None:
        raise FileNotFoundError(f"job {job_id} has no published extract result")
    normalized = _result_json(reader, job_id, "normalized") or {}
    inventory = extract.get("sequence_inventory") or {}
    entries_raw = inventory.get("entries") or []
    wanted = {str(s) for s in seq_ids} if seq_ids else None

    # Region-OCR sidecar (F08: hash-registered in the published manifest).
    sidecar: dict[str, Any] = {}
    try:
        manifest = reader.read_published_manifest(job_id)
        entry = ((manifest or {}).get("stages") or {}).get("extract", {}).get("region-ocr.json")
        if entry and entry.get("relative_path"):
            sidecar = json.loads(
                (artifact_root / entry["relative_path"]).read_text(encoding="utf-8")
            )
    except Exception:  # noqa: BLE001 — the sidecar is optional evidence here
        sidecar = {}

    ocr_pages = {int(p) for p in (sidecar.get("pages") or {})}
    adopted_pages = {int(p) for p in (sidecar.get("adopted_candidates") or {})}
    conflicts = sidecar.get("content_conflicts") or []
    supersedes = sidecar.get("native_superseded") or []

    # Native feed blocks per page (from the published normalized document).
    blocks_by_page: dict[int, list[dict[str, Any]]] = {}
    for page in normalized.get("pages", []) or []:
        if not isinstance(page, dict):
            continue
        page_no = int(page.get("page_number") or 0)
        for block in page.get("blocks", []) or []:
            if isinstance(block, dict):
                blocks_by_page.setdefault(page_no, []).append(block)

    def _feed_blocks() -> list[dict[str, Any]]:
        """The normalized document in the extract-stage's block shape."""

        out: list[dict[str, Any]] = []
        for page_no in sorted(blocks_by_page):
            for block in blocks_by_page[page_no]:
                out.append(
                    {
                        "text": str(block.get("text_raw") or ""),
                        "kind": str(block.get("kind") or "text"),
                        "semantic_role": str(block.get("semantic_role") or "unknown"),
                        "page_number": page_no,
                        "bbox": block.get("bbox"),
                        "block_id": str(block.get("block_id") or ""),
                        "source_refs": [],
                        "source_aliases": [],
                        "source_parser": str(block.get("source_parser") or "native"),
                        "source_artifact_id": None,
                    }
                )
        return out

    def _replay_fusion(pages: list[int]) -> dict[str, Any]:
        """F10: what the FUSION actually kept — reproduced with the SAME
        merge function the extract stage uses, on the SAME inputs (the
        published normalized blocks + the sidecar's OCR lines). The merged
        feed itself is not persisted as an artifact, so replaying the
        deterministic merge is the faithful way to show this layer."""

        from bio_pdf.workflow.handlers import _merge_ocr_page_lines

        ocr_input: dict[int, list[tuple[float, float, float, float, str]]] = {}
        for page_no in pages:
            lines = (sidecar.get("pages") or {}).get(str(page_no)) or []
            ocr_input[page_no] = [
                (
                    float(ln.get("x0") or 0.0),
                    float(ln.get("y0") or 0.0),
                    float(ln.get("x1") or 0.0),
                    float(ln.get("y1") or 0.0),
                    str(ln.get("text") or ""),
                )
                for ln in lines
            ]
        merged, records = _merge_ocr_page_lines(_feed_blocks(), ocr_input)
        by_source: dict[str, dict[str, Any]] = {}
        kept_ocr: list[str] = []
        for block in merged:
            if int(block.get("page_number") or 0) not in pages:
                continue
            source = str(block.get("source_parser") or "native")
            bucket = by_source.setdefault(source, {"line_count": 0, "first_lines": []})
            bucket["line_count"] += 1
            if len(bucket["first_lines"]) < 3:
                bucket["first_lines"].append(str(block.get("text") or ""))
            if source == "rapidocr_region":
                kept_ocr.append(str(block.get("text") or ""))
        return {
            "sources": by_source,
            "kept_ocr_lines": kept_ocr,
            "merge_records": [
                r for r in records if int(r.get("page_number") or -1) in pages
            ],
        }

    parsed_by_seq: dict[str, dict[str, Any]] = {}
    for record in extract.get("sequences") or []:
        if isinstance(record, dict):
            parsed_by_seq[str(record.get("seq_id_no") or "?")] = record

    entries: list[dict[str, Any]] = []
    for raw in entries_raw:
        seq_no = str(raw.get("seq_id_no") or "?")
        if wanted is not None and seq_no not in wanted:
            continue
        residue_pages = [int(p) for p in (raw.get("residue_pages") or [])]
        declaration_page = int(raw.get("declaration_page") or 0)
        scan_pages = residue_pages or ([declaration_page] if declaration_page else [])
        ocr_text_lines: list[str] = []
        for page_no in scan_pages:
            for line in (sidecar.get("pages") or {}).get(str(page_no), []) or []:
                ocr_text_lines.append(str(line.get("text") or ""))
        entered_ocr = any(page_no in ocr_pages for page_no in scan_pages)
        adopted_used = any(page_no in adopted_pages for page_no in scan_pages)

        fusion = _replay_fusion(scan_pages)
        fused = fusion["sources"]

        page_conflicts = [
            c for c in conflicts if int(c.get("page_number") or -1) in scan_pages
        ]
        page_supersedes = [
            s for s in supersedes if int(s.get("page_number") or -1) in scan_pages
        ]

        status = str(raw.get("status") or "unknown")
        reasons = [str(r) for r in (raw.get("reasons") or [])]
        observed = raw.get("observed_length")
        declared = raw.get("declared_length")
        chain: list[str] = []
        chain.append(
            f"declared SEQ {seq_no} on page {declaration_page} "
            f"(length {declared if declared is not None else 'unknown'})"
        )
        if scan_pages:
            chain.append(f"residue region pages: {scan_pages}")
        kept_ocr = fusion["kept_ocr_lines"]
        if entered_ocr:
            chain.append(
                f"region OCR ran on {sorted(set(scan_pages) & ocr_pages)} → "
                f"{len(ocr_text_lines)} OCR line(s), "
                f"{len(kept_ocr)} kept by the fusion"
            )
        elif adopted_used:
            chain.append("residues entered via an ADOPTED candidate (no region OCR on these pages)")
        else:
            chain.append("region OCR did NOT run on the residue pages")
        if page_conflicts:
            chain.append(f"{len(page_conflicts)} native/OCR content conflict(s) on these pages")
        if page_supersedes:
            chain.append(
                f"{len(page_supersedes)} short native fragment(s) superseded by OCR on these pages"
            )
        if status == "complete":
            chain.append(f"residues parsed: observed length {observed}")
        elif status == "partial":
            chain.append(f"partial residues parsed: {observed}/{declared if declared else '?'}")
        elif status == "conflict":
            chain.append("completeness unverified: a source conflict covers these pages")
        elif status in ("missing", "declared"):
            if entered_ocr and ocr_text_lines and kept_ocr:
                chain.append(
                    "OCR text EXISTS and ENTERED the parse feed, yet no residues were "
                    "parsed — inspect the residue parser on the kept lines, this is "
                    "NOT a budget issue"
                )
            elif entered_ocr and ocr_text_lines:
                chain.append(
                    "OCR text exists but the fusion kept none of its lines (consumed "
                    "as native duplicates) — check the merge records above"
                )
            elif entered_ocr:
                chain.append(
                    "OCR ran but returned nothing for the residue region — inspect the scan"
                )
            else:
                chain.append(
                    "page never entered region OCR (selection/budget) — no OCR text to judge"
                )
        if reasons:
            chain.append("reported reasons: " + "; ".join(reasons[:4]))

        entries.append(
            {
                "seq_id_no": seq_no,
                "record_id": raw.get("record_id"),
                "declared": {
                    "declaration_page": declaration_page,
                    "declared_length": declared,
                    "molecule_type": raw.get("molecule_type"),
                },
                "residue_region": {"pages": residue_pages},
                "region_ocr": {
                    "entered": entered_ocr,
                    "pages": sorted(set(scan_pages) & ocr_pages),
                    "line_count": len(ocr_text_lines),
                    "text": "\n".join(ocr_text_lines),
                    "content_conflicts": page_conflicts,
                    "native_superseded": page_supersedes,
                },
                "fused_feed": fused,
                "parse_outcome": {
                    "status": status,
                    "observed_length": observed,
                    "reasons": reasons,
                    # What the parser actually produced (audit only; the
                    # annotation draft deliberately does NOT ship these
                    # residues as a "starting answer" — gold must be read
                    # from the PDF, not from the pipeline).
                    "parsed_residues": (parsed_by_seq.get(seq_no) or {}).get(
                        "normalized_sequence"
                    ),
                },
                "fusion": {
                    "kept_ocr_lines": kept_ocr[:5],
                    "merge_records": fusion["merge_records"][:10],
                },
                "diagnosis": chain,
            }
        )

    return {
        "job_id": job_id,
        "document_id": extract.get("document_id"),
        "baseline_id": None,  # filled by the caller from the publication if needed
        "inventory_summary": inventory.get("summary"),
        "entries": entries,
        "note": (
            "system states, not verification: a 'complete' status has no character-level "
            "gold behind it until a human verifies the annotation queue"
        ),
    }


def build_annotation_queue(
    artifact_root: Path,
    job_id: str,
    *,
    document_pages: list[int] | None = None,
) -> dict[str, Any]:
    """F10: a DRAFT v2 gold manifest — the pending character-level list.

    Every inventory entry becomes a draft ``sequence`` annotation carrying
    what the pipeline recovered plus the diagnosis context, all marked
    ``human_verified=false`` / ``status='draft'`` and the scope
    ``scope_verified=false``. The file loads through
    ``load_corpus_scope``/``evaluate_corpus`` (a draft scope evaluates as
    ``not_evaluated``), so the human workflow is: open this file, check the
    residues CHARACTER BY CHARACTER against the PDF, fix the strings, flip
    the scope + items to verified, then run ``bio-pdf evaluate``.
    """

    from bio_pdf.storage.result_reader import ResultReader

    reader = ResultReader(artifact_root)
    extract = _result_json(reader, job_id, "extract")
    if extract is None:
        raise FileNotFoundError(f"job {job_id} has no published extract result")
    diagnosis = diagnose_sequences(artifact_root, job_id)
    diagnosis_by_seq = {entry["seq_id_no"]: entry for entry in diagnosis["entries"]}

    inventory = extract.get("sequence_inventory") or {}
    document_id = str(extract.get("document_id") or "")
    annotations: list[dict[str, Any]] = []
    for entry in inventory.get("entries") or []:
        seq_no = str(entry.get("seq_id_no") or "?")
        record_id = entry.get("record_id") or f"st25:{seq_no}"
        diag = diagnosis_by_seq.get(seq_no, {})
        notes = "; ".join((diag.get("diagnosis") or [])[:6])
        annotations.append(
            {
                "annotation_id": f"gold-seq-{document_id.replace(':', '-')[:24]}-{seq_no}",
                "document_id": document_id,
                "page_number": int(entry.get("declaration_page") or 1),
                "kind": "sequence",
                "human_verified": False,
                "status": "draft",
                "seq_id_no": seq_no,
                # Draft value = what the pipeline recovered; the human MUST
                # check it character by character before verifying.
                "sequence": None,
                "molecule_type": entry.get("molecule_type"),
                "note": (
                    f"record {record_id}; declared length {entry.get('declared_length')}; "
                    f"system status {entry.get('status')}; recovered length "
                    f"{entry.get('observed_length')}; {notes}"
                ),
            }
        )
    return {
        "schema_version": "v2",
        "draft": True,
        "job_id": job_id,
        "scopes": [
            {
                "document_id": document_id,
                "pages": sorted(int(p) for p in (document_pages or [])),
                "scope_verified": False,
                "source_sha256": (
                    extract.get("source_sha256") if isinstance(extract, dict) else None
                ),
            }
        ],
        "annotations": annotations,
        "instructions": (
            "Fill each 'sequence' with the residues read CHARACTER BY CHARACTER from the "
            "PDF, set status='verified' + human_verified=true, set the scope's "
            "scope_verified=true and pages to the annotated range, then run "
            "'bio-pdf evaluate --gold <this file> --records <published extract.json>'."
        ),
    }


__all__ = ["diagnose_sequences", "build_annotation_queue"]
