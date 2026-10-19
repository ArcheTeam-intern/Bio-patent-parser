"""K05: pure declaration merge logic.

Responsibility: take a single native block + a GROUP of OCR lines that
share the same mention (same x/y overlap), and produce:

- ONE canonical declaration text (with merged tag→value map)
- ONE provenance record (fields, conflict flags)
- The verdict that drives the surrounding pipeline's record-keeping

This module does NOT touch the extraction feed or the ST.25 parser
itself. ``handlers._merge_ocr_page_lines`` calls into here once per
native-mention group so multiple OCR lines that share a mention are
collated into a SINGLE declaration line instead of producing N
``<210>`` duplicates that confuse the parser.

The pure-function shape keeps the logic testable from a unit test
without standing up the workflow handlers; the handler is responsible
for bounding the group (paragraphs / page / adjacent ordering), not
this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


_TAG_CAPTURE = re.compile(r"<\s*(\d{3})\s*>\s*([^<]*)")


def parse_declarations(text: str) -> list[dict[str, str]] | None:
    """Per-DECLARATION tag maps (a new map starts at <210>).

    A multi-sequence line is never flattened into one tag→value dict
    (later tags would overwrite earlier ones); each <210> opens its
    own map, and tags before the first <210> form a leading anonymous
    declaration. ``None`` = the text carries no tag.
    """

    matches = _TAG_CAPTURE.findall(text)
    if not matches:
        return None
    declarations: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for tag, value in matches:
        if tag == "210":
            current = {"210": value.strip()}
            declarations.append(current)
        else:
            if current is None:
                current = {}
                declarations.append(current)
            current[tag] = value.strip()
    return declarations


def _per_declaration_verdict(
    native_fields: list[dict[str, str]],
    ocr_fields: list[dict[str, str]],
) -> str:
    """Field-by-field verdict for ONE declaration (matched by index).

    Returns 'equal' | 'native_superset' | 'ocr_superset' | 'addendum' |
    'conflict' | 'incomparable'.
    """

    if not native_fields or not ocr_fields:
        return "incomparable"
    if len(native_fields) != len(ocr_fields):
        return "conflict"
    n_set = set(native_fields[0].keys())
    o_set = set(ocr_fields[0].keys())
    if any(native_fields[0][t] != ocr_fields[0][t] for t in n_set & o_set):
        return "conflict"
    if n_set == o_set:
        return "equal"
    if n_set >= o_set:
        return "native_superset"
    if o_set >= n_set:
        return "ocr_superset"
    return "addendum"


@dataclass
class FieldProvenance:
    tag: str
    value: str
    origin: str  # "native" or "ocr"
    block_id: str  # empty when origin == "ocr" and the OCR line is anonymous


@dataclass
class DeclarationGroup:
    """The merged result for ONE native mention + its OCR lines."""

    native_text: str
    native_block_id: str
    ocr_texts: list[str] = field(default_factory=list)
    merged_text: str = ""
    fields: list[FieldProvenance] = field(default_factory=list)
    verdict: str = "incomparable"  # overall verdict
    conflict: bool = False
    #: OCR lines that disagree with the native (or with a previous OCR
    #: line) on a shared tag — one entry per offending OCR line. The
    #: caller emits a content_conflict record per entry.
    conflicting_ocr: list[str] = field(default_factory=list)

    def to_record(self, *, page_number: int, bbox: tuple[float, float, float, float]) -> dict[str, Any]:
        return {
            "kind": "declaration_merge",
            "page_number": page_number,
            "x0": float(round(bbox[0], 1)),
            "y_top": float(round(bbox[1], 1)),
            "x1": float(round(bbox[2], 1)),
            "y_bottom": float(round(bbox[3], 1)),
            "native_text": self.native_text,
            "ocr_text": list(self.ocr_texts),
            "merged_text": self.merged_text,
            "fields": [
                {"kind": "declaration_field", "field": f.tag, "value": f.value,
                 "origin": f.origin, "block_id": f.block_id}
                for f in self.fields
            ],
            "verdict": self.verdict,
            "conflict": self.conflict,
            "reason": (
                "K05: one logical declaration from one native mention + N OCR lines; "
                "field provenance recorded per source channel"
            ),
        }


def merge_group(
    native_text: str,
    native_block_id: str,
    ocr_texts: list[str],
) -> DeclarationGroup:
    """Collate ONE native mention + its N OCR lines into one declaration.

    Steps:
    1. Parse the native text into per-declaration tag maps.
    2. Concatenate all OCR lines (in arrival order) and parse their maps.
    3. Match declarations 1:1 by index. For each pair, compute the
       per-declaration verdict; the OVERALL verdict is:
       - ``conflict`` if any pair conflicts or the count mismatches;
       - ``equal`` if every pair is equal / native_superset / ocr_superset
         with no missing fields;
       - ``addendum`` if at least one pair is an addendum AND no
         conflicts exist;
       - ``incomparable`` if the native has no tags (pure prose).
    4. The MERGED text is built from the union of fields for each pair
       (n_set ∪ o_set), preferring the OCR value when both sides
       supply the same tag (the OCR ran on the same line and is the
       more recent read; conflict tags are kept as-is so the audit can
       see both).
    5. Provenance records: ``origin == "native"`` only when the native
       supplied the value; OCR-only fields carry an empty
       ``block_id`` (an OCR line carries no block identity).
    """

    group = DeclarationGroup(native_text=native_text, native_block_id=native_block_id,
                              ocr_texts=list(ocr_texts))
    native_fields_all = parse_declarations(native_text) or []
    # Concatenate OCR text into a single logical stream — each OCR line
    # may carry its own declarations, all of which belong to this group.
    ocr_concat = " ".join(t for t in ocr_texts if t)
    ocr_fields_all = parse_declarations(ocr_concat) or []
    if not native_fields_all:
        group.verdict = "incomparable"
        return group

    pair_count = max(len(native_fields_all), len(ocr_fields_all))
    per_pair_verdicts: list[str] = []
    per_pair_unions: list[dict[str, tuple[str, str]]] = []  # tag -> (value, origin)
    overall_conflict = False
    seen_210: dict[str, dict[str, str]] = {}  # <210> value -> merged fields for cross-pair detection
    for pair_index in range(pair_count):
        n_map = native_fields_all[pair_index] if pair_index < len(native_fields_all) else {}
        # For OCR we re-parse the i-th declaration so we know which
        # text each tag came from (used for provenance block_id).
        o_maps: list[dict[str, str]] = []
        # Greedy walk: collect the i-th declaration from each OCR line.
        cumulative = 0
        target = pair_index
        source_ocr_line: str | None = None
        for line in ocr_texts:
            line_maps = parse_declarations(line) or []
            if target < cumulative + len(line_maps):
                o_maps = [line_maps[target - cumulative]]
                source_ocr_line = line
                break
            cumulative += len(line_maps)
        o_map = o_maps[0] if o_maps else {}
        # K05: cross-pair conflict — when the OCR has more
        # declarations than the native and a later OCR <210> re-uses a
        # value we already saw in an earlier pair, the OCR lines
        # disagree on the same sequence. Tag the later pair as a
        # conflict on the shared <210> so the audit never silently
        # picks one and drops the other.
        seq_no = (o_map.get("210") or n_map.get("210") or "").strip()
        this_pair_conflicts = False
        if seq_no and seq_no in seen_210:
            prior = seen_210[seq_no]
            cur = {k: v for k, v in o_map.items() if k != "210"}
            for tag, value in cur.items():
                if tag in prior and prior[tag] != value:
                    this_pair_conflicts = True
                    overall_conflict = True
            for tag, value in cur.items():
                seen_210[seq_no] = {**prior, tag: value}
        else:
            if seq_no:
                seen_210[seq_no] = {
                    k: v for k, v in o_map.items() if k != "210"
                }
        verdict = _per_declaration_verdict([n_map], [o_map])
        per_pair_verdicts.append(verdict)
        if verdict == "conflict":
            overall_conflict = True
            this_pair_conflicts = True
        if this_pair_conflicts and source_ocr_line is not None:
            group.conflicting_ocr.append(source_ocr_line)
        union: dict[str, tuple[str, str]] = {}
        for tag, val in n_map.items():
            union[tag] = (val, "native")
        for tag, val in o_map.items():
            if tag in union:
                # Field is shared by both channels: native owns it
                # (K06 contract — the native artifact is authoritative;
                # OCR is treated as confirmation, not as a fresh
                # source, when values agree). When values disagree
                # ``this_pair_conflicts`` was already set above so the
                # audit trail records the disagreement; the native
                # value stays in the union.
                continue
            union[tag] = (val, "ocr")
        per_pair_unions.append(union)

    # Overall verdict
    if overall_conflict:
        group.verdict = "conflict"
        group.conflict = True
    elif all(v == "equal" for v in per_pair_verdicts):
        group.verdict = "equal"
    elif any(v == "addendum" for v in per_pair_verdicts):
        group.verdict = "addendum"
    elif any(v == "ocr_superset" for v in per_pair_verdicts) and all(
        v in ("equal", "ocr_superset") for v in per_pair_verdicts
    ):
        group.verdict = "ocr_superset"
    elif any(v == "native_superset" for v in per_pair_verdicts) and all(
        v in ("equal", "native_superset") for v in per_pair_verdicts
    ):
        group.verdict = "native_superset"
    else:
        # Mixed verdicts without explicit conflict — keep the most
        # informative label for the audit trail.
        group.verdict = "addendum"

    # Build merged_text from the union of fields per pair.
    parts: list[str] = []
    for pair_index, union in enumerate(per_pair_unions):
        for tag, (value, _origin) in union.items():
            parts.append(f"<{tag}> {value}".strip())
    group.merged_text = " ".join(parts).strip()
    # Field provenance: walk each pair's union and tag the origin.
    for union in per_pair_unions:
        for tag, (value, origin) in union.items():
            group.fields.append(
                FieldProvenance(
                    tag=tag,
                    value=value,
                    origin=origin,
                    block_id=native_block_id if origin == "native" else "",
                )
            )
    return group


__all__ = [
    "DeclarationGroup",
    "FieldProvenance",
    "merge_group",
    "parse_declarations",
]
