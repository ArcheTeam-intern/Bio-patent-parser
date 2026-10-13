"""Assay record helpers.

An assay describes the *method* that produced a measurement (target, cell
line, conditions). The full :class:`AssayRecord` contract lands with the
relation work in Task 8; this module provides the condition parsing used by
the table/prose extractors now and the assay model later.
"""

from __future__ import annotations

from typing import Any

#: Recognised condition keys that may appear in a methods paragraph or a
#: table caption (temperature, time, pH, substrate, concentration, medium).
CONDITION_KEYS = (
    "temperature",
    "time",
    "ph",
    "substrate",
    "concentration",
    "medium",
    "buffer",
)


def parse_condition(text: str) -> dict[str, str] | None:
    """Parse a single ``key = value`` condition line.

    ``37°C`` and ``pH 7.4`` are examples. Unknown shapes return ``None``.
    """

    text = text.strip()
    if not text:
        return None

    lower = text.lower()
    for key in CONDITION_KEYS:
        if lower.startswith(key):
            value = text[len(key):].lstrip("=: ").strip()
            if value:
                return {"key": key, "value_raw": value}
    return None


__all__ = ["parse_condition", "CONDITION_KEYS"]