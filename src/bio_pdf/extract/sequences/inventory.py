"""Sequence inventory (C02): one trackable row per declared SEQ ID.

The inventory is built from the ST.25 declaration scan — every ``<210>`` block
leaves a row, whether or not residues were recovered — so counts can never be
mistaken for residue completeness. Rows carry the fixed status vocabulary:

================  ============================================================
status            meaning
================  ============================================================
``declared``      declaration found; residue region not reached in this run
``missing``       residue region was reached (``<400>`` seen) but no
                  recoverable residues were produced (e.g. image-only region)
``partial``       residues recovered but incomplete: shorter than declared,
                  unresolved tokens, or truncated by the requested page window
``complete``      recovered and length/alphabet checks passed
``conflict``      duplicate declaration of the same SEQ ID with mismatched
                  metadata, or observed residues exceed the declared length
``outside_scope`` declaration page falls outside the requested page set
================  ============================================================

Honesty rules:

- The summary never presents a partial-page-window inventory as full coverage:
  ``scope`` carries requested/parsed page ranges and the document page count.
- ``<160>`` (total sequence count) is used only to report how many declared
  IDs the *document* claims; rows are never fabricated for IDs that were not
  observed. The unobserved remainder is reported as an aggregate
  (``declared_total_unseen``), which is scope information, not residue status.
"""

from __future__ import annotations

from typing import Any

#: Status vocabulary — stable codes for API/UI/report consumers.
SEQ_INVENTORY_STATUSES = (
    "declared",
    "missing",
    "partial",
    "complete",
    "conflict",
    "outside_scope",
)


def _entry_key(entry: dict[str, Any]) -> str:
    seq_id = entry.get("seq_id_no")
    return str(seq_id) if seq_id else f"@{entry.get('declaration_page')}"


def build_sequence_inventory(
    scan_entries: list[dict[str, Any]],
    *,
    requested_pages: list[int] | tuple[int, ...] = (),
    document_page_count: int | None = None,
    declared_total: int | None = None,
) -> dict[str, Any]:
    """Turn the ST.25 declaration scan into the inventory payload.

    Args:
        scan_entries: Entries from :func:`parse_st25_blocks_with_scan`.
        requested_pages: The job's requested page set (empty = all pages).
        document_page_count: Full document page count when known.
        declared_total: ``<160>`` value when present (the listing's own claim
            about how many sequences the document contains).

    Returns:
        ``{"entries": [...], "summary": {...}}`` — JSON-ready.
    """

    scope_pages = {int(p) for p in requested_pages}

    # Duplicate detection: the same SEQ ID declared more than once is only
    # clean when the declarations agree; two rows must never silently merge.
    by_id: dict[str, list[dict[str, Any]]] = {}
    for entry in scan_entries:
        by_id.setdefault(_entry_key(entry), []).append(entry)

    conflict_ids: dict[str, str] = {}
    for key, entries in by_id.items():
        if len(entries) > 1:
            conflict_ids[key] = (
                f"declared {len(entries)} times "
                f"(pages {', '.join(str(e['declaration_page']) for e in entries)})"
            )

    rows: list[dict[str, Any]] = []
    for entry in scan_entries:
        key = _entry_key(entry)
        page = int(entry.get("declaration_page") or 1)
        declared = entry.get("declared_length")
        observed = entry.get("observed_length")
        reasons: list[str] = []
        status: str

        if scope_pages and page not in scope_pages:
            status = "outside_scope"
            reasons.append(f"declaration page {page} outside requested pages")
        elif key in conflict_ids:
            status = "conflict"
            reasons.append(conflict_ids[key])
        elif observed is None:
            status = "missing" if entry.get("saw_400") else "declared"
            if status == "missing":
                reasons.append(
                    "residue region reached but no recoverable residues "
                    "(text layer absent — OCR candidate)"
                )
            else:
                reasons.append("residue region not reached in this run")
        elif declared is not None and observed > declared:
            status = "conflict"
            reasons.append(f"observed {observed} exceeds declared {declared}")
        elif not entry.get("is_complete"):
            status = "partial"
            if declared is not None and observed < declared:
                reasons.append(f"recovered {observed} of declared {declared}")
            if entry.get("has_unresolved"):
                reasons.append("unresolved residue tokens present")
            if not reasons:
                reasons.append("declared length unknown; completeness unverified")
        else:
            status = "complete"

        rows.append(
            {
                "seq_id_no": entry.get("seq_id_no"),
                "declaration_page": page,
                "declared_length": declared,
                "molecule_type": entry.get("molecule_type") or "unknown",
                "organism_raw": entry.get("organism_raw"),
                "saw_residue_region": bool(entry.get("saw_400")),
                "residue_pages": list(entry.get("residue_pages") or []),
                "observed_length": observed,
                "has_unresolved": bool(entry.get("has_unresolved")),
                "recovery_source": "st25" if observed is not None else None,
                "record_id": entry.get("record_id"),
                "status": status,
                "reasons": reasons,
            }
        )

    counts = {s: 0 for s in SEQ_INVENTORY_STATUSES}
    for row in rows:
        counts[row["status"]] += 1

    located = counts["complete"] + counts["partial"] + counts["conflict"]
    found_pages = sorted(
        {row["declaration_page"] for row in rows}
        | {p for row in rows for p in row["residue_pages"]}
    )
    declared_total_unseen: int | None = None
    if declared_total is not None and declared_total >= len(rows):
        declared_total_unseen = declared_total - len(rows)

    summary = {
        "declared_total": declared_total,
        "declared_total_unseen": declared_total_unseen,
        "observed_declarations": len(rows),
        "located": located,
        "complete": counts["complete"],
        "partial": counts["partial"],
        "conflict": counts["conflict"],
        "missing": counts["missing"],
        "declared_only": counts["declared"],
        "outside_scope": counts["outside_scope"],
        "scope": {
            "requested_pages": sorted(scope_pages) if scope_pages else None,
            "document_page_count": document_page_count,
            "observed_pages": found_pages,
            "covers_full_document": (
                bool(scope_pages)
                and document_page_count is not None
                and len(scope_pages) >= document_page_count
            )
            if scope_pages
            else True,
        },
    }
    return {"entries": rows, "summary": summary}


def extract_declared_total(blocks: list[dict[str, Any]]) -> int | None:
    """The ``<160>`` total-sequence-count claim, when the listing states it."""

    import re

    pattern = re.compile(r"<\s*160\s*>\s*(\d{1,4})")
    for block in blocks:
        text = str(block.get("text") or "")
        match = pattern.search(text)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                continue
    return None


__all__ = [
    "SEQ_INVENTORY_STATUSES",
    "build_sequence_inventory",
    "extract_declared_total",
]
