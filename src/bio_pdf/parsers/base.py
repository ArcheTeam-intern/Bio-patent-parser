"""Parser adapter contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

ProfileName = Literal["native", "layout_native", "layout_ocr", "compare"]


@dataclass(frozen=True)
class ParseRequest:
    """A request for one parser run.

    ``options_ref`` is an opaque pointer to the persisted options JSON. It is
    the parser's responsibility to honour the contents.
    """

    job_id: str
    document_id: str
    source_path: Path
    page_numbers: tuple[int, ...]
    profile: ProfileName
    options_ref: str
    output_dir: Path
    #: Effective engine options from the persisted WorkflowConfig (B04):
    #: tableformer_mode / do_table_structure / ocr_lang / timeout.
    parser_options: dict[str, Any] = field(default_factory=dict)
    #: G05/S05: explicit directory for CROSS-ATTEMPT remote state (the
    #: cloud task identity). Comparisons pass a side-level directory here —
    #: their attempt dirs change every retry, so without this the identity
    #: search fell back to the attempt dir and every retry re-uploaded.
    #: None → the adapter keeps its legacy (workflow) discovery.
    remote_state_dir: str | None = None


@dataclass(frozen=True)
class ParseArtifacts:
    """The files written by a parser run.

    The runner is responsible for hashing and registering these in the
    repository.
    """

    normalized_path: Path
    raw_response_path: Path
    manifest_path: Path


class ParserAdapter(Protocol):
    """The interface every adapter implements."""

    name: str

    def parse(self, request: ParseRequest) -> ParseArtifacts: ...


__all__ = [
    "ParseRequest",
    "ParseArtifacts",
    "ParserAdapter",
    "ProfileName",
]