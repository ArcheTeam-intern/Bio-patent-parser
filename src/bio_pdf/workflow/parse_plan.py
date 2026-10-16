"""Parse plan: page routing decisions as explicit, auditable tasks (T04).

The page classifier's signals become a :class:`ParsePlan` with a primary
task per page, optional supplemental tasks (native text kept as a lexical
cross-check) and every reason recorded. OCR policy is enforced here:

- ``force`` → every non-blank requested page MUST actually run OCR; the
  native engine cannot, so plan building fails with OCR_UNSUPPORTED.
- ``disabled`` → OCR never runs; scan-suspect pages carry
  SCAN_WITH_OCR_DISABLED and are reported, not silently parsed as text.
- ``auto`` → signals decide.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from bio_pdf.errors import EngineUnavailableError, WorkflowError

PageTaskKind = Literal["native", "layout_native", "layout_ocr", "blank", "skip"]


@dataclass(frozen=True)
class PageTask:
    page_number: int
    kind: PageTaskKind
    reasons: list[str]
    route: str  # classifier route label (kept for audit)


@dataclass
class ParsePlan:
    requested_pages: list[int]
    tasks: list[PageTask]
    ocr_policy: str
    engine: str
    warnings: list[str] = field(default_factory=list)

    def pages_for(self, kinds: set[str]) -> list[int]:
        return [t.page_number for t in self.tasks if t.kind in kinds]

    def to_payload(self) -> dict[str, Any]:
        return {
            "requested_pages": list(self.requested_pages),
            "ocr_policy": self.ocr_policy,
            "engine": self.engine,
            "warnings": list(self.warnings),
            "tasks": [
                {
                    "page_number": t.page_number,
                    "kind": t.kind,
                    "route": t.route,
                    "reasons": list(t.reasons),
                }
                for t in self.tasks
            ],
        }


#: Engines that can perform layout/OCR work.
_LAYOUT_ENGINES = {"docling_sdk", "docling_serve", "mineru_http", "docling"}


def build_parse_plan(
    pages: list[dict[str, Any]],
    *,
    page_numbers: tuple[int, ...],
    engine: str,
    ocr_policy: str,
) -> ParsePlan:
    """Turn inspect-stage page records into an executable plan.

    Args:
        pages: ``[{"signals": {...}, "route": {"route": ..., "reasons": [...]}}]``
            as produced by InspectStage.
        page_numbers: The requested page set (already validated).
        engine: The configured parser.
        ocr_policy: auto | force | disabled.

    Raises:
        EngineUnavailableError: ``force`` with the native engine.
    """

    if ocr_policy == "force" and engine not in _LAYOUT_ENGINES:
        raise EngineUnavailableError(
            f"ocr_policy=force requires a layout engine; parser {engine!r} has no OCR",
            details={"parser": engine, "ocr_policy": ocr_policy, "code": "OCR_UNSUPPORTED"},
        )

    tasks: list[PageTask] = []
    warnings: list[str] = []
    for record in pages:
        try:
            page_number = int(record["signals"]["page_number"])
        except (KeyError, TypeError, ValueError):
            continue
        route = str((record.get("route") or {}).get("route") or "unknown")
        reasons = list((record.get("route") or {}).get("reasons") or [])

        if route == "blank":
            tasks.append(PageTask(page_number, "blank", ["blank page (checked, not skipped)"], route))
            continue

        if ocr_policy == "force":
            # Force means force: every non-blank page runs OCR.
            reasons.append("ocr_policy=force")
            tasks.append(PageTask(page_number, "layout_ocr", reasons, route))
            continue

        if route in ("scan_suspect", "text_suspect", "st25_ocr"):
            if ocr_policy == "disabled":
                reasons.append("SCAN_WITH_OCR_DISABLED")
                warnings.append(
                    f"page {page_number} needs OCR but ocr_policy=disabled; text may be incomplete"
                )
                tasks.append(PageTask(page_number, "native", reasons, route))
            else:
                tasks.append(PageTask(page_number, "layout_ocr", reasons, route))
            continue

        if route in ("native", "mixed", "layout_native"):
            task = PageTask(page_number, "native", reasons, route)
            tasks.append(task)
            continue

        # unknown: parse natively; the reviewer sees the route in quality.json.
        reasons.append("insufficient signals; defaulted to native text")
        tasks.append(PageTask(page_number, "native", reasons, route))

    if ocr_policy == "force":
        # Defensive: the routing above already enforces this; a violation
        # would mean the policy was silently dropped.
        missing = [
            t.page_number for t in tasks if t.kind not in ("layout_ocr", "blank")
        ]
        if missing:
            raise EngineUnavailableError(
                "ocr_policy=force was requested but some pages are not OCR-planned",
                details={"pages": missing, "code": "OCR_UNSUPPORTED"},
            )

    return ParsePlan(
        requested_pages=list(page_numbers),
        tasks=tasks,
        ocr_policy=ocr_policy,
        engine=engine,
        warnings=warnings,
    )


def contiguous_runs(pages: list[int]) -> list[tuple[int, int]]:
    """Split a page list into contiguous inclusive ranges.

    ``[2, 4]`` → ``[(2, 2), (4, 4)]`` — the Docling child must never be
    asked for ``range(2, 5)`` (which would pull page 3 into the result).
    """

    ordered = sorted(set(pages))
    if not ordered:
        return []
    runs: list[tuple[int, int]] = []
    start = prev = ordered[0]
    for page in ordered[1:]:
        if page == prev + 1:
            prev = page
            continue
        runs.append((start, prev))
        start = prev = page
    runs.append((start, prev))
    return runs


@dataclass(frozen=True)
class ExecutionRun:
    """One engine invocation: (engine profile kind, contiguous pages)."""

    kind: PageTaskKind  # native | layout_native | layout_ocr | blank | skip
    pages: tuple[int, ...]


def group_execution_runs(plan: ParsePlan) -> list[ExecutionRun]:
    """Collapse the plan into executable invocations (plan B04).

    Pages are grouped by task kind, then split into contiguous runs so one
    invocation never mixes OCR and non-OCR pages: a mixed document runs
    ``layout_ocr`` for exactly its scan pages and ``layout_native`` for the
    text pages — the previous "one layout_native pass over everything"
    silently skipped OCR for scan pages.

    ``blank``/``skip`` pages produce no invocation.
    """

    by_kind: dict[str, list[int]] = {}
    for task in plan.tasks:
        if task.kind in ("blank", "skip"):
            continue
        by_kind.setdefault(task.kind, []).append(task.page_number)

    runs: list[ExecutionRun] = []
    for kind in ("layout_ocr", "layout_native", "native"):
        pages = by_kind.get(kind)
        if not pages:
            continue
        for start, end in contiguous_runs(pages):
            runs.append(
                ExecutionRun(kind=kind, pages=tuple(range(start, end + 1)))
            )
    return runs


__all__ = ["ParsePlan", "PageTask", "build_parse_plan", "contiguous_runs"]
