"""Extraction batch contracts — the single return shape of every extractor.

Sequence and activity extractors return an :class:`ExtractionBatch`
(records + evidence spans + raw mentions + diagnostics). ``RawMention``
carries numeric/text observations that could NOT be confirmed as domain
records (conditions, statistics, dates with unit-looking suffixes,
unclassified table values). Mentions are first-class artifacts for review;
they are never counted as Measurements.

The two domain record types are kept in ``records.py``; this module only
re-exports what consumers need so there is exactly one definition.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from bio_pdf.contracts.common import EvidenceSpan, _StrictModel
from bio_pdf.contracts.records import Measurement, SequenceRecord

__all__ = [
    "RawMention",
    "ExtractionBatch",
    "ParseIssue",
    "Measurement",
    "SequenceRecord",
]


class RawMention(_StrictModel):
    """An unclassified numeric/text mention kept for review.

    ``role`` is the classifier's guess (condition/statistic/metadata/
    claim/cited/undetermined); ``reason_codes`` say why it did not become a
    Measurement. Mentions carry real evidence — they are auditable, not
    silently dropped.
    """

    mention_id: str
    text: str
    role: str
    evidence_ids: list[str]
    reason_codes: list[str] = Field(default_factory=list)


class ExtractionBatch(_StrictModel):
    """The uniform output of one extractor run over one input scope."""

    records: list[SequenceRecord | Measurement] = Field(default_factory=list)
    evidence_spans: list[EvidenceSpan] = Field(default_factory=list)
    raw_mentions: list[RawMention] = Field(default_factory=list)
    diagnostics: list[dict[str, str]] = Field(default_factory=list)


class ParseIssue(_StrictModel):
    """A structured, auditable processing problem (plan B06).

    Unlike a diagnostic string, an issue has a scope (table/region/
    mention/sentence), the source objects it concerns, a stable reason
    code, and a lifecycle state — a reviewer can open it, request a
    re-parse or mark it out-of-target, and the decision is auditable.
    """

    issue_id: str
    job_id: str
    scope: Literal["table", "region", "mention", "sentence", "document"]
    reason_code: str
    source_ids: list[str] = Field(default_factory=list)
    detail: str = ""
    state: Literal["open", "reparse_requested", "dismissed", "resolved"] = "open"
