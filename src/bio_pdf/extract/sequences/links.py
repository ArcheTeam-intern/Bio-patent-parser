"""Sequence ↔ activity candidate links (never a silent hard link).

A patent's sequence listing and its activity tables usually use DIFFERENT
identifiers: the listing numbers declarations ``<210> 4`` while the table
rows name compounds or variants (``Chain 104``, ``Chain 204``). The prose
is the only place the two meet:

    "Met10"-peptide (Chain 100) defined by SEQ ID NO: 4

This module extracts those statements and turns them into CANDIDATE links
between a measurement's entity and a SEQ ID. It deliberately does NOT
write ``Measurement.sequence_ids``: a wrong link is worse than no link,
so every candidate carries its source sentence and stays unconfirmed
until a human accepts it.

Mapping rules (kept small and auditable):

- an exact statement ``Chain 104 ... SEQ ID NO: 4`` links that entity only;
- a "family" statement ``Chain 100 ... SEQ ID NO: 4`` links the whole
  hundred-block (104, 105, ...) because patents name variants within a
  family by their first digits;
- no statement, no candidate — proximity alone is never evidence;
- an entity that matches two statements keeps BOTH candidates (ambiguity
  is reported, not resolved).
"""

from __future__ import annotations

import re
from typing import Any

#: ``Chain 104`` / ``chain. 104`` / ``Chain ID 104``.
_CHAIN_RE = re.compile(r"\bchain(?:\s*(?:id|no\.?))?\s*[-:#]?\s*(\d{1,4})\b", re.IGNORECASE)
#: ``SEQ ID NO: 4`` / ``SEQ ID NO.4`` / ``SEQ NO: 4``.
_SEQ_RE = re.compile(
    r"\bseq(?:uence)?\s*(?:id)?\s*(?:no\.?|number)?\s*[:#]?\s*(\d{1,4})\b",
    re.IGNORECASE,
)

#: Sentence boundary for pairing a chain and a sequence in one statement.
#: NOT used for splitting: abbreviations ("SEQ ID No. 6") break naive
#: splitting, so pairing is done by CHARACTER DISTANCE instead.
_SENTENCE_SPLIT = re.compile(r"(?<=[.;!?])\s+|\n+")

#: A chain and its sequence must appear within this many characters of
#: each other; beyond that the block is listing several unrelated items.
_MAX_PAIR_DISTANCE = 240


def _pair_chains_to_sequences(text: str) -> list[tuple[re.Match[str], re.Match[str]]]:
    """Pair every ``Chain N`` with its NEAREST ``SEQ ID NO: M``.

    Distance-based so abbreviations ("No.", "Fig.") and multi-sentence
    blocks both work: "…Chain 100… SEQ ID NO: 4. …Chain 200… SEQ ID NO: 5."
    pairs 100→4 and 200→5, never 100→5.
    """

    chains = list(_CHAIN_RE.finditer(text))
    sequences = list(_SEQ_RE.finditer(text))
    if not chains or not sequences:
        return []
    pairs: list[tuple[re.Match[str], re.Match[str]]] = []
    for chain in chains:
        nearest = min(
            sequences,
            key=lambda s: min(abs(s.start() - chain.end()), abs(chain.start() - s.end())),
        )
        distance = min(
            abs(nearest.start() - chain.end()), abs(chain.start() - nearest.end())
        )
        if distance <= _MAX_PAIR_DISTANCE:
            pairs.append((chain, nearest))
    return pairs


def _entity_token(entity_raw: str | None) -> str | None:
    """Reduce a table entity label to its numeric chain token.

    ``Chain201`` → ``201``, ``104 D`` → ``104``, ``104`` → ``104``.
    Anything without a leading number is not linkable.
    """

    if not entity_raw:
        return None
    text = str(entity_raw).strip()
    text = re.sub(r"^chain\s*[-:#]?\s*", "", text, flags=re.IGNORECASE)
    match = re.match(r"(\d{1,4})", text)
    return match.group(1) if match else None


def _hundred_family(token: str) -> str | None:
    """``104`` → ``1xx``; only for 3-4 digit tokens with a trailing block."""

    if len(token) < 3:
        return None
    return token[0] + "xx"


def extract_sequence_links(
    blocks: list[dict[str, Any]],
    measurements: list[Any],
    *,
    max_candidates: int = 200,
) -> dict[str, Any]:
    """Build candidate entity → SEQ ID links from the document prose.

    ``blocks`` are the extract-feed dicts (``text``, ``page_number``,
    ``block_id``); ``measurements`` are :class:`Measurement` objects (or
    their dict form). Returns::

        {
          "candidates": [
            {"entity_raw", "entity_token", "seq_id_no", "match_kind",
             "matched_chain", "page_number", "block_id", "source_text",
             "status": "candidate"}
          ],
          "statements": [ ...every parsed statement, linked or not... ],
          "note": "candidates only — a human must confirm",
        }
    """

    statements: list[dict[str, Any]] = []
    for block in blocks:
        text = str(block.get("text") or "")
        if not text or "chain" not in text.lower():
            continue
        if _SEQ_RE.search(text) is None:
            continue
        page_number = int(block.get("page_number") or 0) or None
        block_id = str(block.get("block_id") or "")
        for chain, sequence in _pair_chains_to_sequences(text):
            chain_no = chain.group(1)
            seq_id_no = sequence.group(1)
            # Keep a readable window around the pair as the audit trail.
            window_start = max(0, min(chain.start(), sequence.start()) - 80)
            window_end = min(len(text), max(chain.end(), sequence.end()) + 80)
            statements.append(
                {
                    "chain": chain_no,
                    "seq_id_no": seq_id_no,
                    "match_kind": (
                        "family"
                        if chain_no.endswith("00") and len(chain_no) >= 3
                        else "exact"
                    ),
                    "page_number": page_number,
                    "block_id": block_id,
                    "source_text": text[window_start:window_end].strip()[:300],
                }
            )

    by_exact: dict[str, list[dict[str, Any]]] = {}
    by_family: dict[str, list[dict[str, Any]]] = {}
    for statement in statements:
        chain_no = statement["chain"]
        if statement["match_kind"] == "family":
            family = _hundred_family(chain_no)
            if family:
                by_family.setdefault(family, []).append(statement)
        by_exact.setdefault(chain_no, []).append(statement)

    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for measurement in measurements:
        entity_raw = (
            measurement.get("entity_raw")
            if isinstance(measurement, dict)
            else getattr(measurement, "entity_raw", None)
        )
        token = _entity_token(entity_raw)
        if token is None:
            continue
        matches = list(by_exact.get(token, []))
        family = _hundred_family(token)
        if family:
            matches.extend(by_family.get(family, []))
        for statement in matches:
            key = (str(entity_raw), statement["seq_id_no"])
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    "entity_raw": str(entity_raw),
                    "entity_token": token,
                    "seq_id_no": statement["seq_id_no"],
                    "match_kind": statement["match_kind"],
                    "matched_chain": statement["chain"],
                    "page_number": statement["page_number"],
                    "block_id": statement["block_id"],
                    "source_text": statement["source_text"],
                    "status": "candidate",
                }
            )
            if len(candidates) >= max_candidates:
                break
        if len(candidates) >= max_candidates:
            break

    return {
        "candidates": candidates,
        "statements": statements[:100],
        "note": (
            "entity → SEQ ID candidates parsed from prose; NOT applied to "
            "measurement.sequence_ids — a human must confirm each link"
        ),
    }


__all__ = ["extract_sequence_links"]
