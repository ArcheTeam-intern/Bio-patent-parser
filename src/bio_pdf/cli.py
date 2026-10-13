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


if __name__ == "__main__":
    app()