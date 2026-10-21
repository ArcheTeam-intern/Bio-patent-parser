"""Activity value and unit parsing.

Values are parsed as *strings*, never coerced through ``float()``. The
tokenizer checks categorical labels first, then comparators, ranges,
plus-minus and scientific notation; anything unrecognised is kept as
``unparsed`` with its raw text.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from bio_pdf.contracts import ActivityValue

#: Categorical labels (case-insensitive). These are *missing/inactive*
#: markers, not numeric values.
_CATEGORICAL = {
    "NA",
    "ND",
    "N.D.",
    "N/A",
    "NOT TESTED",
    "NOT DETERMINED",
    "INACTIVE",
    "NO INHIBITION",
    "N/T",
    "NT",
    # A lone dash is the universal "not tested" placeholder in patent
    # tables (live Table 4c prints "-" for every peptide/strain pair it did
    # not run). It reached the extractor as a value but was not recognised
    # here, so it stayed ``unparsed`` and turned into a validation conflict
    # (35 review items that said "value could not be parsed: '-'" people had
    # to dismiss).
    "-",
    "–",
    "—",
    "−",
    "未测",
    "未检出",
    "无活性",
}

#: Numeric literal, including scientific notation. Kept as a string.
_NUM = r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"

_COMPARATOR_RE = re.compile(rf"^(<=|>=|<|>|=|~|≤|≥)\s*({_NUM})$")
_RANGE_RE = re.compile(rf"^({_NUM})\s*(?:-|–|—|~|to)\s*({_NUM})$", re.IGNORECASE)
_PLUS_MINUS_RE = re.compile(rf"^({_NUM})\s*±\s*({_NUM})$")
_SCALAR_RE = re.compile(rf"^({_NUM})$")

#: Unicode comparators normalised to the ASCII forms in the contract enum.
_COMPARATOR_NORMALIZE = {"≤": "<=", "≥": ">="}


def _is_valid_number(text: str) -> bool:
    try:
        Decimal(text)
        return True
    except InvalidOperation:
        return False


def parse_activity_value(raw: str) -> ActivityValue:
    """Parse a raw activity value string into an :class:`ActivityValue`.

    Order matters: categorical first (so ``ND`` is not parsed as a number),
    then plus-minus, comparator, range, then bare scalar.
    """

    text = raw.strip()
    upper = text.upper()

    if upper in _CATEGORICAL:
        return ActivityValue(kind="categorical", qualifier=text, raw=raw)

    if not text:
        return ActivityValue(kind="unparsed", raw=raw)

    if (m := _PLUS_MINUS_RE.match(text)):
        if _is_valid_number(m.group(1)) and _is_valid_number(m.group(2)):
            return ActivityValue(
                kind="plus_minus",
                number=m.group(1),
                uncertainty=m.group(2),
                raw=raw,
            )

    if (m := _COMPARATOR_RE.match(text)):
        if _is_valid_number(m.group(2)):
            operator = _COMPARATOR_NORMALIZE.get(m.group(1), m.group(1))
            return ActivityValue(
                kind="scalar",
                operator=operator,
                number=m.group(2),
                raw=raw,
            )

    if (m := _RANGE_RE.match(text)):
        if _is_valid_number(m.group(1)) and _is_valid_number(m.group(2)):
            # A self-contradictory range (lower > upper, e.g. OCR noise
            # "10-8" from a date or page reference) is not a real interval.
            # Keep the raw text honestly as unparsed instead of raising —
            # the ActivityValue contract rejects lower > upper and a hard
            # error would kill the whole extract stage.
            if Decimal(m.group(1)) <= Decimal(m.group(2)):
                return ActivityValue(
                    kind="range",
                    lower=m.group(1),
                    upper=m.group(2),
                    raw=raw,
                )
            return ActivityValue(kind="unparsed", raw=raw)

    if (m := _SCALAR_RE.match(text)):
        if _is_valid_number(m.group(1)):
            return ActivityValue(kind="scalar", number=m.group(1), raw=raw)

    return ActivityValue(kind="unparsed", raw=raw)


#: Known concentration/activity units (normalised spelling). Used to decide
#: whether :func:`normalize_unit` can report a recognised unit.
_KNOWN_UNITS = {
    "ug/mL",
    "mg/mL",
    "ng/mL",
    "g/mL",
    "mg/L",
    "ug/L",
    "ng/L",
    "g/L",
    "uM",
    "nM",
    "pM",
    "mM",
    "M",
    "ug",
    "mg",
    "ng",
    "g",
    "U/mL",
    "IU/mL",
    "%",
}


def normalize_unit(raw: str | None) -> str | None:
    """Normalise an equivalent unit spelling; return ``None`` if unknown.

    The only transformation performed here is micro-sign unification
    (``µ``/``μ`` → ``u``). Cross-unit conversions (e.g. nM → uM) are *not*
    performed — those need an explicit conversion factor and are handled
    elsewhere. An unrecognised unit returns ``None`` so the caller can keep
    the raw text and report it.
    """

    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    normalized = text.replace("µ", "u").replace("μ", "u")
    # Normalise the "/l" vs "/L" tail so "ug/ml" → "ug/mL".
    normalized = re.sub(r"/(ml)$", "/mL", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"/(l)$", "/L", normalized, flags=re.IGNORECASE)

    if normalized in _KNOWN_UNITS:
        return normalized
    return None


__all__ = ["parse_activity_value", "normalize_unit"]