"""Inline sequence candidates from prose/blocks.

This extractor is deliberately conservative. A run of uppercase letters is
NOT a sequence just because it matches the protein alphabet. We only emit a
candidate when the text is *explicitly* sequence-shaped:

- a long run (>= 15 residues) of a single-letter alphabet, in a body block
  (not a title / caption), with no prose noise, OR
- text following an explicit ``SEQ ID NO:N`` lead-in.

Title blocks, ``% identity`` prose and other short runs are rejected.
"""

from __future__ import annotations

import re
from typing import Any

from bio_pdf.contracts import SequenceRecord, TextBlock
from bio_pdf.extract.sequences.amino_acids import ALLOWED_ALPHABETS

#: Minimum single-letter run length to be considered an explicit candidate.
_MIN_RUN = 15

#: Words that indicate a non-sequence prose context and veto a candidate.
_VETO_WORDS = ("identity", "homolog", "similarity", "title", "figure", "table")

#: A clean single-letter residue token (may carry whitespace around it).
_CLEAN_RUN_RE = re.compile(r"^[A-Za-z\s]+$")


def _is_body_block(block: dict[str, Any]) -> bool:
    kind = str(block.get("kind", "text")).lower()
    return kind not in {"title", "picture", "formula"}


def _looks_like_sequence_run(text: str) -> bool:
    """Return True if ``text`` is one long single-letter residue run."""

    compact = "".join(ch for ch in text if not ch.isspace())
    if len(compact) < _MIN_RUN:
        return False
    if not _CLEAN_RUN_RE.match(text.strip()):
        return False
    return True


def _contains_veto(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in _VETO_WORDS)


def extract_inline_sequences(
    blocks: list[dict[str, Any]],
    document_id: str,
) -> list[SequenceRecord]:
    """Extract conservative inline sequence candidates.

    Args:
        blocks: List of dicts with at least ``text`` and optionally ``kind``
            and ``page_number``.
        document_id: Canonical document id.

    Returns:
        A list of :class:`SequenceRecord` candidates (validation and
        acceptance left ``unchecked``/``candidate``).
    """

    records: list[SequenceRecord] = []
    for idx, block in enumerate(blocks):
        text = str(block.get("text") or "")
        if not text.strip():
            continue
        if not _is_body_block(block):
            continue
        if _contains_veto(text):
            continue
        if not _looks_like_sequence_run(text):
            continue

        compact = "".join(ch for ch in text if not ch.isspace()).upper()
        page_number = int(block.get("page_number", 1))
        records.append(
            SequenceRecord(
                sequence_id=f"{document_id}:inline:{idx}",
                document_id=document_id,
                seq_id_no=None,
                molecule_type="unknown",
                representation="one_letter",
                declared_length=None,
                observed_length=len(compact),
                raw_sequence=text.strip(),
                normalized_sequence=compact,
                residues=[],
                is_complete=False,
                is_explicit_sequence=False,
                evidence_ids=[f"{document_id}:p{page_number}:inline:{idx}"],
                checks=[],
                validation_state="unchecked",
                acceptance_state="candidate",
            )
        )
    return records


__all__ = ["extract_inline_sequences"]