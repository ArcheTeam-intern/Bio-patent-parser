"""Evaluation metrics.

``safe_ratio`` is the reporting helper for precision/recall; a zero
denominator returns ``None`` (unknown) rather than a misleading 100%.
Character error rate uses Levenshtein edit distance and is reported
separately from exact-match accuracy.
"""

from __future__ import annotations


def safe_ratio(numerator: int, denominator: int) -> float | None:
    """Return ``numerator / denominator``, or ``None`` when undefined.

    A ``0/0`` ratio is *unknown*, not perfect — the benchmark must not report
    100% accuracy when there was nothing to measure.
    """

    if denominator == 0:
        return None
    return numerator / denominator


def _levenshtein(left: str, right: str) -> int:
    """Compute the Levenshtein edit distance between two strings."""

    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)

    previous = list(range(len(right) + 1))
    for i, lch in enumerate(left, start=1):
        current = [i]
        for j, rch in enumerate(right, start=1):
            insert = current[j - 1] + 1
            delete = previous[j] + 1
            substitute = previous[j - 1] + (0 if lch == rch else 1)
            current.append(min(insert, delete, substitute))
        previous = current
    return previous[-1]


def character_error_rate(reference: str, candidate: str) -> float:
    """Return the edit distance divided by the reference length.

    Returns 1.0 when the reference is empty (no correct characters).
    """

    if not reference:
        return 1.0
    return _levenshtein(reference, candidate) / len(reference)


__all__ = ["safe_ratio", "character_error_rate"]