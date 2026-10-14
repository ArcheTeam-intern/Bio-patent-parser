"""Workflow stage definitions.

The stage order is fixed by the design (§8) and is not driven by an LLM or
by CLI wiring. Each :class:`StageHandler` returns a :class:`StageOutcome`
and never depends on Typer.

T02: handlers run in an isolated *stage-run* directory
(``jobs/<job>/stage_runs/<stage_run_id>/``), receive the inputs they may
read as :class:`ArtifactRef` maps (never inferred fixed paths), and declare
implementation/provider versions so cache keys track code and engine
changes instead of file mtimes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from bio_pdf.contracts import ArtifactRef, StageOutcome

#: Fixed stage order. ``reparse``/``validate_after_reparse`` are optional and
#: are skipped in the first phase (no second pass configured).
STAGE_ORDER: tuple[str, ...] = (
    "ingest",
    "inspect",
    "native_parse",
    "layout_parse",
    "normalize",
    "extract",
    "validate",
    "export",
)


@dataclass(frozen=True)
class StageContext:
    """Per-stage context handed to a handler.

    ``stage_run_id`` identifies this attempt; every artifact the handler
    writes must land under ``output_dir/stage_runs/<stage_run_id>/`` so a
    second attempt never overwrites a previous success. ``inputs`` carries
    the upstream artifacts this stage may consume, keyed by
    ``<stage_name>.<logical_name>``.
    """

    job_id: str
    document_id: str
    source_path: str
    page_numbers: tuple[int, ...]
    output_dir: str
    options: dict[str, Any]
    stage_run_id: str = ""
    document_sha256: str = ""
    inputs: dict[str, ArtifactRef] = field(default_factory=dict)

    def stage_dir(self) -> str:
        """Directory this attempt's artifacts must be written to."""

        from pathlib import Path

        return str(Path(self.output_dir) / "stage_runs" / self.stage_run_id)


class StageHandler(Protocol):
    """A stage implementation.

    ``name`` must be one of :data:`STAGE_ORDER`. ``cache_key`` returns the
    stable key used to skip already-succeeded stages on resume.
    ``implementation_version`` bumps when the stage's code semantics change;
    ``provider_version`` reports the external engine version (empty for
    pure-Python stages).
    """

    name: str

    def cache_key(self, context: StageContext) -> str: ...

    def run(self, context: StageContext) -> StageOutcome: ...

    def implementation_version(self) -> str: ...

    def provider_version(self, context: StageContext) -> str: ...


__all__ = ["STAGE_ORDER", "StageContext", "StageHandler"]
