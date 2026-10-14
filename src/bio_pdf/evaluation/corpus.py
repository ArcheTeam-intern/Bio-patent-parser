"""Corpus scope for evaluation (plan B10).

A benchmark must know WHERE the human annotation is exhaustive. Outside a
verified scope, candidates are neither TP nor FP: they are unannotated
coverage. A completely unannotated manifest yields ``not_evaluated`` —
never 0%/100% scores.

A scope with ``no_sequence_region`` + zero gold is a CONFIRMED negative
region: every candidate there is a false positive (the reviewer checked
the pages and found no sequences).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CorpusScope:
    """One document's verified annotation scope."""

    document_id: str
    pages: list[int] = field(default_factory=list)
    scope_verified: bool = False
    no_sequence_region: bool = False
    source_sha256: str | None = None

    def evaluates(self, page_number: int | None) -> bool:
        if not self.scope_verified:
            return False
        if page_number is None:
            return True  # document-level scope
        return page_number in self.pages

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "pages": list(self.pages),
            "scope_verified": self.scope_verified,
            "no_sequence_region": self.no_sequence_region,
            "source_sha256": self.source_sha256,
        }


def load_corpus_scope(manifest_path: Path) -> tuple[list[CorpusScope], list[Any]]:
    """Load a v2 manifest; returns (scopes, annotations).

    Expected shape::

        {
          "schema_version": "v2",
          "scopes": [{"document_id": ..., "pages": [...],
                      "scope_verified": true, ...}],
          "annotations": [...]
        }
    """

    from bio_pdf.evaluation.annotation import Annotation

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    scopes = [
        CorpusScope(
            document_id=str(item["document_id"]),
            pages=[int(p) for p in item.get("pages", [])],
            scope_verified=bool(item.get("scope_verified", False)),
            no_sequence_region=bool(item.get("no_sequence_region", False)),
            source_sha256=item.get("source_sha256"),
        )
        for item in payload.get("scopes", [])
    ]
    annotations = [Annotation.model_validate(raw) for raw in payload.get("annotations", [])]
    return scopes, annotations


def evaluation_status(scopes: list[CorpusScope], annotations: list[Any]) -> str:
    """``not_evaluated`` unless at least one verified scope with gold/negatives."""

    verified_scopes = [s for s in scopes if s.scope_verified]
    if not verified_scopes:
        return "not_evaluated"
    has_verified_gold = any(
        getattr(a, "human_verified", False) and getattr(a, "status", "") == "verified"
        for a in annotations
    )
    has_confirmed_negative = any(s.no_sequence_region for s in verified_scopes)
    if not has_verified_gold and not has_confirmed_negative:
        return "not_evaluated"
    return "evaluated"


__all__ = ["CorpusScope", "load_corpus_scope", "evaluation_status"]
