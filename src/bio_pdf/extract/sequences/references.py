"""SEQ ID reference extraction.

A reference is a *mention* of a sequence identifier in prose, not a
sequence itself. ``SEQ ID NO:7`` and ``SEQ ID NOs:7-9`` produce
:class:`SequenceReference` records only — we never fabricate a sequence
from a mention.
"""

from __future__ import annotations

import re

from bio_pdf.contracts import SequenceReference

#: Match ``SEQ ID NO: 7``, ``SEQ ID NOs: 7-9``, ``SEQ ID No. 7``, case
#: insensitive. The captured group is a comma/range separated number list.
_SEQ_REF_RE = re.compile(
    r"SEQ\s+ID\s+NOS?\s*\.?:?\s*"
    r"([0-9]+(?:\s*(?:[-–—]\s*[0-9]+|\s*,\s*[0-9]+)*))",
    re.IGNORECASE,
)

_SINGLE_NUM = re.compile(r"^\s*([0-9]+)\s*$")
_RANGE_NUM = re.compile(r"^\s*([0-9]+)\s*[-–—]\s*([0-9]+)\s*$")


def _expand_number_list(raw: str) -> list[str]:
    """Expand ``7,9,10-12`` into individual string numbers."""

    out: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if (single := _SINGLE_NUM.match(part)):
            out.append(single.group(1))
            continue
        if (rng := _RANGE_NUM.match(part)):
            start, end = int(rng.group(1)), int(rng.group(2))
            if end >= start:
                out.extend(str(n) for n in range(start, end + 1))
                continue
        # Fallback: keep the raw token (caller can review).
        out.append(part)
    return out


def extract_sequence_references(
    text: str,
    *,
    document_id: str,
) -> list[SequenceReference]:
    """Extract every SEQ ID mention in ``text``.

    Each mention becomes one :class:`SequenceReference` per individual
    number (ranges are expanded). The ``resolved_sequence_id`` stays ``None``
    until a later linking pass resolves it against an actual sequence record.
    """

    references: list[SequenceReference] = []
    for match in _SEQ_REF_RE.finditer(text):
        mention_raw = match.group(0)
        numbers = _expand_number_list(match.group(1))
        for number in numbers:
            references.append(
                SequenceReference(
                    document_id=document_id,
                    seq_id_no=number,
                    mention_raw=mention_raw,
                    evidence_ids=[f"{document_id}:ref:{number}"],
                    resolved_sequence_id=None,
                )
            )
    return references


__all__ = ["extract_sequence_references"]