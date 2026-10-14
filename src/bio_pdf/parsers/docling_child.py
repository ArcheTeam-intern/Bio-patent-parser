"""Child-process entry point for the Docling adapter.

This module exists so :class:`DoclingAdapter` can shell out to a fresh Python
interpreter via :mod:`subprocess`. Keeping the actual Docling call in a
separate module avoids polluting the parent process with the SDK's heavy
imports.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from bio_pdf.parsers.docling_adapter import _DOCLING_CHILD_REQUEST_ENV, _build_converter_from_request
from bio_pdf.parsers.normalize_docling import normalize_docling_document


def main() -> None:
    payload_path = os.environ.get(_DOCLING_CHILD_REQUEST_ENV)
    if not payload_path:
        raise SystemExit("missing request payload path")
    payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
    request = payload["request"]

    converter = _build_converter_from_request(request)

    # Requested pages may be sparse: convert each contiguous run separately
    # so {2, 4} never pulls page 3 into the result, then merge by the
    # authoritative source page number.
    from bio_pdf.workflow.parse_plan import contiguous_runs

    requested = sorted(set(request["page_numbers"]))
    runs = contiguous_runs(requested)
    merged_texts: list[dict] = []
    merged_tables: list[dict] = []
    sizes: dict[str, dict] = {}

    for start, end in runs:
        result = converter.convert(
            request["source_path"],
            page_range=(start, end),
            raises_on_error=False,
        )
        try:
            raw_document = result.document.export_to_dict()
        except AttributeError:
            raw_document = None
        if raw_document is None:
            legacy_path = Path(request["raw_path"]).with_suffix(f".r{start}-{end}.docling.json")
            result.document.save_as_json(str(legacy_path))
            raw_document = json.loads(legacy_path.read_text(encoding="utf-8"))

        pages_field = raw_document.get("pages") or {}
        if isinstance(pages_field, dict):
            for key, value in pages_field.items():
                sizes[key] = value
        for key in ("texts", "tables"):
            for item in raw_document.get(key, []) or []:
                # Keep only items whose provenance points at a requested page.
                prov_pages = {
                    int(p.get("page_no"))
                    for p in (item.get("prov") or [])
                    if p.get("page_no") is not None
                }
                if prov_pages and not (prov_pages & set(requested)):
                    continue
                (merged_texts if key == "texts" else merged_tables).append(item)

    combined = {"pages": sizes, "texts": merged_texts, "tables": merged_tables}
    raw_document = combined

    # The upstream Docling document JSON is the *raw* artifact; it must not
    # be presented to consumers as our normalised contract.
    raw_path = Path(request["raw_path"])
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(
        json.dumps(raw_document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    document = normalize_docling_document(
        raw_document,
        parser_run_id=f"{request['job_id']}:docling",
        document_id=request["document_id"],
    )
    if not document.pages:
        raise SystemExit("docling produced no pages")

    # Keep only requested pages (the child of a contiguous run returns the
    # whole run; sparse requests must filter locally).
    if requested:
        filtered = [p for p in document.pages if p.page_number in set(requested)]
        if len(filtered) != len(document.pages):
            document = document.model_copy(update={"pages": filtered})

    normalised_path = Path(request["normalised_path"])
    normalised_path.parent.mkdir(parents=True, exist_ok=True)
    normalised_path.write_text(document.model_dump_json(), encoding="utf-8")


if __name__ == "__main__":
    main()