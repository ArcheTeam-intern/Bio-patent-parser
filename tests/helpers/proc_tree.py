"""I01/S01: descendant-process discovery scoped to ONE test-owned tree.

The H01 tests must prove that the inference grandchild of THEIR OWN
comparison child was terminated — never machine-wide ``*docling_child*``
matches (which would also match the query command itself and could kill
unrelated jobs' inference processes).

This helper reads the process table once (a read-only CIM query), builds a
parent→children map, and walks ONLY the descendants of a caller-supplied
root PID. The PowerShell query itself is never part of that subtree, so it
cannot satisfy a "grandchild started" wait.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    cmdline: str
    created: str


def _query_process_table() -> dict[int, ProcInfo]:
    """Read {pid: ProcInfo} from the live process table (read-only)."""

    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,"
                "CommandLine,CreationDate | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
        )
    except Exception:  # noqa: BLE001 — discovery is best-effort
        return {}
    body = (result.stdout or "").strip()
    if not body:
        return {}
    try:
        raw = json.loads(body)
    except json.JSONDecodeError:
        return {}
    if isinstance(raw, dict):
        raw = [raw]
    table: dict[int, ProcInfo] = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item.get("ProcessId"))
            ppid = int(item.get("ParentProcessId"))
        except (TypeError, ValueError):
            continue
        table[pid] = ProcInfo(
            pid=pid,
            ppid=ppid,
            cmdline=str(item.get("CommandLine") or ""),
            created=str(item.get("CreationDate") or ""),
        )
    return table


def descendants_of(root_pid: int) -> list[ProcInfo]:
    """Transitive descendants of ``root_pid`` — nothing else.

    The relationship is decided by ParentProcessId chains from the caller's
    own PID, so the discovery query process (a child of pytest, not of the
    comparison child) can never appear in the result.
    """

    table = _query_process_table()
    if not table or root_pid not in table:
        return []
    children: dict[int, list[ProcInfo]] = {}
    for info in table.values():
        children.setdefault(info.ppid, []).append(info)
    found: list[ProcInfo] = []
    frontier = [root_pid]
    seen: set[int] = {root_pid}
    while frontier:
        parent = frontier.pop()
        for child in children.get(parent, []):
            if child.pid in seen:
                continue
            seen.add(child.pid)
            found.append(child)
            frontier.append(child.pid)
    return found


def wait_for_engine_descendant(
    root_pid: int,
    engine_entry: str,
    *,
    timeout: float = 60.0,
    poll_interval: float = 2.0,
) -> list[ProcInfo]:
    """Wait until a descendant of ``root_pid`` runs ``engine_entry``.

    Returns the matching descendants (empty on timeout). "The query itself
    matched" is impossible here: matches come from the descendant set.
    """

    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        matches = [
            info for info in descendants_of(root_pid) if engine_entry in info.cmdline
        ]
        if matches:
            return matches
        time.sleep(poll_interval)
    return []


def pid_alive(pid: int) -> bool:
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30,
    )
    return str(pid) in (result.stdout or "")


def kill_tree(pid: int) -> None:
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(pid)],
        capture_output=True,
        errors="replace",
        timeout=30,
    )
