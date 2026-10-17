"""H01 + I01: the budget kills the WHOLE process tree — scoped to OUR tree.

The old tests used a machine-wide ``*docling_child*`` CIM match for both
the "grandchild started" wait and the cleanup — so the query command itself
could satisfy the wait (its command line contained the pattern), and a
cleanup could hit unrelated jobs' inference processes. Everything here is
scoped by ParentProcessId chains from the comparison child THIS test
spawned; the discovery query never contains the engine-entry string, and
cleanup only ever targets the recorded root PID tree.

A real unrelated long-running process runs beside each scenario and must
survive the test.
"""

from __future__ import annotations

import json
import os
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

ENGINE_ENTRY = "bio_pdf.parsers.docling_child"


def _sample_pdf(path: Path) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    t = c.beginText(50, 800)
    for line in ["<210> 1", "<211> 4", "<212> PRT", "<400> 1", "Ala Gly Leu Lys"]:
        t.textLine(line)
    c.drawText(t)
    c.save()
    return path


def _spawn_unrelated() -> subprocess.Popen:
    """A long-running process that is NOT part of any execution tree."""

    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_children(timeout: float = 30.0) -> list[int]:
    import multiprocessing as mp

    deadline = time.time() + timeout
    while time.time() < deadline:
        children = [p.pid for p in mp.active_children()]
        if children:
            return children
        time.sleep(0.2)
    return []


def _wait_until_dead(pids: list[int], timeout: float = 30.0) -> list[int]:
    deadline = time.time() + timeout
    remaining = list(pids)
    while time.time() < deadline and remaining:
        remaining = [pid for pid in remaining if proc_tree.pid_alive(pid)]
        if remaining:
            time.sleep(1)
    return remaining


def test_budget_timeout_terminates_our_own_docling_descendants(tmp_path: Path) -> None:
    """Wait until OUR comparison child has a real inference descendant, let
    the budget fire, and assert exactly that subtree is gone — while an
    unrelated process keeps running."""

    from bio_pdf.application.comparison_service import ComparisonService
    from bio_pdf.storage.repository import Repository

    unrelated = _spawn_unrelated()
    pdf = _sample_pdf(tmp_path / "cmp.pdf")
    data = tmp_path / "data"
    svc = ComparisonService(
        repository=Repository(data / "bio.sqlite", check_same_thread=False),
        artifact_root=data,
        lease_seconds=600.0,
        execution_budget_seconds=30.0,
        run_mode="process",
    )
    cid = svc.create(document_id="doc-i01", source_path=pdf, pages=[1], engines=["docling"])
    middle_ids: list[int] = []
    grand_ids: list[int] = []
    try:
        svc.start(cid)
        middle_ids = _wait_children(30.0)
        assert middle_ids, "the comparison child process never appeared"
        middle = middle_ids[0]
        # I01: discovery is scoped to our child's descendants; the query
        # itself is not among them and cannot satisfy the wait.
        matches = proc_tree.wait_for_engine_descendant(middle, ENGINE_ENTRY, timeout=60.0)
        if not matches:
            pytest.skip("the docling inference descendant never appeared in this environment")
        grand_ids = [m.pid for m in matches]
        assert all(ENGINE_ENTRY in m.cmdline for m in matches)
        # Parent-child relation is guaranteed by construction; also require
        # a real creation timestamp for each descendant.
        assert all(m.created for m in matches), "descendants must be reachable with creation data"

        deadline = time.time() + 90
        while time.time() < deadline:
            side = svc.get(cid)["sides"][0]
            if side["status"] == "failed":
                break
            time.sleep(0.5)
        side = svc.get(cid)["sides"][0]
        assert side["status"] == "failed", side
        assert "EXECUTION_BUDGET_EXCEEDED" in (side["error"] or "")
        remaining = _wait_until_dead(grand_ids + middle_ids, timeout=30.0)
        assert remaining == [], f"test-owned descendants still running: {remaining}"
        assert proc_tree.pid_alive(unrelated.pid), "an unrelated process must survive"
        # The discovery query never carries the engine-entry string, so it
        # cannot be mistaken for an inference descendant even in principle.
        query_procs = [
            info
            for info in proc_tree.descendants_of(os.getpid())
            if "Get-CimInstance" in info.cmdline
        ]
        assert all(ENGINE_ENTRY not in info.cmdline for info in query_procs)
    finally:
        # Cleanup is scoped to THIS test's own subtree roots — never a
        # machine-wide match.
        for pid in middle_ids:
            if proc_tree.pid_alive(pid):
                proc_tree.kill_tree(pid)
        unrelated.kill()
        svc.repository.close()


_SCENARIO = '''
import json
import sys
import time
from pathlib import Path

from helpers import proc_tree

from bio_pdf.application.comparison_service import ComparisonService
from bio_pdf.storage.repository import Repository


def main() -> None:
    data = Path(sys.argv[1])
    out = Path(sys.argv[2])
    pdf = Path(sys.argv[3])
    svc = ComparisonService(
        repository=Repository(data / "bio.sqlite", check_same_thread=False),
        artifact_root=data,
        lease_seconds=600.0,
        execution_budget_seconds=600.0,
        run_mode="process",
    )
    cid = svc.create(document_id="doc-i01b", source_path=pdf, pages=[1], engines=["docling"])
    svc.start(cid)
    import multiprocessing as mp
    middle = []
    deadline = time.time() + 60
    while time.time() < deadline:
        middle = [p.pid for p in mp.active_children()]
        if middle:
            break
        time.sleep(0.2)
    grand = []
    if middle:
        grand = [
            m.pid
            for m in proc_tree.wait_for_engine_descendant(
                middle[0], "bio_pdf.parsers.docling_child", timeout=90.0
            )
        ]
    out.write_text(json.dumps({"middle": middle, "grand": grand}), encoding="utf-8")
    # NORMAL interpreter exit -> our atexit sweeps the registered trees.


main()
'''


def test_service_shutdown_sweeps_only_our_trees(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario_i01.py"
    scenario.write_text(_SCENARIO, encoding="utf-8")
    pdf = _sample_pdf(tmp_path / "cmp.pdf")
    data = tmp_path / "data"
    out = tmp_path / "pids.json"
    unrelated = _spawn_unrelated()
    try:
        proc = subprocess.run(
            [sys.executable, str(scenario), str(data), str(out), str(pdf)],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=300,
            cwd=str(REPO),
            env={**os.environ, "PYTHONPATH": str(TESTS_DIR)},
        )
        assert proc.returncode == 0, proc.stderr[-800:]
        recorded = json.loads(out.read_text(encoding="utf-8"))
        assert recorded["middle"], "the scenario never spawned its comparison child"
        if not recorded["grand"]:
            pytest.skip("the docling inference descendant never appeared in this environment")
        remaining = _wait_until_dead(
            recorded["grand"] + recorded["middle"], timeout=30.0
        )
        assert remaining == [], f"test-owned processes survived shutdown: {remaining}"
        assert proc_tree.pid_alive(unrelated.pid), "an unrelated process must survive"
    finally:
        unrelated.kill()


def test_wait_fails_without_an_engine_descendant() -> None:
    """I01: with only the query process around (no inference child), the
    wait MUST time out — the old machine-wide match returned the query
    itself (false positive)."""

    matches = proc_tree.wait_for_engine_descendant(
        os.getpid(), ENGINE_ENTRY, timeout=4.0, poll_interval=1.0
    )
    assert matches == []
