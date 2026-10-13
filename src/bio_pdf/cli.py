"""Command-line entry point.

The CLI is intentionally thin: it probes the environment and forwards to the
workflow runner. Domain logic lives outside the Typer layer so future HTTP
and SDK surfaces can share it.
"""

from __future__ import annotations

import importlib.util
import json
import platform
import sys
from pathlib import Path
from typing import Any, Optional

import typer

app = typer.Typer(
    no_args_is_help=True,
    help=(
        "Bio PDF workflow CLI. Provides environment probing now and "
        "ingest/run/export commands in later milestones."
    ),
)


@app.callback()
def _main_callback() -> None:
    """Top-level Typer callback.

    Defining an explicit callback is required in Typer >=0.16 so that the
    application is wrapped as a :class:`typer.core.TyperGroup` and routes
    sub-commands like ``doctor`` correctly.
    """


def _module_status(names: tuple[str, ...]) -> dict[str, bool]:
    """Return a mapping of module name → whether it can be imported."""

    return {name: importlib.util.find_spec(name) is not None for name in names}


@app.command("doctor")
def doctor() -> None:
    """Probe the runtime environment without touching models or remote APIs."""

    result: dict[str, Any] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "executable": sys.executable,
        "modules": _module_status(
            (
                "pydantic",
                "typer",
                "pypdfium2",
                "docling",
                "httpx",
            )
        ),
        # We never implicitly install or load models; downstream stages will
        # report their own readiness when they actually need a model.
        "model_readiness": "not_checked",
    }
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("inventory")
def inventory(
    input_dir: Path = typer.Option(..., "--input", help="Directory containing PDF files."),
    output: Optional[Path] = typer.Option(
        None, "--out", help="Optional path for the JSON inventory report."
    ),
) -> None:
    """Inspect every PDF under ``input_dir`` and emit an inventory report.

    The original files are never modified. Per-file errors (HTML disguised as
    PDF, encrypted files, …) are reported with their ``WorkflowError`` code.
    """

    from bio_pdf.ingest.inspector import (
        build_inventory_report,
        inspect_directory,
    )

    if not input_dir.is_dir():
        typer.echo(
            json.dumps(
                {"error": "INPUT_NOT_DIR", "path": str(input_dir)},
                ensure_ascii=False,
            )
        )
        raise typer.Exit(code=2)

    records = inspect_directory(input_dir)
    report = build_inventory_report(records)
    payload = json.dumps(report, ensure_ascii=False, indent=2)

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
        typer.echo(f"wrote {output}")
    else:
        typer.echo(payload)


@app.command("parse")
def parse_pdf(
    input: Path = typer.Option(..., "--input", help="Path to a single PDF file."),
    pages: str = typer.Option("all", "--pages", help="Page selection, e.g. '1-3,7'."),
    engine: str = typer.Option("native", "--engine", help="Parser engine (native or docling)."),
    output_dir: Optional[Path] = typer.Option(
        None, "--out", help="Directory for normalized/raw/manifest artifacts."
    ),
) -> None:
    """Parse a single PDF with the given engine and write normalized artifacts."""

    from bio_pdf.errors import EngineUnavailableError, WorkflowError
    from bio_pdf.ingest.inspector import inspect_pdf, parse_page_selection
    from bio_pdf.parsers.base import ParseRequest
    from bio_pdf.parsers.native_adapter import NativeAdapter

    try:
        inspection = inspect_pdf(input)
    except WorkflowError as exc:
        typer.echo(json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False))
        raise typer.Exit(code=2) from exc

    try:
        page_numbers = parse_page_selection(pages, page_count=inspection.page_count)
    except WorkflowError as exc:
        typer.echo(json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False))
        raise typer.Exit(code=2) from exc

    out = output_dir or (input.parent / "out")
    out.mkdir(parents=True, exist_ok=True)

    if engine == "native":
        adapter = NativeAdapter()
    elif engine == "docling":
        from bio_pdf.parsers.docling_adapter import DoclingAdapter

        adapter = DoclingAdapter()
    elif engine == "docling_serve":
        from bio_pdf.parsers.docling_serve_adapter import DoclingServeAdapter

        adapter = DoclingServeAdapter()
    else:
        typer.echo(json.dumps({"error": "ENGINE_UNAVAILABLE", "engine": engine}, ensure_ascii=False))
        raise typer.Exit(code=2)

    request = ParseRequest(
        job_id=f"cli-{inspection.sha256[:12]}",
        document_id=inspection.document_id,
        source_path=input,
        page_numbers=page_numbers,
        profile="native" if engine == "native" else "layout_native",
        options_ref="configs/demo.toml",
        output_dir=out,
    )

    try:
        artifacts = adapter.parse(request)
    except WorkflowError as exc:
        typer.echo(json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False))
        raise typer.Exit(code=3) from exc

    typer.echo(
        json.dumps(
            {
                "document_id": inspection.document_id,
                "engine": engine,
                "pages": list(page_numbers),
                "normalized": str(artifacts.normalized_path),
                "manifest": str(artifacts.manifest_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("run")
def run_job(
    input: Path = typer.Option(..., "--input", help="Path to a single PDF file."),
    engine: str = typer.Option("native", "--engine", help="Layout engine (native or docling)."),
    pages: str = typer.Option("all", "--pages", help="Page selection, e.g. '1-3,7'."),
    data_dir: Path = typer.Option(
        Path("data/demo"), "--data-dir", help="Application data directory (SQLite + artifacts)."
    ),
    resume: bool = typer.Option(False, "--resume", help="Resume an existing job with cache verification."),
) -> None:
    """Run the complete domain workflow: inspect → parse → extract → validate → export.

    This is the production entry point (design D0): it produces sequence and
    activity records, a review queue and the export artifacts — not just a
    parse dump.
    """

    from bio_pdf.application.job_service import JobPaths, JobService
    from bio_pdf.errors import WorkflowError
    from bio_pdf.ingest.inspector import parse_page_selection

    if not input.is_file():
        typer.echo(json.dumps({"error": "INPUT_NOT_PDF", "message": f"not a file: {input}"}, ensure_ascii=False))
        raise typer.Exit(code=2)

    service = JobService(JobPaths.under(data_dir))
    try:
        try:
            from bio_pdf.ingest.inspector import inspect_pdf as _inspect

            page_count = _inspect(input).page_count
            page_numbers = parse_page_selection(pages, page_count=page_count)
        except WorkflowError as exc:
            typer.echo(json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False))
            raise typer.Exit(code=2) from exc

        job, _page_count = service.create_job(
            input,
            engine=engine,
            page_numbers=page_numbers,
        )
        typer.echo(json.dumps({"job_id": job.job_id, "status": job.status, "pages": list(page_numbers)}, ensure_ascii=False))

        result = service.run_job(job.job_id, resume=resume)
        payload = {
            "job_id": result.job_id,
            "status": result.status,
            "stages_run": result.stages_run,
            "stages_skipped": result.stages_skipped,
            "review_count": result.review_count,
            "failed_stage": result.failed_stage,
            "export": str((service.paths.artifact_root / "jobs" / job.job_id / "export" / "result.json")),
        }
        typer.echo(json.dumps(payload, ensure_ascii=False))
        if result.status == "failed":
            raise typer.Exit(code=3)
    finally:
        service.close()


if __name__ == "__main__":
    app()