"""Mention role classification (plan T08).

A number+unit in prose is not automatically an activity result:

- ``condition``  — treatment/incubation dose and time ("cells were treated
  with 20 µM peptide A for 2 h") belongs to the assay recipe;
- ``statistic``  — confidence intervals, ± values in error bars, n= counts;
- ``metadata``   — dates, page numbers, IDs ("Accepted: 17 May 2017");
- ``claim``      — threshold language in claims ("less than", "at least");
- ``result``     — a measured outcome stated as such.

Only ``result`` (and well-evidenced unclassified statements) may become a
Measurement; everything else is a RawMention with its role and reasons.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ROLE_ORDER = ("metadata", "statistic", "condition", "claim", "result", "undetermined")

#: Date/metadata shapes: month names, years, DOI/ID tokens, "Accepted:".
_MONTH_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b",
    re.IGNORECASE,
)
_METADATA_RE = re.compile(
    r"\b(accepted|received|revised|published|doi|isbn|issn|page|pp\.|vol\.|"
    r"figure|fig\.|table\s+\d|supplementary|[A-Z]\.\s*[A-Z]\.)\b",
    re.IGNORECASE,
)

#: Statistics: CI, SD, SEM, n=, error bars.
_STATISTIC_RE = re.compile(
    r"(\b\d{2}\s*%\s*(?:confidence|ci)\b|\bconfidence interval\b|\bstandard deviation\b"
    r"|\bs\.?d\.?\b|\bs\.?e\.?m\.?\b|\bn\s*=\s*\d+|\bmean\s*±|\bSEM\b)",
    re.IGNORECASE,
)

#: Treatment/condition verbs marking a dose or time condition.
_CONDITION_RE = re.compile(
    r"\b(treated with|incubated with|incubated at|cultured in|cultured at|"
    r"grown in|grown at|diluted to|suspended in|supplemented with|"
    r"resuspended in|maintained in)\b",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(r"\bfor\s+\d+(\.\d+)?\s*(h|hr|hrs|hours|min|minutes|s|sec|days?|weeks?)\b", re.IGNORECASE)

#: Claim language (patent claims / "threshold" prose).
_CLAIM_RE = re.compile(
    r"\b(claim(s)?\s+\d|at least|at most|no more than|no less than|less than|"
    r"greater than|not more than|not less than)\b",
    re.IGNORECASE,
)

#: Result language.
_RESULT_RE = re.compile(
    r"\b(inhibit(ed|ion)?|reduced|increased|decreased|viability|activity|"
    r"killed|eradicated|effective|mic|ic50|ec50|kd|ki|km|potency)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RoleGuess:
    role: str
    reasons: list[str]


def classify_mention_role(sentence: str) -> RoleGuess:
    """Classify a sentence containing a number+unit mention.

    First match wins in the order metadata → statistic → condition → claim →
    result; anything else is ``undetermined`` (kept as a mention, never
    silently promoted).
    """

    if _METADATA_RE.search(sentence):
        return RoleGuess("metadata", ["metadata keyword (date/DOI/figure/table)"])

    if _STATISTIC_RE.search(sentence):
        return RoleGuess("statistic", ["statistical language (CI/SD/n=)"])

    if _CONDITION_RE.search(sentence) or (
        _DURATION_RE.search(sentence) and re.search(r"\b(treated|incubat|cultur|grown)\w*", sentence, re.IGNORECASE)
    ):
        return RoleGuess("condition", ["treatment/incubation condition language"])

    if _CLAIM_RE.search(sentence):
        return RoleGuess("claim", ["threshold/claim language"])

    if _RESULT_RE.search(sentence):
        return RoleGuess("result", ["result verb present"])

    return RoleGuess("undetermined", ["no role evidence"])


__all__ = ["RoleGuess", "classify_mention_role"]
