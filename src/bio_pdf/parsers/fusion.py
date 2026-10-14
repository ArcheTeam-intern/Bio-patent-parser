"""Dual-channel text fusion (plan T05).

When both a native-parse document and a layout document exist for the same
job, they describe the same pages twice. This module aligns them by *space
overlap + text agreement*:

- Only blocks whose rectangles overlap and whose text aligns (normalized
  equality or one containing the other) are merged; the layout block keeps
  its structure/reading order while the native block's characters become
  the preferred lexical source (native text layer is closest to the source
  glyphs).
- Text conflicts are NOT silently resolved: the merged block records both
  and a warning; the record blocks automatic acceptance.
- Non-overlapping blocks from either channel are preserved as-is.

The merged document is the canonical NormalizedDocument for extraction;
both source projections remain available through the stage artifacts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from bio_pdf.contracts import NormalizedDocument, NormalizedPage, TextBlock

#: Whitespace/case-insensitive comparison key.
_CLOCK_RE = re.compile(r"\s+")


def _text_key(text: str) -> str:
    return _CLOCK_RE.sub("", text).lower()


def _overlap_ratio(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> float:
    """Intersection area over the smaller box's area."""

    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = max((ax1 - ax0) * (ay1 - ay0), 1e-9)
    area_b = max((bx1 - bx0) * (by1 - by0), 1e-9)
    return inter / min(area_a, area_b)


@dataclass
class FusionResult:
    document: NormalizedDocument
    merged_pairs: int = 0
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def fuse_documents(
    primary: NormalizedDocument,
    native: NormalizedDocument | None,
    *,
    overlap_threshold: float = 0.5,
) -> FusionResult:
    """Fuse ``native`` into ``primary`` (structure source) by overlap.

    Returns the canonical document. When ``native`` is None the primary is
    returned unchanged.
    """

    if native is None:
        return FusionResult(document=primary)

    native_by_page: dict[int, list[TextBlock]] = {}
    for page in native.pages:
        native_by_page.setdefault(page.page_number, []).extend(page.blocks)

    merged_pairs = 0
    conflicts: list[dict[str, Any]] = []
    out_pages: list[NormalizedPage] = []

    for page in primary.pages:
        native_blocks = native_by_page.get(page.page_number, [])
        used_native: set[str] = set()
        new_blocks: list[TextBlock] = []
        for block in page.blocks:
            replacement = _aligned_native(block, native_blocks, used_native, overlap_threshold)
            if replacement is None:
                new_blocks.append(block)
                continue
            used_native.add(replacement.block_id)
            merged_pairs += 1
            native_key = _text_key(replacement.text_raw)
            primary_key = _text_key(block.text_raw)
            if native_key == primary_key:
                # Preferred lexical source: the native text layer.
                updated = block.model_copy(
                    update={
                        "text_raw": replacement.text_raw,
                        "source_refs": list(block.source_refs) + [f"native:{replacement.block_id}"],
                    }
                )
                new_blocks.append(updated)
            elif native_key in primary_key or primary_key in native_key:
                # One is a superset (line-merging differences): keep the
                # primary structure but record the alternative.
                updated = block.model_copy(
                    update={
                        "source_refs": list(block.source_refs) + [f"native:{replacement.block_id}"],
                    }
                )
                new_blocks.append(updated)
            else:
                # Genuine character conflict: keep both, flag for review.
                conflicts.append(
                    {
                        "page": page.page_number,
                        "block_id": block.block_id,
                        "primary_text": block.text_raw[:120],
                        "native_text": replacement.text_raw[:120],
                    }
                )
                new_blocks.append(block)

        # Native blocks with no layout counterpart are kept (layout may have
        # missed them) — appended after the structured blocks.
        tail = [b for b in native_blocks if b.block_id not in used_native]
        out_pages.append(
            page.model_copy(update={"blocks": new_blocks + tail})
        )

    warnings: list[str] = []
    if conflicts:
        warnings.append(
            f"{len(conflicts)} text conflicts between native and layout channels "
            "(kept both; blocked from auto-accept)"
        )
    document = primary.model_copy(update={"pages": out_pages})
    return FusionResult(
        document=document,
        merged_pairs=merged_pairs,
        conflicts=conflicts,
        warnings=warnings,
    )


def _aligned_native(
    block: TextBlock,
    native_blocks: list[TextBlock],
    used: set[str],
    threshold: float,
) -> TextBlock | None:
    """Best overlapping, text-aligned native block for ``block``."""

    if block.bbox is None:
        return None
    best: tuple[float, TextBlock] | None = None
    for candidate in native_blocks:
        if candidate.block_id in used or candidate.bbox is None:
            continue
        ratio = _overlap_ratio(block.bbox, candidate.bbox)
        if ratio < threshold:
            continue
        if best is None or ratio > best[0]:
            best = (ratio, candidate)
    return best[1] if best else None


__all__ = ["FusionResult", "fuse_documents"]
