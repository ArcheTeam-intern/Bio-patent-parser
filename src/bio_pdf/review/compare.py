"""Two-way sequence comparison with real alignment (plan B11).

Comparing ``"AG"`` vs ``"AGL"`` must show an INSERTION at position 2 — not
a whole-tail replacement, and definitely not an IndexError from naive
per-index access (the review's crash). The aligner is a standard global
alignment (Needleman-Wunsch, unit costs); both full sequences are always
preserved on the diff object.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SequenceDiff:
    """The result of comparing two sequences (aligned)."""

    left: str = ""
    right: str = ""
    #: Aligned forms (gaps as "-"); same length by construction.
    aligned_left: str = ""
    aligned_right: str = ""
    #: Conflict positions in ALIGNED coordinates with both aligned chars.
    conflicts: list[tuple[int, str, str]] = field(default_factory=list)
    edit_distance: int = 0

    @property
    def identical(self) -> bool:
        return self.edit_distance == 0

    def describe(self, *, context: int = 8, limit: int = 5) -> list[dict[str, str]]:
        """Human-readable conflict windows around the first conflicts."""

        windows: list[dict[str, str]] = []
        for position, left_char, right_char in self.conflicts[:limit]:
            start = max(0, position - context)
            end = min(len(self.aligned_left), position + context + 1)
            windows.append(
                {
                    "position": str(position),
                    "left_window": self.aligned_left[start:end],
                    "right_window": self.aligned_right[start:end],
                    "left_char": left_char or "-",
                    "right_char": right_char or "-",
                }
            )
        return windows


def compare_sequences(left: str, right: str) -> SequenceDiff:
    """Align two sequences and report per-aligned-position conflicts.

    Both sequences are compared in FULL — no truncation; gaps render as
    ``-`` in the aligned forms and as ``''`` in conflict tuples.
    """

    if left == right:
        return SequenceDiff(
            left=left,
            right=right,
            aligned_left=left,
            aligned_right=right,
            conflicts=[],
            edit_distance=0,
        )

    # Global alignment (unit-cost Needleman-Wunsch). Sequences here are
    # residue strings (bytes-scale), so O(n*m) is fine; guard pathological
    # lengths with a tail alignment fallback.
    n, m = len(left), len(right)
    if n * m > 4_000_000:
        return _tail_alignment(left, right)

    previous = list(range(m + 1))
    pointers: list[list[int]] = []
    for i in range(1, n + 1):
        current = [i] + [0] * m
        row = [1] * (m + 1)  # 1=up, 2=diag, 3=left
        for j in range(1, m + 1):
            diag = previous[j - 1] + (0 if left[i - 1] == right[j - 1] else 1)
            up = previous[j] + 1
            lft = current[j - 1] + 1
            best = min(diag, up, lft)
            current[j] = best
            row[j] = 2 if best == diag else (1 if best == up else 3)
        pointers.append(row)
        previous = current

    # Traceback.
    aligned_left: list[str] = []
    aligned_right: list[str] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and pointers[i - 1][j] == 2:
            aligned_left.append(left[i - 1])
            aligned_right.append(right[j - 1])
            i -= 1
            j -= 1
        elif i > 0 and (j == 0 or pointers[i - 1][j] == 1):
            aligned_left.append(left[i - 1])
            aligned_right.append("-")
            i -= 1
        else:
            aligned_left.append("-")
            aligned_right.append(right[j - 1])
            j -= 1
    aligned_left.reverse()
    aligned_right.reverse()

    conflicts = [
        (idx, l if l != "-" else "", r if r != "-" else "")
        for idx, (l, r) in enumerate(zip(aligned_left, aligned_right))
        if l != r
    ]
    return SequenceDiff(
        left=left,
        right=right,
        aligned_left="".join(aligned_left),
        aligned_right="".join(aligned_right),
        conflicts=conflicts,
        edit_distance=previous[m],
    )


def _tail_alignment(left: str, right: str) -> SequenceDiff:
    """Fallback for very long sequences: positional prefix + tail diff."""

    prefix = min(len(left), len(right))
    conflicts = [
        (i, left[i] if i < len(left) else "", right[i] if i < len(right) else "")
        for i in range(max(len(left), len(right)))
        if (left[i] if i < len(left) else "") != (right[i] if i < len(right) else "")
    ]
    aligned_left = left.ljust(max(len(left), len(right)), "-")
    aligned_right = right.ljust(max(len(left), len(right)), "-")
    return SequenceDiff(
        left=left,
        right=right,
        aligned_left=aligned_left,
        aligned_right=aligned_right,
        conflicts=conflicts,
        edit_distance=len(conflicts),
    )


__all__ = ["compare_sequences", "SequenceDiff"]
