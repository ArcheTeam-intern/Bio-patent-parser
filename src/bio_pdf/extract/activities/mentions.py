"""Mention role classification (plan T08, per-mention in B05).

A number+unit in prose is judged from its LOCAL CLAUSE plus sentence and
section context — not by a fixed whole-sentence priority:

- ``condition``  — treatment/incubation dose and time ("treated with 20 µM
  peptide A") belongs to the assay recipe. A dose in one clause does not
  condemn a result in another clause of the same sentence.
- ``statistic``  — ONLY when the mention itself carries statistical
  qualifiers (confidence interval, SD/SEM attached to the value, n=).
  "(mean ± SD)" after a plus-minus value describes the ERROR and must not
  remove the measurement; a bare "Figure 1"/"Table 2" is a LOCATION clue,
  never a veto.
- ``metadata``   — dates, DOIs, page/pagination tokens (never figure refs).
- ``claim``      — claims-section or explicit claim numbering ("Claim 1:");
  "less than" in a results sentence is a VALUE COMPARISON (operator), not
  a patent claim.
- ``result``     — result verbs / measured outcomes.
- ``undetermined`` — no signal; the caller keeps a typed RawMention, never
  a formal unknown Measurement.

Output roles feed either a Measurement (result) or a RawMention (all
other roles and undetermined).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Date/pagination/DOI metadata (figure/table references are NOT here).
_METADATA_RE = re.compile(
    r"(\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{4}\b|\baccepted:?\b|\breceived:?\b|"
    r"\brevised:?\b|\bdoi:|\bissn\b|\bisbn\b|\bpp\.\s*\d+)",
    re.IGNORECASE,
)
#: A bare year in a citation-like clause is metadata.
_YEAR_ONLY_RE = re.compile(r"\((?:19|20)\d{2}\)")

#: Statistical qualifiers ATTACHED to the mention (not just anywhere).
#: NOTE: "(mean ± SD)" after a ± value describes the ERROR — it never
#: vetoes the measurement (plan B05); only true statistical constructs
#: (confidence interval, n=, SD/SEM as the measured quantity) count.
_STATISTIC_ATTACHED_RE = re.compile(
    r"(\b\d{2}\s*%\s*(?:confidence|ci)\b|\bconfidence interval\b|"
    r"\bs\.?d\.?\s*[:=]|\bs\.?e\.?m\.?\s*[:=]|\bn\s*=\s*\d+)",
    re.IGNORECASE,
)

#: Treatment/condition verbs — evaluated per CLAUSE.
_CONDITION_RE = re.compile(
    r"\b(treated with|treated by|incubated with|incubated at|cultured in|"
    r"cultured at|grown in|grown at|diluted to|suspended in|supplemented with|"
    r"resuspended in|maintained in|administrated|administered|injected|"
    r"challenged with|infected with)\b",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(
    r"\bfor\s+\d+(\.\d+)?\s*(h\b|hr|hrs|hours|min|minutes|s\b|sec|days?|weeks?)",
    re.IGNORECASE,
)

#: Explicit claim context (patent claims section or claim numbering).
_CLAIM_CONTEXT_RE = re.compile(
    r"(\bclaim(?:s)?\s+\d+|\bin\s+(?:the\s+)?claims?\b|\bclaimed\s+is\b|"
    r"\bwhat\s+is\s+claimed\b)",
    re.IGNORECASE,
)

#: Result verbs / measured outcomes.
_RESULT_RE = re.compile(
    r"\b(inhibit(?:ed|ion|s)?|reduc(?:ed|es|ing)|increas(?:ed|es|ing)|"
    r"decreas(?:ed|es|ing)|viability|activity|killed|eradicated|effective|"
    r"potent|protected|sensitiz(?:ed|es)|survived|MIC|IC50|EC50|MBC)\b",
    re.IGNORECASE,
)

#: Metric words that make a bare percent a result (viability/inhibition).
_PERCENT_METRIC_MAP = (
    (re.compile(r"\bviabilit(?:y|ies)\b", re.IGNORECASE), "viability"),
    (re.compile(r"\binhibit(?:ion|ed)?\b", re.IGNORECASE), "inhibition"),
)


@dataclass(frozen=True)
class RoleGuess:
    role: str
    reasons: list[str]


def split_clauses(text: str) -> list[tuple[str, int]]:
    """Split a sentence into (clause, offset) pairs on , ; and subordinators.

    The clause carries the local context a mention is judged on; the
    offset lets callers locate the mention inside the original text.
    """

    out: list[tuple[str, int]] = []
    start = 0
    for m in re.finditer(r"[,;]|\b(?:reducing|while|whereas|thereby|leading to)\b", text):
        piece = text[start : m.start()]
        if piece.strip():
            out.append((piece, start))
        # For subordinator words the word itself starts the next clause.
        start = m.start() if m.group(0)[0].isalpha() else m.end()
    tail = text[start:]
    if tail.strip():
        out.append((tail, start))
    return out


def clause_for(text: str, start: int, end: int) -> tuple[str, int]:
    """Return the clause (and its absolute offset) containing [start, end)."""

    best = (text, 0)
    for clause, offset in split_clauses(text):
        if offset <= start < offset + len(clause):
            best = (clause, offset)
    return best


def classify_mention_role(clause: str, sentence: str = "") -> RoleGuess:
    """Classify a mention from its clause; ``sentence`` adds claim context."""

    scope = sentence or clause

    # Claim context is judged on the whole sentence/claim numbering.
    if _CLAIM_CONTEXT_RE.search(scope):
        return RoleGuess("claim", ["explicit claim context"])

    # Statistic ONLY when the qualifier attaches to the mention clause.
    if _STATISTIC_ATTACHED_RE.search(clause):
        return RoleGuess("statistic", ["statistical qualifier attached to the value"])

    # Metadata: dates/pagination/DOI — never figure references.
    if _METADATA_RE.search(clause) or _YEAR_ONLY_RE.search(clause):
        return RoleGuess("metadata", ["date/DOI/pagination token"])

    # Condition: treatment verbs in THIS clause (a dose in another clause
    # of the sentence does not condemn a result here).
    if _CONDITION_RE.search(clause):
        return RoleGuess("condition", ["treatment/administration language in clause"])
    if _DURATION_RE.search(clause) and not _RESULT_RE.search(clause):
        return RoleGuess(
            "condition", ["dose/time phrase without a result verb"]
        )

    if _RESULT_RE.search(clause):
        return RoleGuess("result", ["result language in clause"])

    return RoleGuess("undetermined", ["no role evidence in clause"])


def resolve_percent_metric(clause: str) -> str | None:
    """Map a bare % mention to a canonical metric from its clause."""

    for pattern, metric in _PERCENT_METRIC_MAP:
        if pattern.search(clause):
            return metric
    return None


__all__ = [
    "RoleGuess",
    "classify_mention_role",
    "clause_for",
    "resolve_percent_metric",
    "split_clauses",
]
