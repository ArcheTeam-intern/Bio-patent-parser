"""Parser adapter contracts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

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