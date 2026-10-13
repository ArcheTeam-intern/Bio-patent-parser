"""Workflow stage definitions.

The stage order is fixed by the design (§8) and is not driven by an LLM or
by CLI wiring. Each :class:`StageHandler` returns a :class:`StageOutcome` and
never depends on Typer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from bio_pdf.contracts import StageOutcome

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
    """Per-stage context handed to a handler."""

    job_id: str
    document_id: str
    source_path: str
    page_numbers: tuple[int, ...]
    output_dir: str
    options: dict[str, Any]


class StageHandler(Protocol):
    """A stage implementation.

    ``name`` must be one of :data:`STAGE_ORDER`. ``cache_key`` returns the
    stable key used to skip already-succeeded stages on resume.
    """

    name: str

    def cache_key(self, context: StageContext) -> str: ...

    def run(self, context: StageContext) -> StageOutcome: ...


__all__ = ["STAGE_ORDER", "StageContext", "StageHandler"]