"""Acceptance policy.

``validation_state`` (did the rules pass?) is deliberately separate from
``acceptance_state`` (is it good enough to trust?). Passing length/alphabet
checks only yields ``checks_passed``; acceptance additionally requires an
explicit reference match or human verification when ``auto_accept`` is off
(the default in this phase).
"""

from __future__ import annotations

from typing import Any

from bio_pdf.contracts import AcceptanceDecision

#: Current acceptance policy version. Bump whenever the decision rules change
#: so historic decisions can be reproduced.
POLICY_VERSION = "v0"

_REASON_RULE_PASS_ONLY = "REQUIRES_HUMAN_OR_REFERENCE_VERIFICATION"
_REASON_CONFLICT = "VALIDATION_CONFLICT"
_REASON_AUTO_ACCEPT = "AUTO_ACCEPT_POLICY"


def decide_acceptance(
    record: dict[str, Any],
    *,
    auto_accept: bool = False,
) -> AcceptanceDecision:
    """Decide the acceptance state for a language-neutral record dict.

    Args:
        record: A ``model_dump()`` view of a sequence/measurement record. It
            must contain at least ``validation_state``; ``checks`` and
            ``acceptance_state`` are read if present.
        auto_accept: When True, a ``checks_passed`` record without conflicts
            may be accepted automatically (used only behind an evaluated
            policy + calibration report).

    Returns:
        An :class:`AcceptanceDecision`. Passing checks alone never produces
        ``accepted`` unless an external verification method is present.
    """

    validation_state = str(record.get("validation_state", "unchecked"))
    checks = record.get("checks", []) or []
    has_fail = any(
        isinstance(c, dict) and c.get("outcome") == "fail" for c in checks
    )

    # External verification wins: an official reference or a human already
    # confirmed this record.
    if validation_state in ("verified_reference", "verified_human"):
        return AcceptanceDecision(
            acceptance_state="accepted",
            verification_method=validation_state,
            reason_codes=[],
            policy_version=POLICY_VERSION,
        )

    # Conflicts always require review; they are never auto-accepted.
    if validation_state == "conflict" or has_fail:
        return AcceptanceDecision(
            acceptance_state="needs_review",
            verification_method=None,
            reason_codes=[_REASON_CONFLICT],
            policy_version=POLICY_VERSION,
        )

    if auto_accept and validation_state == "checks_passed":
        return AcceptanceDecision(
            acceptance_state="accepted",
            verification_method="auto_accept",
            reason_codes=[_REASON_AUTO_ACCEPT],
            policy_version=POLICY_VERSION,
        )

    # Rules passing is not proof of truth. With auto_accept off, everything
    # that isn't externally verified needs human review.
    return AcceptanceDecision(
        acceptance_state="needs_review",
        verification_method=None,
        reason_codes=[_REASON_RULE_PASS_ONLY],
        policy_version=POLICY_VERSION,
    )


__all__ = ["decide_acceptance", "POLICY_VERSION"]