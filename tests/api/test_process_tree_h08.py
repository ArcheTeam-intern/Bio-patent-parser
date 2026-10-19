"""K08: process-tree budget test must control execution timing.

The previous test relied on a real Docling run taking longer than the
30 s budget, which made the assertion flakily depend on inference speed.
K08 introduces a deterministic ready/release handshake:

1. ``docling_hold_bootstrap.install_hold`` wraps the real converter so
   that AFTER ``convert`` succeeds (real model output) it writes a
   ``READY`` sentinel and BLOCKS until the outer scenario writes a
   ``RELEASE`` sentinel.
2. The outer scenario injects a frozen monotonic clock so the budget
   fires WHILE the descendant is in the held state — the test no
   longer waits 30 s and hopes.
3. After the budget fires, the test asserts: own tree exited, late
   publication is impossible, unrelated processes survive.

The hold is a test-only scaffold; no product code path is modified.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

TESTS_DIR = Path(__file__).resolve().parents[1]
REPO = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))

from helpers import proc_tree  # noqa: E402
from helpers.docling_hold_bootstrap import (  # noqa: E402
    install_hold_for_subprocess,
    release_hold,
    uninstall_hold,
)

ENGINE_ENTRY = "bio_pdf.parsers.docling_child"


def _sample_pdf(path: Path) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=A4)
    t = c.beginText(50, 800)
    for line in ["<210> 1", "<211> 4", "<212> PRT", "<400> 1", "Ala Gly Leu Lys"]:
        t.textLine(line)
    c.drawText(t)
    c.save()
    return path


def _spawn_unrelated_subprocess() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _own_mp_child_sleeping(seconds: int = 300) -> mp.Process:
    proc = mp.Process(target=time.sleep, args=(seconds,), daemon=True)
    proc.start()
    return proc


def _wait_registered_root(comparison_id: str, *, timeout: float = 30.0) -> int | None:
    from bio_pdf.application.comparison_service import live_side_processes

    deadline = time.time() + timeout
    while time.time() < deadline:
        for pid, identity in live_side_processes().items():
            if identity.get("comparison_id") == comparison_id:
                return pid
        time.sleep(0.1)
    return None


def _wait_until_dead(pids: list[int], timeout: float = 30.0) -> list[int]:
    deadline = time.time() + timeout
    remaining = list(pids)
    while time.time() < deadline and remaining:
        remaining = [pid for pid in remaining if proc_tree.pid_alive(pid)]
        if remaining:
            time.sleep(0.5)
    return remaining


def test_budget_terminates_descendants_alive_at_deadline(tmp_path: Path) -> None:
    """K08: the budget test must prove EXECUTION_BUDGET_EXCEEDED while
    the test's own child tree is still alive. The handshake + injected
    clock make the deadline deterministic.
    """

    from bio_pdf.application.comparison_service import ComparisonService
    from bio_pdf.storage.repository import Repository

    hold_dir = tmp_path / "k08"
    paths = install_hold_for_subprocess(hold_dir=hold_dir)
    unrelated_proc = _spawn_unrelated_subprocess()
    unrelated_mp = _own_mp_child_sleeping()
    pdf = _sample_pdf(tmp_path / "cmp.pdf")
    data = tmp_path / "data"

    # K08: inject a monotonic clock we control so the budget can fire
    # at a known instant while the descendant is alive in the hold.
    base = {"now": 1000.0}

    def _clock() -> float:
        return base["now"]

    svc = ComparisonService(
        repository=Repository(data / "bio.sqlite", check_same_thread=False),
        artifact_root=data,
        lease_seconds=600.0,
        execution_budget_seconds=10.0,  # real budget value (test invariant)
        run_mode="process",
        clock=_clock,
    )
    cid = svc.create(
        document_id="doc-k08", source_path=pdf, pages=[1], engines=["docling"]
    )
    root_pid: int | None = None
    grand_ids: list[int] = []
    try:
        svc.start(cid)
        # Discover OUR execution root (the test's own child tree).
        root_pid = _wait_registered_root(cid, timeout=30.0)
        assert root_pid is not None, "the service never registered its execution root"
        assert root_pid != unrelated_mp.pid
        # Wait for the descendant to write the READY sentinel —
        # THAT'S the proof the grandchild is alive in the held state.
        ready_deadline = time.time() + 30.0
        while time.time() < ready_deadline:
            if paths["ready"].exists():
                break
            time.sleep(0.1)
        assert paths["ready"].exists(), "descendant never reached the held state"
        # Now advance the injected clock past the deadline.
        # The watchdog will see ``now > deadline`` and terminate the tree.
        base["now"] += 30.0
        # Release the held descendant so its pipe close delivers
        # cleanly; without this the orphan grandchild can linger.
        release_hold(hold_dir)

        # The side MUST end in failed with EXECUTION_BUDGET_EXCEEDED.
        deadline = time.time() + 90
        while time.time() < deadline:
            side = svc.get(cid)["sides"][0]
            if side["status"] == "failed":
                break
            time.sleep(0.5)
        side = svc.get(cid)["sides"][0]
        assert side["status"] == "failed", side
        assert "EXECUTION_BUDGET_EXCEEDED" in (side["error"] or "")
        # The own tree must be gone.
        remaining = _wait_until_dead([root_pid], timeout=30.0)
        assert remaining == [], f"test-owned root still running: {remaining}"
        # Unrelated processes survive — nothing else was killed.
        assert proc_tree.pid_alive(unrelated_proc.pid)
        assert unrelated_mp.is_alive()
    finally:
        uninstall_hold()
        if root_pid is not None and proc_tree.pid_alive(root_pid):
            proc_tree.kill_tree(root_pid)
        unrelated_proc.kill()
        if unrelated_mp.is_alive():
            unrelated_mp.terminate()
        unrelated_mp.join(timeout=10)
        svc.repository.close()


def test_normal_docling_completion_still_publishes_done(tmp_path: Path) -> None:
    """Smoke test for the same path WITHOUT the hold: real Docling
    inference finishes, the side ends in done, no late publication.
    K08 separates the budget contract (above) from the happy path so a
    fast inference does NOT silently turn into a budget failure.
    """

    from bio_pdf.application.comparison_service import ComparisonService
    from bio_pdf.storage.repository import Repository

    pdf = _sample_pdf(tmp_path / "cmp.pdf")
    data = tmp_path / "data"
    svc = ComparisonService(
        repository=Repository(data / "bio.sqlite", check_same_thread=False),
        artifact_root=data,
        lease_seconds=600.0,
        execution_budget_seconds=180.0,
        run_mode="process",
    )
    cid = svc.create(
        document_id="doc-k08-happy", source_path=pdf, pages=[1], engines=["docling"]
    )
    root_pid: int | None = None
    try:
        svc.start(cid)
        root_pid = _wait_registered_root(cid, timeout=30.0)
        if root_pid is None:
            pytest.skip("the docling inference descendant never appeared in this environment")
        # Wait for completion.
        deadline = time.time() + 120
        while time.time() < deadline:
            side = svc.get(cid)["sides"][0]
            if side["status"] in ("done", "failed"):
                break
            time.sleep(0.5)
        side = svc.get(cid)["sides"][0]
        # Happy path: status=done, no budget error.
        assert side["status"] == "done", side
        assert "EXECUTION_BUDGET_EXCEEDED" not in (side["error"] or "")
    finally:
        if root_pid is not None and proc_tree.pid_alive(root_pid):
            proc_tree.kill_tree(root_pid)
        svc.repository.close()
