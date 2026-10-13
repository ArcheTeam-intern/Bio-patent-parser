"""Native adapter wrapping :mod:`bio_pdf.pdf` for the parser-adapter interface."""

from __future__ import annotations

import json
from pathlib import Path

from bio_pdf.contracts import (
    ArtifactRef,
    NormalizedDocument,
    NormalizedPage,
    Provenance,
)
from bio_pdf.parsers.base import ParseArtifacts, ParseRequest, ParserAdapter
from bio_pdf.pdf.native import read_native_pages


def _manifest(
    *,
    request: ParseRequest,
    document: NormalizedDocument,
    normalized_path: Path,
    raw_response_path: Path,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build a run manifest dict describing the native parse."""

    return {
        "schema_version": "1.0.0",
        "engine": "native",
        "parser_run_id": document.parser_run_id,
        "document_id": document.document_id,
        "profile": request.profile,
        "pages": list(request.page_numbers),
        "options_ref": request.options_ref,
        "capabilities": {
            "tables": False,
            "ocr": False,
            "structure": False,
        },
        "artifacts": {
            "normalized": str(normalized_path.relative_to(request.output_dir)),
            "raw_response": str(raw_response_path.relative_to(request.output_dir)),
        },
        "page_coverage": {
            "requested": list(request.page_numbers),
            "produced": [p.page_number for p in document.pages],
        },
        "errors": list(document.errors),
        "extra": extra or {},
    }


class NativeAdapter:
    """Parser adapter that wraps :func:`read_native_pages`."""

    name: str = "native"

    def parse(self, request: ParseRequest) -> ParseArtifacts:
        document = read_native_pages(
            request.source_path,
            pages=tuple(request.page_numbers),
            parser_run_id=f"{request.job_id}:native",
        )

        raw_response_path = request.output_dir / "raw_response.json"
        normalized_path = request.output_dir / "normalized.json"
        manifest_path = request.output_dir / "manifest.json"

        request.output_dir.mkdir(parents=True, exist_ok=True)

        # The native adapter has no "raw response" separate from the
        # normalised payload; we emit the text per line as a debugging aid.
        raw_payload = {
            "schema_version": "1.0.0",
            "engine": "native",
            "pages": [
                {
                    "page_number": page.page_number,
                    "blocks": [
                        {
                            "block_id": block.block_id,
                            "bbox": list(block.bbox) if block.bbox else None,
                            "text": block.text_raw,
                        }
                        for block in page.blocks
                    ],
                }
                for page in document.pages
            ],
        }
        raw_response_path.write_text(
            json.dumps(raw_payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        normalized_path.write_text(
            document.model_dump_json(),
            encoding="utf-8",
        )

        manifest = _manifest(
            request=request,
            document=document,
            normalized_path=normalized_path,
            raw_response_path=raw_response_path,
        )
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        return ParseArtifacts(
            normalized_path=normalized_path,
            raw_response_path=raw_response_path,
            manifest_path=manifest_path,
        )


__all__ = ["NativeAdapter"]