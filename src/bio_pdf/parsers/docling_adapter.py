"""Docling adapter.

The adapter is responsible for:

1. Configuring a :class:`docling.document_converter.DocumentConverter`.
2. Running it on a page range.
3. Persisting the raw response, the normalised document and the manifest.

The actual conversion call is deferred to a child process in the worker
(Task 10) so we never block the main process with model inference.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from bio_pdf.contracts import NormalizedDocument
from bio_pdf.errors import EngineTimeoutError, EngineUnavailableError, WorkflowError
from bio_pdf.parsers.base import ParseArtifacts, ParseRequest
from bio_pdf.parsers.normalize_docling import (
    normalize_docling_document,
)

#: Environment variable carrying the child request payload path.
_DOCLING_CHILD_REQUEST_ENV = "BIO_PDF_DOCLING_CHILD_REQUEST"
#: Environment variable carrying the child result path (reserved for future use).
_DOCLING_CHILD_RESULT_ENV = "BIO_PDF_DOCLING_CHILD_RESULT"


def _kill_process_tree(exc: subprocess.TimeoutExpired) -> None:
    """Terminate a timed-out child. ``run`` already killed the direct child;
    on Windows, torch/tesseract grandchildren may need an explicit taskkill."""

    if os.name == "nt":
        pid = getattr(exc, "pid", None) or getattr(getattr(exc, "args", [None])[0], "pid", None)
        if isinstance(pid, int):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )


def _import_docling() -> Any:
    """Lazy-import the ``docling`` SDK so the core CLI works without it."""

    try:
        import docling  # type: ignore[import-untyped]
    except ImportError as exc:
        raise EngineUnavailableError(
            "docling is not installed; install the 'docling' extra to enable this adapter",
            details={"hint": "uv sync --extra docling"},
        ) from exc
    return docling


def docling_is_available() -> bool:
    """Return True if the ``docling`` package is importable."""

    try:
        import docling  # noqa: F401  # type: ignore[import-untyped]
    except ImportError:
        return False
    return True


def _write_manifest(
    *,
    request: ParseRequest,
    document: NormalizedDocument,
    raw_path: Path,
    normalised_path: Path,
    output_dir: Path,
    extra: dict[str, Any] | None = None,
) -> Path:
    manifest_path = output_dir / "manifest.json"
    manifest = {
        "schema_version": "1.0.0",
        "engine": "docling",
        "parser_run_id": document.parser_run_id,
        "document_id": document.document_id,
        "profile": request.profile,
        "pages": list(request.page_numbers),
        "options_ref": request.options_ref,
        "capabilities": {
            "tables": True,
            "ocr": request.profile == "layout_ocr",
            "structure": True,
        },
        "artifacts": {
            "normalized": "normalized.json",
            "raw_response": "raw_response.json",
        },
        "page_coverage": {
            "requested": list(request.page_numbers),
            "produced": [p.page_number for p in document.pages],
        },
        "errors": list(document.errors),
        "extra": extra or {},
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest_path


def _build_converter_from_request(request_payload: dict[str, Any]) -> Any:
    """Rebuild a Docling converter inside the child process from the request.

    Plan B04: the configured ``parser_options`` (tableformer mode,
    do_table_structure, ocr_lang) really reach the converter — previously
    the options changed only the cache hash while the engine kept its
    defaults.
    """

    # pylint: disable=import-outside-toplevel
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        PdfPipelineOptions,
        TableStructureOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption

    profile = request_payload["profile"]
    do_ocr = profile == "layout_ocr"
    raw_options = request_payload.get("parser_options") or {}

    options = PdfPipelineOptions()
    options.do_ocr = do_ocr
    options.do_table_structure = bool(raw_options.get("do_table_structure", True))
    options.force_backend_text = not do_ocr

    tableformer_mode = raw_options.get("tableformer_mode")
    table_kwargs: dict[str, Any] = {"do_cell_matching": True}
    if tableformer_mode:
        try:
            from docling.datamodel.pipeline_options import TableFormerMode

            mode_enum = (
                TableFormerMode.ACCURATE
                if str(tableformer_mode).lower() == "accurate"
                else TableFormerMode.FAST
            )
            table_kwargs["mode"] = mode_enum
        except Exception:  # noqa: BLE001 - installed SDK may not expose it
            pass
    options.table_structure_options = TableStructureOptions(**table_kwargs)

    if do_ocr:
        from bio_pdf.parsers.ocr import resolve_ocr_options

        from docling.datamodel.pipeline_options import (
            OcrMode,
            TesseractCliOcrOptions,
        )

        runtime = resolve_ocr_options(raw_options)
        ocr_kwargs = {
            "mode": OcrMode.FULL_PAGE,
            "lang": [runtime["effective_language"]],
            "scale": 300 / 72,
        }
        if runtime["selected_engine"] == "rapidocr":
            from docling.datamodel.pipeline_options import RapidOcrOptions

            options.ocr_options = RapidOcrOptions(backend="onnxruntime", **ocr_kwargs)
        else:
            options.ocr_options = TesseractCliOcrOptions(**ocr_kwargs)

    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )
    # K08: when the budget test sets BIO_PDF_K08_HOLD_DIR the
    # grandchild wraps ``convert`` so the outer scenario can observe
    # the descendant in a held state while the watchdog fires. Only
    # active under that env var; production paths are untouched.
    if os.environ.get("BIO_PDF_K08_HOLD_DIR"):
        import importlib
        import sys

        # The docling grandchild does NOT inherit the parent's
        # sys.path additions — ensure the test helper is reachable.
        repo_root = Path(__file__).resolve().parents[3]
        tests_dir = repo_root / "tests"
        for entry in (str(tests_dir), str(repo_root)):
            if entry not in sys.path:
                sys.path.insert(0, entry)
        try:
            from tests.helpers.docling_hold_bootstrap import (
                apply_hold_if_configured,
            )
        except ImportError:  # pragma: no cover — only test scenarios
            apply_hold_if_configured = None  # type: ignore[assignment]
        if apply_hold_if_configured is not None:
            apply_hold_if_configured(converter, request_payload)
    return converter


class DoclingAdapter:
    """Adapter that delegates to the real Docling SDK via a separate process.

    The current implementation invokes :func:`_child_main` in a fresh Python
    process using :mod:`subprocess`. We avoid in-process model loading so the
    main process stays responsive.
    """

    name: str = "docling"

    def parse(self, request: ParseRequest) -> ParseArtifacts:
        if not docling_is_available():
            raise EngineUnavailableError(
                "docling is not installed; cannot run Docling adapter",
                details={"hint": "uv sync --extra docling"},
            )

        request.output_dir.mkdir(parents=True, exist_ok=True)
        raw_path = (request.output_dir / "raw_response.json").resolve()
        normalised_path = (request.output_dir / "normalized.json").resolve()

        from bio_pdf.parsers.ocr import resolve_ocr_options

        ocr_runtime = (
            resolve_ocr_options(dict(request.parser_options or {}))
            if request.profile == "layout_ocr" else None
        )
        effective_parser_options = dict(request.parser_options or {})
        if ocr_runtime:
            # Pin the resolved engine before entering the child process.
            effective_parser_options["ocr_engine"] = ocr_runtime["selected_engine"]
            effective_parser_options["ocr_lang"] = ocr_runtime["effective_language"]

        request_payload = {
            "request": {
                "job_id": request.job_id,
                "document_id": request.document_id,
                "source_path": str(Path(request.source_path).resolve()),
                "page_numbers": list(request.page_numbers),
                "profile": request.profile,
                "options_ref": request.options_ref,
                "raw_path": str(raw_path),
                "normalised_path": str(normalised_path),
                "timeout_seconds": float(
                    effective_parser_options.get("engine_timeout_seconds") or 900.0
                ),
                # B04: configured engine options really reach the converter.
                "parser_options": effective_parser_options,
            }
        }
        payload_path = (request.output_dir / "docling_request.json").resolve()
        payload_path.write_text(
            json.dumps(request_payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        env = os.environ.copy()
        # Absolute paths only: the child runs with cwd=output_dir, so a
        # relative payload path would resolve against the wrong directory.
        env[_DOCLING_CHILD_REQUEST_ENV] = str(payload_path)

        timeout = float(request_payload["request"].get("timeout_seconds") or 900.0)
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "bio_pdf.parsers.docling_child"],
                env=env,
                cwd=str(request.output_dir),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            # Never leave the inference child running (plan T04).
            _kill_process_tree(exc)
            raise EngineTimeoutError(
                f"docling child exceeded {timeout:.0f}s and was terminated",
                details={"timeout_seconds": timeout},
            ) from exc
        (request.output_dir / "engine.stdout.log").write_text(completed.stdout or "", encoding="utf-8")
        (request.output_dir / "engine.stderr.log").write_text(completed.stderr or "", encoding="utf-8")
        if completed.returncode != 0:
            raise WorkflowError(
                "docling child process failed",
                details={
                    "returncode": completed.returncode,
                    "stderr_tail": completed.stderr[-2000:] if completed.stderr else "",
                },
            )

        # The child has already written the strict NormalizedDocument contract
        # to disk; load it back so the manifest reflects exactly what a disk
        # consumer would read.
        document = NormalizedDocument.model_validate_json(
            normalised_path.read_text(encoding="utf-8")
        )

        manifest_path = _write_manifest(
            request=request,
            document=document,
            raw_path=raw_path,
            normalised_path=normalised_path,
            output_dir=request.output_dir,
            extra={"ocr_runtime": ocr_runtime},
        )

        return ParseArtifacts(
            normalized_path=normalised_path,
            raw_response_path=raw_path,
            manifest_path=manifest_path,
        )


__all__ = ["DoclingAdapter", "docling_is_available"]
