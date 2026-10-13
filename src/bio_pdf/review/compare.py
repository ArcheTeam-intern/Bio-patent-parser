"""Two-way sequence comparison.

Used later by the reparse stage (Task 14) to preserve differences between two
independent parse results. The comparison is per-residue and reports the
conflict positions; it never picks a "winner" by majority or by which string
looks "more protein-like".
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SequenceDiff:
    """The result of comparing two sequences."""

    positions: list[int] = field(default_factory=list)
    left: str = ""
    right: str = ""

    @property
    def conflicts(self) -> list[tuple[int, str, str]]:
        return [(i, self.left[i], self.right[i]) for i in self.positions]

    @property
    def identical(self) -> bool:
        return not self.positions and self.left == self.right


def compare_sequences(left: str, right: str) -> SequenceDiff:
    """Compare two sequences and report per-residue conflicts.

    Positions are 0-based. Length differences are reported as conflicts at
    the tail (with the missing side as an empty string).
    """

    if left == right:
        return SequenceDiff(positions=[], left=left, right=right)

    max_len = max(len(left), len(right))
    positions: list[int] = []
    for i in range(max_len):
        l = left[i] if i < len(left) else ""
        r = right[i] if i < len(right) else ""
        if l != r:
            positions.append(i)
    return SequenceDiff(positions=positions, left=left, right=right)


__all__ = ["compare_sequences", "SequenceDiff"]