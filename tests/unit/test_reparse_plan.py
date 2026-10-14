"""B11: aligned diffs, bounded reparse planning and budget enforcement."""

from __future__ import annotations

from bio_pdf.review.compare import compare_sequences
from bio_pdf.review.reparse_plan import ReparsePlanner, ReparsePolicy


# ---- aligned diff ----------------------------------------------------------

def test_unequal_lengths_show_a_real_insertion_not_a_crash() -> None:
    diff = compare_sequences("AG", "AGL")
    assert len(diff.conflicts) == 1
    position, left_char, right_char = diff.conflicts[0]
    assert left_char == ""  # gap on the left
    assert right_char == "L"
    assert diff.aligned_left == "AG-"
    assert diff.aligned_right == "AGL"
    # Full sequences preserved.
    assert diff.left == "AG" and diff.right == "AGL"
    assert diff.describe()


def test_deletion_and_substitution_positions() -> None:
    deleted = compare_sequences("AGLK", "AGK")
    assert deleted.aligned_left.startswith("A")
    assert any(left == "L" and right == "" for _, left, right in deleted.conflicts)

    substituted = compare_sequences("AGLK", "AG1K")
    assert substituted.conflicts == [(2, "L", "1")]
    assert substituted.edit_distance == 1


def test_identical_and_long() -> None:
    assert compare_sequences("AGLK", "AGLK").identical
    long_left = "A" * 2600
    long_right = "A" * 2599 + "G"
    diff = compare_sequences(long_left, long_right)  # fallback path
    assert diff.conflicts  # no crash, diff visible


# ---- reparse planning & budget --------------------------------------------

def test_planner_generates_requests_for_actionable_issues() -> None:
    planner = ReparsePlanner(ReparsePolicy())
    issues = [
        {"scope": "table", "reason_code": "MISSING_TABLE", "source_ids": ["p8"], "detail": "page 8"},
        {"scope": "table", "reason_code": "TABLE_GRID_OVERLAP", "source_ids": ["p7:t0"], "detail": "overlap"},
        {"scope": "mention", "reason_code": "RESULT_WITHOUT_METRIC", "source_ids": ["ev-1"], "detail": "x"},
    ]
    requests = planner.plan("job-1", issues)
    codes = {r.trigger_code for r in requests}
    # Only actionable triggers get a region request.
    assert "MISSING_TABLE" in codes
    assert "TABLE_GRID_OVERLAP" in codes
    assert "RESULT_WITHOUT_METRIC" not in codes
    for request in requests:
        assert request.job_id == "job-1"
        assert request.budget_max_attempts == 2
        assert request.timeout_seconds > 0


def test_budget_exhaustion_is_reported_not_ignored() -> None:
    planner = ReparsePlanner(ReparsePolicy(max_regions_per_job=2))
    issues = [
        {"scope": "table", "reason_code": "MISSING_TABLE", "source_ids": [f"p{i}"], "detail": ""}
        for i in range(5)
    ]
    requests = planner.plan("job-1", issues)
    assert len(requests) == 2
    exhausted = planner.exhausted_issues
    assert len(exhausted) == 3
    assert all(e["reason_code"] == "REPARSE_EXHAUSTED" for e in exhausted)


def test_attempt_budget_is_bounded() -> None:
    planner = ReparsePlanner(ReparsePolicy(max_regions_per_job=10, max_attempts_per_region=2))
    requests = planner.plan(
        "job-1",
        [{"scope": "table", "reason_code": "MISSING_TABLE", "source_ids": ["p8"], "detail": ""}],
    )
    request = requests[0]
    assert planner.next_attempt(request) == 1
    assert planner.next_attempt(request) == 2
    assert planner.next_attempt(request) is None  # budget spent


def test_region_request_maps_back_to_the_original_page() -> None:
    """A region on page 8 keeps page 8 + its transform — never 'page 1'."""

    planner = ReparsePlanner(ReparsePolicy())
    requests = planner.plan(
        "job-1",
        [{"scope": "table", "reason_code": "MISSING_TABLE", "source_ids": ["p8"], "detail": ""}],
    )
    request = requests[0]
    assert request.original_page == 8
    assert request.expected_kind == "table"
    # The transform chain includes rotation (identity here, but present).
    assert len(request.transform) == 6
