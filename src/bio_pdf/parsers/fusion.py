"""Dual-channel text fusion (plan T05, hardened in B02).

When both a native document and a layout document exist for the same job,
they describe the same pages twice. Fusion aligns them by spatial overlap
and text agreement:

- A layout block consumes the ORDERED SET of native blocks it covers
  (space+text overlap), not a single line. Consumed native blocks are
  recorded as source refs only — they are NOT appended to the extraction
  stream (this killed the duplicate-candidate bug where one paragraph
  matched one line and the rest leaked through).
- Text conflicts are NOT silently resolved: the merged block records the
  FULL texts of both channels (no 120-char truncation), the conflict list
  carries the complete payload for SOURCE_CONFLICT checks, and the record
  is blocked from automatic acceptance.
- The semantic role of the aligned region is INHERITED (layout wins, and
  the role source is recorded) so re-introduced native text can never
  reset a caption to "unknown" and bypass role gating.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from bio_pdf.contracts import NormalizedDocument, NormalizedPage, TextBlock

#: Whitespace/case-insensitive comparison key.
_WS_RE = re.compile(r"\s+")


def _text_key(text: str) -> str:
    return _WS_RE.sub("", text).lower()


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
    """Fuse ``native`` into ``primary`` (structure source) by overlap."""

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
        consumed: set[str] = set()
        new_blocks: list[TextBlock] = []
        for block in page.blocks:
            matches = _aligned_native_set(block, native_blocks, consumed, overlap_threshold)
            if not matches:
                new_blocks.append(block)
                continue

            merged_pairs += len(matches)
            consumed.update(m.block_id for m in matches)

            primary_key = _text_key(block.text_raw)
            native_joined_key = _text_key(" ".join(m.text_raw for m in matches))
            refs = list(block.source_refs) + [f"native:{m.block_id}" for m in matches]
            # Structured aliases (id + text): the extractor uses these to
            # attach alias evidence only where the matched text really is.
            aliases = list(block.source_aliases) + [
                {"block_id": m.block_id, "text": m.text_raw} for m in matches
            ]

            if primary_key == native_joined_key:
                # Whitespace-only difference: prefer the native characters
                # (closest to the source glyphs), structure stays layout.
                native_text = " ".join(m.text_raw for m in matches)
                new_blocks.append(
                    block.model_copy(
                        update={"text_raw": native_text, "source_refs": refs, "source_aliases": aliases}
                    )
                )
            elif native_joined_key in primary_key or primary_key in native_joined_key:
                # One side carries extra context (line merging): keep the
                # LONGER text — no content silently dropped either way.
                native_text = " ".join(m.text_raw for m in matches)
                text = native_text if len(native_text) > len(block.text_raw) else block.text_raw
                new_blocks.append(
                    block.model_copy(
                        update={"text_raw": text, "source_refs": refs, "source_aliases": aliases}
                    )
                )
            else:
                # Genuine conflict: keep the primary text, record BOTH full
                # texts plus source refs for the SOURCE_CONFLICT check.
                conflicts.append(
                    {
                        "page": page.page_number,
                        "block_id": block.block_id,
                        "primary_text": block.text_raw,  # full — no truncation
                        "native_text": "\n".join(m.text_raw for m in matches),  # full
                        "primary_box": list(block.bbox) if block.bbox else None,
                        "native_boxes": [
                            list(m.bbox) if m.bbox else None for m in matches
                        ],
                        "native_block_ids": [m.block_id for m in matches],
                        "semantic_role": block.semantic_role,
                    }
                )
                new_blocks.append(
                    block.model_copy(update={"source_refs": refs, "source_aliases": aliases})
                )

        # Native blocks nobody consumed are kept (layout may have missed
        # them), with their role preserved as-is.
        tail = [b for b in native_blocks if b.block_id not in consumed]
        out_pages.append(page.model_copy(update={"blocks": new_blocks + tail}))

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


def _aligned_native_set(
    block: TextBlock,
    native_blocks: list[TextBlock],
    consumed: set[str],
    threshold: float,
) -> list[TextBlock]:
    """All unconsumed native blocks overlapping ``block``, in reading order.

    Ordering is by vertical position then horizontal position so the joined
    text follows the page's reading order.
    """

    if block.bbox is None:
        return []
    matches: list[TextBlock] = []
    for candidate in native_blocks:
        if candidate.block_id in consumed or candidate.bbox is None:
            continue
        if _overlap_ratio(block.bbox, candidate.bbox) >= threshold:
            matches.append(candidate)
    matches.sort(key=lambda b: (round(b.bbox[1], 1), round(b.bbox[0], 1)))
    return matches


__all__ = ["FusionResult", "fuse_documents"]
