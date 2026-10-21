"""Residue alphabets and three-letter → one-letter mapping.

The mapping is intentionally lossy-*aware*: an unknown three-letter word is
never silently dropped or coerced to a guessed residue. The mapper keeps the
original token so downstream validation can flag ``unresolved`` entries
instead of pretending the length is correct.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Standard three-letter → one-letter amino-acid mapping. Includes the
#: documented ambiguous/non-standard codes B/Z/J/U/O per the design contract.
AMINO_ACID_THREE_TO_ONE: dict[str, str] = {
    "ALA": "A",
    "ARG": "R",
    "ASN": "N",
    "ASP": "D",
    "CYS": "C",
    "GLN": "Q",
    "GLU": "E",
    "GLY": "G",
    "HIS": "H",
    "ILE": "I",
    "LEU": "L",
    "LYS": "K",
    "MET": "M",
    "PHE": "F",
    "PRO": "P",
    "SER": "S",
    "THR": "T",
    "TRP": "W",
    "TYR": "Y",
    "VAL": "V",
    # Ambiguous / non-standard codes allowed when explicitly documented.
    "ASX": "B",
    "GLX": "Z",
    "XLE": "J",
    "XAA": "X",
    "SEC": "U",
    "PYL": "O",
    # Common terminal-modification spellings; kept only for display, never
    # normalised silently into the sequence.
    "FME": "M",
}

#: One-letter amino-acid alphabet (incl. documented non-standard letters).
AMINO_ACID_ONE_LETTERS: frozenset[str] = frozenset(
    "ACDEFGHIKLMNPQRSTVWYBZXJUO"
)

#: Three-letter code with a GLUED residue position (``Glu180``). Residue
#: TABLES print the position inside the residue cell, so the token must be
#: recognised as ``GLU`` + position 180 — never as an unknown six-letter word.
_POSITION_SUFFIXED_CODE_RE = re.compile(r"^([A-Za-z]{3})\d+$")

#: Single letters that occur in nucleic-acid residue runs.
NUCLEIC_ONE_LETTERS: frozenset[str] = frozenset("ACGTUNRYSWKMBDHV")


def strip_position_suffix(token: str) -> str:
    """Drop a trailing residue-position number from a three-letter code.

    ``"Glu180"`` → ``"Glu"``; anything else is returned unchanged so callers
    can use it as a pure normaliser.
    """

    match = _POSITION_SUFFIXED_CODE_RE.match(token.strip())
    return match.group(1) if match else token


def looks_like_residue_token(text: str) -> bool:
    """Whether a cell/token holds residues rather than prose or a value.

    Accepts a three-letter code (optionally position-suffixed) or a run of
    nucleic-acid letters long enough not to be an ordinary word. Used to tell
    a residue LISTING table apart from activity/condition matrices.
    """

    stripped = text.strip()
    if not stripped:
        return False
    base = strip_position_suffix(stripped).upper()
    if base in AMINO_ACID_THREE_TO_ONE:
        return True
    if len(stripped) >= 8 and stripped.isalpha():
        return all(ch in NUCLEIC_ONE_LETTERS for ch in stripped.upper())
    return False


#: IUPAC nucleic-acid ambiguity codes.
NUCLEIC_AMBIGUITY: dict[str, str] = {
    "N": "ACGT",
    "R": "AG",
    "Y": "CT",
    "S": "GC",
    "W": "AT",
    "K": "GT",
    "M": "AC",
    "B": "CGT",
    "D": "AGT",
    "H": "ACT",
    "V": "ACG",
}

#: Allowed single-letter alphabets keyed by molecule type.
ALLOWED_ALPHABETS: dict[str, frozenset[str]] = {
    "protein": AMINO_ACID_ONE_LETTERS,
    "DNA": frozenset("ACGTNRYSWKMBDHV"),
    "RNA": frozenset("ACGUNRYSWKMBDHV"),
    "unknown": frozenset(),
}


@dataclass(frozen=True)
class MappedResidue:
    """One token → residue mapping, keeping the original for audit."""

    token: str
    one_letter: str | None
    resolved: bool


class ResidueMapper:
    """Convert three-letter tokens to one-letter residues with provenance."""

    def map_token(self, token: str) -> MappedResidue:
        """Map a single three-letter token (case-insensitive)."""

        upper = token.strip().upper()
        if not upper:
            return MappedResidue(token=token, one_letter=None, resolved=False)
        one = AMINO_ACID_THREE_TO_ONE.get(upper)
        if one is None:
            # Residue tables glue the position onto the code (``Glu180``).
            one = AMINO_ACID_THREE_TO_ONE.get(strip_position_suffix(upper).upper())
        if one is not None:
            return MappedResidue(token=token, one_letter=one, resolved=True)
        # Unknown three-letter token: keep unresolved, do not guess.
        return MappedResidue(token=token, one_letter=None, resolved=False)

    def map_three_letter_text(self, text: str) -> tuple[str, list[MappedResidue]]:
        """Map a whitespace-separated three-letter sequence.

        Returns ``(one_letter_sequence, mapped_tokens)``. Unresolved tokens
        contribute ``?`` to the sequence so length is preserved, and the
        token list keeps the original for audit.
        """

        tokens = text.split()
        mapped: list[MappedResidue] = []
        out: list[str] = []
        for token in tokens:
            result = self.map_token(token)
            mapped.append(result)
            out.append(result.one_letter if result.resolved else "?")
        return "".join(out), mapped

    @staticmethod
    def normalize_nucleic_text(text: str) -> str:
        """Normalise a nucleic-acid sequence: strip whitespace, uppercase.

        Does NOT convert T<->U; that is a downstream, evidence-driven step.
        """

        return "".join(ch for ch in text if not ch.isspace()).upper()


__all__ = [
    "AMINO_ACID_THREE_TO_ONE",
    "AMINO_ACID_ONE_LETTERS",
    "NUCLEIC_AMBIGUITY",
    "NUCLEIC_ONE_LETTERS",
    "ALLOWED_ALPHABETS",
    "MappedResidue",
    "ResidueMapper",
    "looks_like_residue_token",
    "strip_position_suffix",
]
