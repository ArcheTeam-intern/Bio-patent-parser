"""Engine comparison experiments (C11/D07).

A comparison runs the SAME document, SAME pages and SAME domain rules
through two engines, each side independently — never through the job queue:
no review items are registered and no publication is written, so an
experiment cannot pollute the formal review queue (plan §5.5).

The domain pass mirrors the extract stage's core rules (the shared
functions, not a re-implementation): blocks → ST.25 sequences + inventory →
table measurements. Entities/relations and evidence publishing stay out of
scope and are listed as such in every diff payload.

Execution uses a background thread; status lives in the database so the UI
can poll, retry failed sides, and read each side's error independently.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bio_pdf.errors import WorkflowError
from bio_pdf.storage.repository import Repository

#: Default engine pair for the comparison page.
DEFAULT_ENGINES = ("docling", "mineru_http")


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _side_engine(engine: str, source_path: Path, request: Any) -> Any:
    """Resolve the adapter for one side (patchable in tests)."""

    if engine in ("docling", "docling_sdk"):
        from bio_pdf.parsers.docling_adapter import DoclingAdapter

        return DoclingAdapter()
    if engine == "mineru_http":
        import os

        from bio_pdf.parsers.mineru_adapter import MinerUAdapter

        token = os.environ.get("MINERU_TOKEN", "")
        if not token:
            raise WorkflowError(
                "mineru_http requires MINERU_TOKEN; the cloud side is not runnable here",
                details={"engine": engine, "code": "ENGINE_UNAVAILABLE"},
            )
        return MinerUAdapter(token=token)
    raise WorkflowError(f"unknown comparison engine: {engine}", details={"engine": engine})



def _table_anchor(table: Any) -> str:
    """Semantic table identity for cross-engine pairing (F06/P02).

    Docling and MinerU each invent their OWN local table ids, so those must
    never participate in matching. The anchor is built from semantics both
    engines can agree on: the caption when present, otherwise the header
    cells' normalized text. The local ``table_id`` stays in the payload as
    an evidence reference only.
    """

    import re

    def norm(text: str) -> str:
        return re.sub(r"\s+", " ", str(text)).strip().lower()

    caption = getattr(table, "caption", None)
    if caption:
        return f"caption:{norm(caption)}"
    header_cells = [
        cell
        for cell in table.cells
        if str(getattr(cell, "header_role", "unknown")) in ("column", "row", "both")
        and cell.text_raw
    ]
    if not header_cells:
        header_cells = [cell for cell in table.cells if cell.row_index == 0 and cell.text_raw]
    signature = "|".join(
        norm(cell.text_raw)
        for cell in sorted(header_cells, key=lambda c: (c.row_index, c.column_index))
    )
    return f"header:{signature}" if signature else "header:"


def pair_measurements(ms_a: list[dict[str, Any]], ms_b: list[dict[str, Any]]) -> dict[str, Any]:
    """F06/P02 cross-engine activity pairing.

    Identity = (entity, metric, target, structured conditions, page,
    semantic table anchor, row/column context) — NEVER the parser-local
    ``table_id`` (engines invent their own; a re-detection must not turn
    a pairing into "everything missing") and never the value (a value
    difference must surface as a diff, not as two missing rows).

    Group-based and order-invariant: only unique-on-both-sides candidates
    are compared; a key with multiple candidates on either side is
    reported AMBIGUOUS with its candidate sets — never dictionary
    last-wins. Values compare STRUCTURALLY (kind/operator/Decimal numbers/
    qualifier); units get only glyph+case+space normalization (µ/μ→u),
    never magnitude conversion (mg ≠ g is a reported unit difference).
    """

    from collections import defaultdict
    from decimal import Decimal, InvalidOperation

    def norm_text(value: Any) -> str:
        import re

        return re.sub(r"\s+", " ", str(value or "")).strip().lower()

    def norm_unit(u: str | None) -> str:
        return (u or "").strip().lower().replace("µ", "u").replace("μ", "u").replace(" ", "")

    def conditions_key(m: dict[str, Any]) -> tuple:
        items = []
        for cond in m.get("conditions") or []:
            if isinstance(cond, dict):
                items.append(
                    tuple(sorted((norm_text(k), norm_text(v)) for k, v in cond.items()))
                )
            else:
                items.append((norm_text(cond),))
        return tuple(sorted(items, key=str))

    def measurement_key(m: dict[str, Any]) -> tuple:
        return (
            norm_text(m.get("entity")),
            norm_text(m.get("metric")),
            norm_text(m.get("target")),
            conditions_key(m),
            int(m.get("page_number") or 0),
            norm_text(m.get("table_anchor")),
            norm_text(m.get("row_context")),
            norm_text(m.get("column_context")),
        )

    def group(ms: list[dict[str, Any]]) -> dict[tuple, list[dict[str, Any]]]:
        out: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
        for m in ms:
            out[measurement_key(m)].append(m)
        return out

    def num(value: Any) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            return None

    def structured(m: dict[str, Any]) -> dict[str, Any]:
        value = m.get("value_structured")
        return value if isinstance(value, dict) else {}

    def compare_values(ma: dict[str, Any], mb: dict[str, Any]) -> dict[str, bool]:
        """Structured comparison → {operator_differs, value_differs}.

        Operator and value are SEPARATE axes: '≤5' vs '5' is an operator
        change (the number itself is equal), '5' vs '6' is a value change.
        """

        va, vb = structured(ma), structured(mb)
        if va and vb and va.get("kind") and va.get("kind") == vb.get("kind"):
            operator_differs = (va.get("operator") or "") != (vb.get("operator") or "")
            numbers_equal = all(
                num(va.get(field)) == num(vb.get(field))
                for field in ("number", "lower", "upper", "uncertainty")
            )
            qualifiers_equal = norm_text(va.get("qualifier")) == norm_text(vb.get("qualifier"))
            return {
                "operator_differs": operator_differs,
                "value_differs": not (numbers_equal and qualifiers_equal),
            }
        na, nb = num(ma.get("value_raw")), num(mb.get("value_raw"))
        if na is not None and nb is not None:
            return {"operator_differs": False, "value_differs": na != nb}
        return {
            "operator_differs": False,
            "value_differs": str(ma.get("value_raw") or "") != str(mb.get("value_raw") or ""),
        }

    ga, gb = group(ms_a), group(ms_b)
    keys = set(ga) | set(gb)
    paired = 0
    ambiguous: list[dict[str, Any]] = []
    value_diffs: list[dict[str, Any]] = []
    only_a: list[list[str]] = []
    only_b: list[list[str]] = []
    for key in sorted(keys, key=str):
        ca, cb = ga.get(key, []), gb.get(key, [])
        if len(ca) > 1 or len(cb) > 1:
            ambiguous.append(
                {
                    "key": [
                        part if isinstance(part, str) else str(part) for part in key
                    ],
                    "candidates_a": [
                        {
                            "value_raw": m.get("value_raw"),
                            "unit_raw": m.get("unit_raw"),
                            "conditions": m.get("conditions") or [],
                            "table_id": m.get("table_id"),
                        }
                        for m in ca
                    ],
                    "candidates_b": [
                        {
                            "value_raw": m.get("value_raw"),
                            "unit_raw": m.get("unit_raw"),
                            "conditions": m.get("conditions") or [],
                            "table_id": m.get("table_id"),
                        }
                        for m in cb
                    ],
                    "reason": "multiple candidates on at least one side",
                }
            )
            continue
        if ca and not cb:
            only_a.append([str(part) for part in key])
        elif cb and not ca:
            only_b.append([str(part) for part in key])
        else:
            paired += 1
            ma, mb = ca[0], cb[0]
            comparison = compare_values(ma, mb)
            units_differ = norm_unit(ma.get("unit_normalized") or ma.get("unit_raw")) != norm_unit(
                mb.get("unit_normalized") or mb.get("unit_raw")
            )
            kinds = []
            if comparison["operator_differs"]:
                kinds.append("operator")
            if comparison["value_differs"]:
                kinds.append("value")
            if units_differ:
                kinds.append("unit")
            if kinds:
                value_diffs.append(
                    {
                        "entity": key[0],
                        "metric": key[1],
                        "target": key[2],
                        "condition": key[3] or None,
                        "row_context": key[6] or None,
                        "column_context": key[7] or None,
                        "value_a": ma.get("value_raw"),
                        "unit_a": ma.get("unit_raw"),
                        "operator_a": structured(ma).get("operator"),
                        "value_b": mb.get("value_raw"),
                        "unit_b": mb.get("unit_raw"),
                        "operator_b": structured(mb).get("operator"),
                        "kind": "+".join(kinds),
                    }
                )
    return {
        "keys_only_a": only_a,
        "keys_only_b": only_b,
        "paired": paired,
        "ambiguous": len(ambiguous),
        "ambiguous_candidates": ambiguous[:50],
        "value_diffs": value_diffs[:200],
        "unit_normalization_note": (
            "units compare AFTER glyph/case/space normalization (µ/μ→u); "
            "no magnitude conversion is applied (mg vs g is a reported unit difference)"
        ),
    }

@dataclass
class ComparisonService:
    repository: Repository
    artifact_root: Path
    #: tests patch this to a fake adapter factory
    engine_factory: Callable[..., Any] = staticmethod(_side_engine)
    #: E2/F04: side execution lease. A running side whose lease has expired
    #: (its process died or its heartbeat stopped) becomes claimable again —
    #: this is what makes restarts recover without double-executing live
    #: work. A live execution RENEWS its lease (heartbeat below), so a long
    #: legit run is never stolen just because its initial lease lapsed.
    lease_seconds: float = 900.0
    #: heartbeat cadence; None → lease_seconds/3 (floor 0.05s). The
    #: heartbeat is a token-conditioned UPDATE: once another worker holds
    #: the lease, renewals stop matching and the execution stops publishing.
    heartbeat_interval: float | None = None
    #: hard wall-clock budget for one side execution; exceeding it is
    #: published as an explicit failure (never a silent hang).
    execution_budget_seconds: float = 3600.0

    @classmethod
    def from_service(cls, service):
        # Daemon thread outlives the FastAPI request; the per-request
        # Repository connection would be closed before the thread writes
        # its status. Open a fresh one with check_same_thread=False so
        # the worker can also touch the connection across threads.
        artifact_root = service.paths.artifact_root
        return cls(
            repository=Repository(artifact_root / "bio.sqlite", check_same_thread=False),
            artifact_root=artifact_root,
        )

    # ---- lifecycle --------------------------------------------------------

    def create(
        self,
        *,
        document_id: str,
        source_path: Path,
        pages: list[int],
        engines: list[str],
    ) -> str:
        import uuid

        comparison_id = f"cmp-{uuid.uuid4().hex[:12]}"
        now = _now()
        conn = self.repository.connection
        conn.execute(
            """
            INSERT INTO comparisons (comparison_id, document_id, source_path,
                                     pages_json, engines_json, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            (
                comparison_id,
                document_id,
                str(source_path),
                json.dumps(sorted(pages)),
                json.dumps(list(engines)),
                now,
                now,
            ),
        )
        for engine in engines:
            conn.execute(
                """
                INSERT INTO comparison_sides (comparison_id, side, status)
                VALUES (?, ?, 'queued')
                """,
                (comparison_id, engine),
            )
        conn.commit()
        return comparison_id

    def start(self, comparison_id: str) -> None:
        """Execute every CLAIMABLE side on a daemon thread (E2/S02+S05).

        Claiming is atomic per side (lease + fenced completion), so two
        concurrent starts/retries can never run the same side twice: the
        second caller finds nothing claimable while the first lease lives.
        Expired leases (a crashed process) are claimable again.
        """

        thread = threading.Thread(
            target=self._run_sides, args=(comparison_id,), daemon=True
        )
        thread.start()

    def retry(self, comparison_id: str) -> dict[str, Any]:
        """F03/S02: an explicit retry is the ONLY path that re-arms a
        FAILED side — one atomic ``failed → queued`` flip per side per
        call, then a normal claim pass. A plain execution pass (first
        start, recovery sweep, or an older run's snapshot) can never pick
        up a 'failed' side, so the recheck's stale-snapshot sequence
        ['docling','mineru','mineru'] is impossible: after another flow
        already failed the side, its old traversal finds nothing claimable.
        A side running with a VALID lease is never disturbed either."""

        conn = self.repository.connection
        conn.execute(
            """
            UPDATE comparison_sides SET status = 'queued'
            WHERE comparison_id = ? AND status = 'failed'
            """,
            (comparison_id,),
        )
        conn.commit()
        self.start(comparison_id)
        return self.get(comparison_id)

    def recover_orphans(self) -> list[str]:
        """E2/S05 + F04/S03: orphan recovery — sides left 'running' by a
        dead process become claimable once their lease expires; 'queued'
        sides never reached by a vanished thread also restart. Called at
        boot AND periodically by the API's sweeper, so a fast restart with
        still-valid leases recovers as soon as they lapse (no retry click
        needed). 'failed' stays manual — a recovery sweep must not silently
        re-run something that legitimately errored."""

        import datetime as _dt

        now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
        rows = self.repository.connection.execute(
            """
            SELECT DISTINCT comparison_id FROM comparison_sides
            WHERE status = 'queued'
               OR (status = 'running' AND (lease_until IS NULL OR lease_until < ?))
            """,
            (now,),
        ).fetchall()
        ids = [str(row["comparison_id"]) for row in rows]
        for comparison_id in ids:
            self.start(comparison_id)
        return ids

    def get(self, comparison_id: str) -> dict[str, Any]:
        conn = self.repository.connection
        # Short-lived connection — a daemon thread may have left the
        # shared Repository connection mid-statement (sqlite3 forbids
        # two simultaneous cursors), so a fresh one here is reliable.
        import sqlite3
        fresh = sqlite3.connect(str(self.repository.database_path), check_same_thread=False)
        try:
            fresh.row_factory = sqlite3.Row
            row = fresh.execute(
                "SELECT * FROM comparisons WHERE comparison_id = ?", (comparison_id,)
            ).fetchone()
            if row is None:
                raise WorkflowError(
                    f"comparison not found: {comparison_id}",
                    details={"comparison_id": comparison_id, "code": "NOT_FOUND"},
                )
            side_rows = fresh.execute(
                "SELECT * FROM comparison_sides WHERE comparison_id = ? ORDER BY side",
                (comparison_id,),
            ).fetchall()
            sides = [
                {
                    "side": sr["side"],
                    "status": sr["status"],
                    "error": sr["error"],
                    "artifact_dir": sr["artifact_dir"],
                    "summary": json.loads(sr["summary_json"] or "{}"),
                    "started_at": sr["started_at"],
                    "ended_at": sr["ended_at"],
                    # E2: execution lifecycle visibility for the UI/tests.
                    "attempt": int(sr["attempt"]) if "attempt" in sr.keys() else 0,
                    "lease_until": (sr["lease_until"] if "lease_until" in sr.keys() else None),
                }
                for sr in side_rows
            ]
        finally:
            fresh.close()
        return {
            "comparison_id": comparison_id,
            "document_id": row["document_id"],
            "pages": json.loads(row["pages_json"] or "[]"),
            "engines": json.loads(row["engines_json"] or "[]"),
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "sides": sides,
        }

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.repository.connection.execute(
            "SELECT comparison_id FROM comparisons ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        ).fetchall()
        return [self.get(row["comparison_id"]) for row in rows]

    # ---- execution --------------------------------------------------------

    def _next_lease_token(self) -> str:
        import uuid

        return f"lease-{uuid.uuid4().hex}"

    def _claim_side(self, comparison_id: str, side: str) -> tuple[str, int] | None:
        """Atomically claim one side (E2/S02 + F03/S02). Returns
        (lease_token, attempt) or None when not claimable.

        Claimable: 'queued' (freshly created or re-queued by an explicit
        retry), or 'running' with an EXPIRED lease (a dead worker / stopped
        heartbeat). 'failed' is deliberately NOT claimable here — only
        ``retry()``'s atomic failed→queued flip re-arms a failed side, so a
        stale execution snapshot can never re-run work another flow just
        failed. The attempt counter increments per execution so each run
        writes its own immutable directory; the lease token fences the
        completion write.
        """

        import datetime as _dt

        conn = self.repository.connection
        now = _dt.datetime.now(tz=_dt.timezone.utc)
        lease_until = (now + _dt.timedelta(seconds=self.lease_seconds)).isoformat()
        token = self._next_lease_token()
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                """
                UPDATE comparison_sides
                SET status = 'running', lease_token = ?, lease_until = ?,
                    attempt = attempt + 1, started_at = ?, error = NULL
                WHERE comparison_id = ? AND side = ?
                  AND (
                        status = 'queued'
                        OR (status = 'running' AND (lease_until IS NULL OR lease_until < ?))
                  )
                """,
                (token, lease_until, _now(), comparison_id, side, now.isoformat()),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                return None
            row = conn.execute(
                "SELECT attempt FROM comparison_sides WHERE comparison_id = ? AND side = ?",
                (comparison_id, side),
            ).fetchone()
            conn.execute(
                "UPDATE comparisons SET status = 'running', updated_at = ? WHERE comparison_id = ?",
                (_now(), comparison_id),
            )
            conn.execute("COMMIT")
            return token, int(row["attempt"])
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _publish_side_result(
        self,
        comparison_id: str,
        side: str,
        lease_token: str,
        *,
        status: str,
        summary_json: str = "{}",
        artifact_dir: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Fenced completion write (E2): succeeds only while this attempt
        still owns the lease. A stale attempt (its lease was taken over by
        a recovering worker) MUST NOT publish — returns False and its
        artifacts remain as forensic records under its own attempt dir."""

        conn = self.repository.connection
        cur = conn.execute(
            """
            UPDATE comparison_sides
            SET status = ?, summary_json = ?, artifact_dir = COALESCE(?, artifact_dir),
                error = ?, ended_at = ?, lease_until = NULL
            WHERE comparison_id = ? AND side = ? AND lease_token = ?
            """,
            (status, summary_json, artifact_dir, error, _now(), comparison_id, side, lease_token),
        )
        conn.commit()
        return cur.rowcount == 1

    def _renew_lease(self, comparison_id: str, side: str, lease_token: str) -> bool:
        """F04/S04: token-conditioned heartbeat renewal. Succeeds only while
        THIS attempt still owns the lease; once a takeover happened the
        renewal stops matching (rowcount 0) and the caller must stop
        publishing. Renewals and takeover share the same lease rule."""

        import datetime as _dt

        now = _dt.datetime.now(tz=_dt.timezone.utc)
        lease_until = (now + _dt.timedelta(seconds=self.lease_seconds)).isoformat()
        conn = self.repository.connection
        cur = conn.execute(
            """
            UPDATE comparison_sides SET lease_until = ?
            WHERE comparison_id = ? AND side = ? AND lease_token = ? AND status = 'running'
            """,
            (lease_until, comparison_id, side, lease_token),
        )
        conn.commit()
        return cur.rowcount == 1

    def _claimable_sides(self, comparison_id: str) -> list[tuple[str, str]]:
        """Side names a claim could currently grab (queued / lease-expired
        running — mirroring ``_claim_side``'s predicate exactly; 'failed'
        needs an explicit retry).

        Used only as a hint for the loop; the claim SQL remains the
        authority (a hint that went stale simply fails to claim)."""

        import datetime as _dt

        now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
        rows = self.repository.connection.execute(
            """
            SELECT side FROM comparison_sides
            WHERE comparison_id = ?
              AND (
                    status = 'queued'
                    OR (status = 'running' AND (lease_until IS NULL OR lease_until < ?))
              )
            ORDER BY side
            """,
            (comparison_id, now),
        ).fetchall()
        return [str(row["side"]) for row in rows]

    def _run_sides(self, comparison_id: str) -> None:
        """Claim-and-run pass: every side is executed by exactly one live
        lease holder. A live execution keeps its lease fresh with a
        token-conditioned heartbeat (F04/S04), so a run longer than the
        initial lease is never stolen. A side that FAILS stays 'failed'
        (only an explicit retry() re-queues it — F03/S02), and lease-
        expired running sides become claimable again (restart recovery)."""

        import time

        info = self.get(comparison_id)
        pages = [int(p) for p in info["pages"]]
        source_path = Path(self._source_path_of(comparison_id))
        for engine in self._claimable_sides(comparison_id):
            claimed = self._claim_side(comparison_id, engine)
            if claimed is None:
                continue  # another worker holds a live lease
            lease_token, attempt = claimed
            started = time.monotonic()
            interval = (
                self.heartbeat_interval
                if self.heartbeat_interval is not None
                else max(0.05, self.lease_seconds / 3.0)
            )
            stop_beat = threading.Event()
            lease_lost = threading.Event()
            budget_exceeded = threading.Event()

            def _heartbeat(
                _cid: str = comparison_id,
                _side: str = engine,
                _token: str = lease_token,
                _stop: threading.Event = stop_beat,
                _lost: threading.Event = lease_lost,
                _budget: threading.Event = budget_exceeded,
                _started: float = started,
                _interval: float = interval,
            ) -> None:
                while not _stop.wait(_interval):
                    if time.monotonic() - _started > self.execution_budget_seconds:
                        _budget.set()
                    if not self._renew_lease(_cid, _side, _token):
                        _lost.set()  # lease taken over — stop publishing
                        return

            beat = threading.Thread(target=_heartbeat, daemon=True, name=f"cmp-beat-{engine}")
            beat.start()
            summary: dict[str, Any] | None = None
            error: str | None = None
            try:
                summary = self._run_one(
                    comparison_id=comparison_id,
                    engine=engine,
                    attempt=attempt,
                    document_id=str(info["document_id"]),
                    source_path=source_path,
                    pages=pages,
                )
            except Exception as exc:  # noqa: BLE001 — one side's failure is data
                error = f"{type(exc).__name__}: {exc}"[:500]
                details = getattr(exc, "details", None) or {}
                if isinstance(details, dict):
                    code = details.get("code")
                    if code and code not in error:
                        error = f"{error} [code={code}]"
            finally:
                stop_beat.set()
                beat.join(timeout=5.0)

            if error is not None:
                self._publish_side_result(
                    comparison_id, engine, lease_token, status="failed", error=error[:500]
                )
            elif budget_exceeded.is_set() and not lease_lost.is_set():
                self._publish_side_result(
                    comparison_id,
                    engine,
                    lease_token,
                    status="failed",
                    error=(
                        "WorkflowError: EXECUTION_BUDGET_EXCEEDED — side ran longer "
                        f"than the {self.execution_budget_seconds:.0f}s execution budget"
                    )[:500],
                )
            else:
                published = self._publish_side_result(
                    comparison_id,
                    engine,
                    lease_token,
                    status="done",
                    summary_json=json.dumps(summary, ensure_ascii=False),
                    artifact_dir=(summary or {}).get("artifact_dir"),
                )
                if not published:
                    # Our lease was taken over while running (heartbeat
                    # lost): keep the artifacts (attempt dir) but publish
                    # NOTHING.
                    continue
        self._refresh_status(comparison_id)

    def _source_path_of(self, comparison_id: str) -> str:
        row = self.repository.connection.execute(
            "SELECT source_path FROM comparisons WHERE comparison_id = ?",
            (comparison_id,),
        ).fetchone()
        return str(row["source_path"]) if row else ""

    def _refresh_status(self, comparison_id: str) -> None:
        rows = self.repository.connection.execute(
            "SELECT status FROM comparison_sides WHERE comparison_id = ?",
            (comparison_id,),
        ).fetchall()
        statuses = {row["status"] for row in rows}
        if statuses <= {"done"}:
            overall = "done"
        elif "running" in statuses or "queued" in statuses:
            overall = "running"
        else:
            overall = "failed"
        self.repository.connection.execute(
            "UPDATE comparisons SET status = ?, updated_at = ? WHERE comparison_id = ?",
            (overall, _now(), comparison_id),
        )
        self.repository.connection.commit()

    def _run_one(
        self,
        *,
        comparison_id: str,
        engine: str,
        document_id: str,
        source_path: Path,
        pages: list[int],
        attempt: int = 1,
    ) -> dict[str, Any]:
        """One side execution → shared domain rules → side summary.

        E2: each attempt writes into its OWN immutable directory
        (``comparisons/<cid>/<side>/attempt-<n>/``), so a recovering
        worker's run never overwrites the previous attempt's artifacts —
        both remain readable evidence even when the older attempt's lease
        expired.
        """

        from bio_pdf.parsers.base import ParseRequest

        side_dir = (
            self.artifact_root / "comparisons" / comparison_id / engine / f"attempt-{attempt}"
        )
        side_dir.mkdir(parents=True, exist_ok=True)
        request = ParseRequest(
            job_id=f"{comparison_id}:{engine}",
            document_id=document_id,
            source_path=source_path,
            page_numbers=tuple(pages),
            profile="layout_native",  # type: ignore[arg-type]
            options_ref="configs/demo.toml",
            output_dir=side_dir,
            parser_options={},
        )
        # Resolve the engine factory at call time so tests can monkeypatch
        # ``comparison_service._side_engine`` (staticmethods freeze the
        # class-attribute reference, hence the indirection).
        from bio_pdf.application import comparison_service as _self
        adapter = _self._side_engine(engine, source_path, request)
        import inspect
        sig = inspect.signature(adapter.parse)
        kwargs = {}
        if "local_file" in sig.parameters:
            kwargs["local_file"] = source_path
        artifacts = adapter.parse(request, **kwargs)

        normalized = json.loads(artifacts.normalized_path.read_text(encoding="utf-8"))
        summary = self._domain_pass(normalized, document_id=document_id, engine=engine)
        summary["artifact_dir"] = str(side_dir.relative_to(self.artifact_root))
        summary["manifest"] = str(artifacts.manifest_path.relative_to(self.artifact_root))
        summary["attempt"] = attempt
        return summary

    def _domain_pass(
        self, normalized: dict[str, Any], *, document_id: str, engine: str
    ) -> dict[str, Any]:
        """The SHARED rules: ST.25 parse + inventory + table measurements.

        Entities/relations and evidence publishing are intentionally absent —
        the diff payload states this so a reader never assumes full-chain
        equivalence.
        """

        from bio_pdf.contracts import NormalizedDocument
        from bio_pdf.extract.activities.table_classifier import TableKind, classify_table
        from bio_pdf.extract.activities.tables import extract_table_measurements
        from bio_pdf.extract.evidence import EvidenceIndex, span_from_cell
        from bio_pdf.extract.sequences.inventory import build_sequence_inventory
        from bio_pdf.extract.sequences.st25 import parse_st25_blocks_with_scan
        from bio_pdf.workflow.handlers import _page_blocks, _table_rows

        document = NormalizedDocument.model_validate(normalized)
        blocks = _page_blocks(document)
        sequences, scan = parse_st25_blocks_with_scan(blocks, document_id)
        inventory = build_sequence_inventory(
            scan,
            requested_pages=[p.page_number for p in document.pages],
            document_page_count=None,
        )

        measurements: list[dict[str, Any]] = []
        evidence = EvidenceIndex()
        for table, _legacy in _table_rows(document):
            if getattr(table, "source", "parser") == "geometry_reconstructed":
                # Same rule as the extract stage: a rebuilt table feeds
                # extraction but is marked as reconstructed in the summary.
                pass
            kind = classify_table(table)
            if kind in (
                TableKind.INVENTORY,
                TableKind.SCORING,
                TableKind.UNCLASSIFIED,
                TableKind.SEQUENCE,
                TableKind.ASSAY_CONDITIONS,
            ):
                continue
            cell_evidence: dict[str, str] = {}
            for cell in table.cells:
                if not cell.text_raw:
                    continue
                span = span_from_cell(
                    document_id=document_id,
                    parser_run_id=f"{engine}-cmp",
                    page_number=table.page_number,
                    table_id=table.table_id,
                    cell_id=cell.cell_id,
                    text_raw=cell.text_raw,
                    bbox=cell.bbox,
                )
                evidence.add(span)
                cell_evidence[cell.cell_id] = span.evidence_id
            # E7/P03 + F06/P02: a measurement's pairing must carry WHERE it
            # came from. Two results that differ only in an experiment
            # condition (different row: time/dose/sample) share
            # entity/metric/target; without context the diff would pair
            # arbitrary candidates and invent "field differences". The
            # pairing identity uses the SEMANTIC table anchor (caption /
            # header signature — local table ids are engine-private and
            # only travel as evidence references), the full structured
            # conditions, and the row/column context.
            table_anchor = _table_anchor(table)
            cells_by_id = {cell.cell_id: cell for cell in table.cells}
            row_contexts: dict[int, str] = {}
            col_contexts: dict[int, str] = {}
            for cell in table.cells:
                if not cell.text_raw:
                    continue
                row_contexts.setdefault(cell.row_index, cell.text_raw)
                col_contexts.setdefault(cell.column_index, cell.text_raw)

            for record in extract_table_measurements(
                table, document_id=document_id, evidences=cell_evidence, evidence_kind="undetermined"
            ):
                anchor_row: int | None = None
                anchor_col: int | None = None
                for eid in record.evidence_ids:
                    for cell_id, mapped in cell_evidence.items():
                        if mapped == eid:
                            cell = cells_by_id.get(cell_id)
                            if cell is not None:
                                anchor_row = cell.row_index
                                anchor_col = cell.column_index
                            break
                    if anchor_row is not None:
                        break
                measurements.append(
                    {
                        "entity": record.entity_raw,
                        "metric": record.metric,
                        "target": record.target_raw,
                        "value_raw": record.value.raw,
                        # F06/P02: the STRUCTURED value (kind/operator/
                        # numbers/qualifier) is what gets compared; the raw
                        # string stays for display.
                        "value_structured": record.value.model_dump(mode="json"),
                        "unit_raw": record.unit_raw,
                        "unit_normalized": record.unit_normalized,
                        # F06/P02: engine-local id = evidence reference only.
                        "table_id": table.table_id,
                        "table_anchor": table_anchor,
                        "page_number": table.page_number,
                        # F06/P02: the full structured experiment conditions
                        # (method/time/dose…), not just the first row text.
                        "conditions": [
                            {str(k): str(v) for k, v in cond.items()}
                            for cond in (getattr(record, "conditions", None) or [])
                        ],
                        "row_context": row_contexts.get(anchor_row) if anchor_row is not None else None,
                        "column_context": col_contexts.get(anchor_col) if anchor_col is not None else None,
                    }
                )

        return {
            "engine": engine,
            "pages_produced": sorted(p.page_number for p in document.pages),
            "page_count": len(document.pages),
            "errors": list(document.errors),
            "sequences": [
                {
                    "seq_id_no": record.seq_id_no,
                    "molecule_type": record.molecule_type,
                    "declared_length": record.declared_length,
                    "observed_length": record.observed_length,
                    "normalized_sequence": record.normalized_sequence,
                }
                for record in sequences
                if record.seq_id_no is not None
            ],
            "inventory_summary": inventory["summary"],
            "measurements": measurements,
            "scope_note": (
                "sequence + inventory + table measurements under the shared rules; "
                "entities/relations and evidence publishing are out of scope here"
            ),
        }

    # ---- diff -------------------------------------------------------------

    def diff(self, comparison_id: str) -> dict[str, Any]:
        info = self.get(comparison_id)
        sides = {side["side"]: side for side in info["sides"]}
        done = {name: side for name, side in sides.items() if side["status"] == "done"}
        if len(done) < 2:
            raise WorkflowError(
                "both sides must be done before a diff can be computed",
                details={
                    "comparison_id": comparison_id,
                    "side_statuses": {name: s["status"] for name, s in sides.items()},
                    "code": "COMPARISON_NOT_READY",
                },
            )
        names = sorted(done)
        a_name, b_name = names[0], names[1]
        a, b = done[a_name]["summary"], done[b_name]["summary"]

        requested = sorted(int(p) for p in info["pages"])
        pages = {
            "requested": requested,
            a_name: {"produced": a["pages_produced"], "missing": sorted(set(requested) - set(a["pages_produced"]))},
            b_name: {"produced": b["pages_produced"], "missing": sorted(set(requested) - set(b["pages_produced"]))},
        }

        seq_a = {str(s["seq_id_no"]): s for s in a["sequences"]}
        seq_b = {str(s["seq_id_no"]): s for s in b["sequences"]}
        matched, only_a, only_b, diffs = [], [], [], []
        for seq_no in sorted(set(seq_a) | set(seq_b), key=lambda x: (len(x), x)):
            in_a, in_b = seq_no in seq_a, seq_no in seq_b
            if in_a and in_b:
                sa, sb = seq_a[seq_no], seq_b[seq_no]
                matched.append(seq_no)
                if sa["normalized_sequence"] != sb["normalized_sequence"]:
                    diff_index = next(
                        (
                            i
                            for i, (ca, cb) in enumerate(
                                zip(sa["normalized_sequence"], sb["normalized_sequence"])
                            )
                            if ca != cb
                        ),
                        min(len(sa["normalized_sequence"]), len(sb["normalized_sequence"])),
                    )
                    diffs.append(
                        {
                            "seq_id_no": seq_no,
                            "length_a": sa["observed_length"],
                            "length_b": sb["observed_length"],
                            "first_difference_index": diff_index,
                        }
                    )
            elif in_a:
                only_a.append(seq_no)
            else:
                only_b.append(seq_no)

        # F01/S05 + F06/P02: ONE pairing implementation (module-level
        # pair_measurements, unit-tested): semantic identity without local
        # table ids, group-based (ambiguity reported, never force-paired),
        # value/unit/operator compared structurally.
        measurements = pair_measurements(a["measurements"], b["measurements"])

        return {
            "comparison_id": comparison_id,
            "document_id": info["document_id"],
            "engines": {a_name: a_name, b_name: b_name},
            "pages": pages,
            "sequences": {
                "matched": matched,
                "only_a": only_a,
                "only_b": only_b,
                "field_diffs": diffs[:200],
                "count_a": len(a["sequences"]),
                "count_b": len(b["sequences"]),
            },
            "inventory": {
                a_name: a["inventory_summary"],
                b_name: b["inventory_summary"],
            },
            "measurements": measurements,
            "evaluation": {
                "gold": None,
                "status": "not_evaluated",
                "note": "no verified human gold exists; agreement is not accuracy",
            },
        }