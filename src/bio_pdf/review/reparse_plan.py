"""Bounded region reparse planning (plan B11).

Requests are generated ONLY from specific, actionable problems (a missing
table, a grid conflict, a source conflict, a sequence-length conflict) —
never "results look few, retry the whole document". The budget is explicit
and enforced: N regions per job, M attempts per region, a per-attempt
timeout; exhaustion becomes a ``REPARSE_EXHAUSTED`` issue, not a silent
retry loop.

The region request maps results BACK to the original page and carries the
transform chain (rotation included) so a crop is never mislabelled as
"page 1".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Issue reason codes that justify a region reparse.
_ACTIONABLE = {
    "MISSING_TABLE": "table",
    "TABLE_GRID_OVERLAP": "table",
    "TABLE_STRUCTURE": "table",
    "SOURCE_CONFLICT": "region",
    "TEXT_CONFLICT": "region",
    "SEQ_LENGTH_CONFLICT": "region",
    "ZERO_RESIDUE_LISTING": "region",
}

#: Expected artifact kind per trigger.
_EXPECTED_KIND = {
    "MISSING_TABLE": "table",
    "TABLE_GRID_OVERLAP": "table",
    "TABLE_STRUCTURE": "table",
    "SOURCE_CONFLICT": "text",
    "TEXT_CONFLICT": "text",
    "SEQ_LENGTH_CONFLICT": "sequence",
    "ZERO_RESIDUE_LISTING": "sequence",
}

_PAGE_RE = re.compile(r"p(\d+)|page\s*(\d+)|^(\d+)(?:$|:)", re.IGNORECASE)


@dataclass
class ReparsePolicy:
    max_regions_per_job: int = 10
    max_attempts_per_region: int = 2
    timeout_seconds: float = 120.0


@dataclass
class RegionRequest:
    request_id: str
    job_id: str
    trigger_code: str
    original_page: int
    source_ids: list[str]
    expected_kind: str
    budget_max_attempts: int
    timeout_seconds: float
    #: canonical → pdf-user affine (identity here; filled by the renderer).
    transform: tuple[float, float, float, float, float, float] = (
        1.0, 0.0, 0.0, 1.0, 0.0, 0.0,
    )
    bbox: tuple[float, float, float, float] | None = None
    attempts_used: int = 0


class ReparsePlanner:
    """Turns actionable issues into bounded region requests."""

    def __init__(self, policy: ReparsePolicy | None = None) -> None:
        self.policy = policy or ReparsePolicy()
        self.exhausted_issues: list[dict[str, Any]] = []
        self._attempts: dict[str, int] = {}

    def plan(self, job_id: str, issues: list[dict[str, Any]]) -> list[RegionRequest]:
        requests: list[RegionRequest] = []
        self.exhausted_issues = []
        for issue in issues:
            trigger = str(issue.get("reason_code", ""))
            if trigger not in _ACTIONABLE:
                continue
            if len(requests) >= self.policy.max_regions_per_job:
                self.exhausted_issues.append(
                    {
                        "reason_code": "REPARSE_EXHAUSTED",
                        "source_ids": list(issue.get("source_ids") or []),
                        "detail": (
                            f"region budget ({self.policy.max_regions_per_job}) spent; "
                            f"issue kept for review"
                        ),
                    }
                )
                continue
            page = _page_from_issue(issue)
            if page is None:
                self.exhausted_issues.append(
                    {
                        "reason_code": "REPARSE_NO_REGION",
                        "source_ids": list(issue.get("source_ids") or []),
                        "detail": "issue has no resolvable page for a region request",
                    }
                )
                continue
            request_id = f"rp-{job_id}-{page}-{trigger.lower()}-{len(requests)}"
            requests.append(
                RegionRequest(
                    request_id=request_id,
                    job_id=job_id,
                    trigger_code=trigger,
                    original_page=page,
                    source_ids=list(issue.get("source_ids") or []),
                    expected_kind=_EXPECTED_KIND.get(trigger, "region"),
                    budget_max_attempts=self.policy.max_attempts_per_region,
                    timeout_seconds=self.policy.timeout_seconds,
                )
            )
        return requests

    def next_attempt(self, request: RegionRequest) -> int | None:
        """Consume one attempt; None when the per-region budget is spent."""

        used = self._attempts.get(request.request_id, 0)
        if used >= request.budget_max_attempts:
            return None
        self._attempts[request.request_id] = used + 1
        request.attempts_used = used + 1
        return used + 1


def _page_from_issue(issue: dict[str, Any]) -> int | None:
    """Extract the source page from an issue's source ids (never guessed)."""

    for source in issue.get("source_ids") or []:
        text = str(source)
        match = _PAGE_RE.search(text)
        if match:
            for group in match.groups():
                if group:
                    return int(group)
    detail = str(issue.get("detail") or "")
    match = re.search(r"page\s*(\d+)", detail, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


__all__ = ["ReparsePlanner", "ReparsePolicy", "RegionRequest"]
