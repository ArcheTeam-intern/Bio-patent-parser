"""Residue alphabets and three-letter → one-letter mapping.

The mapping is intentionally lossy-*aware*: an unknown three-letter word is
never silently dropped or coerced to a guessed residue. The mapper keeps the
original token so downstream validation can flag ``unresolved`` entries
instead of pretending the length is correct.
"""

from __future__ import annotations

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
    "ALLOWED_ALPHABETS",
    "MappedResidue",
    "ResidueMapper",
]