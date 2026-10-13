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
    result = converter.convert(
        request["source_path"],
        page_range=(min(request["page_numbers"]), max(request["page_numbers"])),
        raises_on_error=False,
    )

    raw_path = Path(request["raw_path"])
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(
        json.dumps(
            {"status": str(getattr(result, "status", "unknown"))},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    normalised_path = Path(request["normalised_path"])
    normalised_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result.document.save_as_json(str(normalised_path))
    except AttributeError:
        # Older Docling APIs expose ``export_to_dict``.
        raw = result.document.export_to_dict()
        normalised_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    # Best-effort validation: load back the document and ensure pages exist.
    document = normalize_docling_document(
        json.loads(normalised_path.read_text(encoding="utf-8")),
        parser_run_id=f"{request['job_id']}:docling",
        document_id=request["document_id"],
    )
    if not document.pages:
        raise SystemExit("docling produced no pages")


if __name__ == "__main__":
    main()