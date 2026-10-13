"""Per-page quality classification.

The classifier runs before we commit to OCR-only routing. We deliberately keep
it conservative and surface unknowns rather than over-committing to a route.
"""

from __future__ import annotations

from bio_pdf.contracts import PageQuality, QualityConfig, RouteDecision

RULE_VERSION = "v0"


def inspect_page_signals(
    *,
    page_number: int,
    text_chars: str,
    image_area_pt2: float,
    page_area_pt2: float,
    repeated_char_ratio: float = 0.0,
    hidden_text_flag: bool = False,
) -> PageQuality:
    """Compute quality signals from raw page-level primitives.

    Args:
        page_number: 1-based page index.
        text_chars: The concatenated glyphs in reading order.
        image_area_pt2: Sum of image rectangle areas (points²). Caller is
            responsible for taking the *union* of overlapping rectangles so the
            coverage does not exceed 100%.
        page_area_pt2: The visible page area in points².
        repeated_char_ratio: Fraction of characters that are non-printable
            (e.g. control bytes).
        hidden_text_flag: True if the text is from an invisible / hidden
            layer.
    """

    if page_area_pt2 <= 0:
        raise ValueError(f"page_area_pt2 must be positive, got {page_area_pt2}")

    nonspace_chars = sum(1 for c in text_chars if not c.isspace())
    printable_chars = sum(1 for c in text_chars if c.isprintable() or c.isspace())
    printable_ratio = printable_chars / len(text_chars) if text_chars else 1.0
    replacement_count = sum(1 for c in text_chars if c == "�")
    replacement_ratio = replacement_count / max(len(text_chars), 1)
    image_coverage = min(max(image_area_pt2 / page_area_pt2, 0.0), 1.0)

    return PageQuality(
        page_number=page_number,
        nonspace_chars=nonspace_chars,
        printable_ratio=float(printable_ratio),
        replacement_ratio=float(replacement_ratio),
        image_coverage=float(image_coverage),
        repeated_char_ratio=float(repeated_char_ratio),
        hidden_text_flag=hidden_text_flag,
        warnings=[],
    )


def classify_page(signals: PageQuality, config: QualityConfig) -> RouteDecision:
    """Classify a page from its quality signals.

    Returns a :class:`RouteDecision` with at least one ``reasons`` line.
    """

    reasons: list[str] = []
    route: str = "unknown"

    if signals.nonspace_chars <= config.blank_max_chars and signals.image_coverage < 0.05:
        return RouteDecision(route="blank", reasons=["page has no printable text"], rule_version=RULE_VERSION)

    if signals.replacement_ratio > config.replacement_ratio:
        route = "text_suspect"
        reasons.append(
            f"replacement_ratio={signals.replacement_ratio:.3f} exceeds threshold {config.replacement_ratio:.3f}"
        )

    if signals.image_coverage >= config.scan_image_coverage and signals.nonspace_chars < config.low_text_char_count:
        route = "scan_suspect"
        reasons.append(
            f"image_coverage={signals.image_coverage:.3f} with only {signals.nonspace_chars} chars"
        )

    if signals.hidden_text_flag and signals.nonspace_chars > config.low_text_char_count:
        # Hidden OCR layer with plenty of text: treat as suspect, do not promote
        # to native without downstream verification.
        reasons.append("text is from a hidden OCR layer")
        if route not in {"scan_suspect", "text_suspect"}:
            route = "text_suspect"

    if route == "unknown":
        if signals.image_coverage >= 0.2 and signals.nonspace_chars >= config.low_text_char_count:
            route = "mixed"
            reasons.append("page contains readable text plus non-trivial image area")
        elif signals.nonspace_chars >= config.low_text_char_count:
            route = "native"
            reasons.append("page has readable native text")
        else:
            route = "unknown"
            reasons.append("insufficient signals to classify confidently")

    return RouteDecision(route=route, reasons=reasons, rule_version=RULE_VERSION)