"""Tests for the top-level CLI surface."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from bio_pdf.cli import app

runner = CliRunner()


def test_help_lists_doctor_command() -> None:
    """``--help`` must list the doctor command without exiting with an error."""

    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "doctor" in result.stdout


def test_doctor_emits_machine_readable_json() -> None:
    """The doctor command must emit JSON with module availability, no models loaded."""

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["python"]
    assert "pydantic" in payload["modules"]
    assert "pypdfium2" in payload["modules"]
    # We never implicitly load models in the doctor probe.
    assert payload["model_readiness"] == "not_checked"


def test_run_without_command_shows_help() -> None:
    """Running the app with no command must show help (no_args_is_help=True).

    Typer >=0.16 with a callback returns a non-zero exit when the app is run
    with no sub-command but the help text is still printed. We assert on
    the help body rather than the exit code so the test stays aligned with
    the user-visible behaviour of the CLI.
    """

    result = runner.invoke(app, [])
    assert "Bio PDF workflow CLI" in result.stdout or "Usage:" in result.stdout