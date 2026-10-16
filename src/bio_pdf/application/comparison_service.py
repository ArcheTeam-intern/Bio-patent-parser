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
        """G08/P03: SI case is SEMANTIC (µM ≠ µm, M ≠ mM).

        Normalization merges only EQUIVALENT GLYPHS and explicit aliases —
        the micro sign µ (U+00B5) and Greek mu μ (U+03BC) become ``u``, the
        spelled-out ``mcg`` becomes ``ug``, and whitespace is formatting.
        Case is preserved; magnitude conversion is NEVER applied here (a
        mg-vs-g or mM-vs-µM difference is reported, not silently unified).
        """

        import re as _re

        text = (u or "").strip().replace(" ", "")
        text = text.replace("µ", "u").replace("μ", "u")
        text = _re.sub("mcg", "ug", text, flags=_re.IGNORECASE)
        return text

    #: H06/P03: only these fields are EXPERIMENT SEMANTICS. Evidence ids,
    #: block ids, source labels and engine names differ between engines for
    #: the very same condition — including them in the key made identical
    #: 1 h conditions unpairable. They stay in the payload for traceability.
    _CONDITION_SEMANTIC_KEYS = ("kind", "value", "unit", "operator")

    def conditions_key(m: dict[str, Any]) -> tuple:
        items = []
        for cond in m.get("conditions") or []:
            if isinstance(cond, dict):
                semantic = tuple(
                    sorted(
                        (key, norm_text(cond.get(key)))
                        for key in _CONDITION_SEMANTIC_KEYS
                        if cond.get(key) is not None
                    )
                )
                items.append(semantic)
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
    pairs_with_unverified_conditions = 0
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
            # G07/P02: both sides "unverified" = conditions were never
            # established, NOT confirmed-identical experiments.
            if (
                str(ma.get("conditions_status") or "unverified") == "unverified"
                and str(mb.get("conditions_status") or "unverified") == "unverified"
            ):
                pairs_with_unverified_conditions += 1
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
        "pairs_with_unverified_conditions": pairs_with_unverified_conditions,
        "conditions_note": (
            "paired rows whose conditions could not be established on BOTH sides are counted "
            "as 'conditions unverified', never displayed as experimentally identical"
        ),
        "unit_normalization_note": (
            "units compare AFTER equivalent-glyph/alias normalization only (µ/μ→u, mcg→ug); "
            "SI case is significant (µM ≠ µm, M ≠ mM) and no magnitude conversion is "
            "applied — mg vs g, mM vs µM are REPORTED unit differences, never unified"
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
    #: F04: hidden wall-clock budget for one side execution. G03 makes it
    #: REAL: the execution unit is supervised (a child process by default)
    #: and is terminated at budget expiry — the side goes to a fenced
    #: 'failed', renewals stop, and a late child can never publish.
    execution_budget_seconds: float = 3600.0
    #: G03/S03: how a side executes. "process" (default) runs the side in a
    #: spawn-ed child that CAN be terminated on budget expiry; "thread"
    #: keeps an in-process thread (used by stub-based tests, where a child
    #: process could not see the patched engine factory). Both modes share
    #: the same watchdog: the parent owns the completion write, so a late
    #: unit can never publish.
    run_mode: str = "process"

    @classmethod
    def from_service(cls, service):
        # G02/S02: the service no longer does its own DB work on the
        # Repository's long-lived connection — every operation opens its
        # OWN short-lived connection (see ``_connect``), so a heartbeat in
        # one thread can never commit a claim transaction that belongs to
        # another thread. The Repository stays as the path/config carrier;
        # its connection is for tests/inspection in the constructing thread.
        artifact_root = service.paths.artifact_root
        # G03: production runs supervise sides in a terminable child
        # process; the env override exists for stub-based tests whose
        # patched engine factory cannot cross a process boundary.
        import os

        run_mode = os.environ.get("BIO_PDF_COMPARISON_RUN_MODE", "process")
        return cls(
            repository=Repository(artifact_root / "bio.sqlite", check_same_thread=False),
            artifact_root=artifact_root,
            run_mode=run_mode if run_mode in ("process", "thread") else "process",
        )

    def _connect(self):
        """G02/S02: one connection PER OPERATION.

        ``check_same_thread=False`` permits cross-thread ATTRIBUTE access; it
        does not isolate TRANSACTIONS. Sharing one connection let a
        heartbeat's commit swallow another thread's open claim transaction
        (the recheck reproduced 'cannot rollback - no transaction is
        active' with a running side that never executed). With a fresh
        connection per operation, every transaction's lifetime is confined
        to the thread that opened it, and SQLite's own locking (busy
        timeout) serializes writers."""

        import sqlite3

        conn = sqlite3.connect(str(self.repository.database_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

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
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
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
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
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

        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE comparison_sides SET status = 'queued'
                WHERE comparison_id = ? AND status = 'failed'
                """,
                (comparison_id,),
            )
            conn.commit()
        finally:
            conn.close()
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
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT comparison_id FROM comparison_sides
                WHERE status = 'queued'
                   OR (status = 'running' AND (lease_until IS NULL OR lease_until < ?))
                """,
                (now,),
            ).fetchall()
        finally:
            conn.close()
        ids = [str(row["comparison_id"]) for row in rows]
        for comparison_id in ids:
            self.start(comparison_id)
        return ids

    def get(self, comparison_id: str) -> dict[str, Any]:
        fresh = self._connect()
        try:
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
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT comparison_id FROM comparisons ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        finally:
            conn.close()
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

        now = _dt.datetime.now(tz=_dt.timezone.utc)
        lease_until = (now + _dt.timedelta(seconds=self.lease_seconds)).isoformat()
        token = self._next_lease_token()
        # G02/S02: this transaction lives on ITS OWN connection — no other
        # thread can commit or roll it back through a shared handle.
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
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
                conn.rollback()
                return None
            row = conn.execute(
                "SELECT attempt FROM comparison_sides WHERE comparison_id = ? AND side = ?",
                (comparison_id, side),
            ).fetchone()
            conn.execute(
                "UPDATE comparisons SET status = 'running', updated_at = ? WHERE comparison_id = ?",
                (_now(), comparison_id),
            )
            conn.commit()
            return token, int(row["attempt"])
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

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

        conn = self._connect()
        try:
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
        finally:
            conn.close()

    def _renew_lease(self, comparison_id: str, side: str, lease_token: str) -> bool:
        """F04/S04: token-conditioned heartbeat renewal. Succeeds only while
        THIS attempt still owns the lease; once a takeover happened the
        renewal stops matching (rowcount 0) and the caller must stop
        publishing. Renewals and takeover share the same lease rule."""

        import datetime as _dt

        now = _dt.datetime.now(tz=_dt.timezone.utc)
        lease_until = (now + _dt.timedelta(seconds=self.lease_seconds)).isoformat()
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                UPDATE comparison_sides SET lease_until = ?
                WHERE comparison_id = ? AND side = ? AND lease_token = ? AND status = 'running'
                """,
                (lease_until, comparison_id, side, lease_token),
            )
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()

    def _claimable_sides(self, comparison_id: str) -> list[tuple[str, str]]:
        """Side names a claim could currently grab (queued / lease-expired
        running — mirroring ``_claim_side``'s predicate exactly; 'failed'
        needs an explicit retry).

        Used only as a hint for the loop; the claim SQL remains the
        authority (a hint that went stale simply fails to claim)."""

        import datetime as _dt

        now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
        conn = self._connect()
        try:
            rows = conn.execute(
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
        finally:
            conn.close()
        return [str(row["side"]) for row in rows]

    def _run_sides(self, comparison_id: str) -> None:
        """Claim-and-run pass: every side is executed by exactly one live
        lease holder. The execution runs as a SUPERVISED unit (G03): the
        watchdog waits at most ``execution_budget_seconds``, then fences
        the side failed and terminates the unit. A side that FAILS stays
        'failed' (only an explicit retry() re-queues it — F03/S02), and
        lease-expired running sides become claimable again."""

        info = self.get(comparison_id)
        pages = [int(p) for p in info["pages"]]
        source_path = Path(self._source_path_of(comparison_id))
        for engine in self._claimable_sides(comparison_id):
            claimed = self._claim_side(comparison_id, engine)
            if claimed is None:
                continue  # another worker holds a live lease
            lease_token, attempt = claimed
            interval = (
                self.heartbeat_interval
                if self.heartbeat_interval is not None
                else max(0.05, self.lease_seconds / 3.0)
            )
            stop_beat = threading.Event()
            lease_lost = threading.Event()

            def _heartbeat(
                _cid: str = comparison_id,
                _side: str = engine,
                _token: str = lease_token,
                _stop: threading.Event = stop_beat,
                _lost: threading.Event = lease_lost,
                _interval: float = interval,
            ) -> None:
                while not _stop.wait(_interval):
                    if not self._renew_lease(_cid, _side, _token):
                        _lost.set()  # lease taken over — stop publishing
                        return

            beat = threading.Thread(target=_heartbeat, daemon=True, name=f"cmp-beat-{engine}")
            beat.start()

            # G05/S05: the remote (cloud) identity lives at the SIDE level —
            # one directory per (comparison, engine) shared across attempts,
            # so a retry after a download failure polls the SAME batch
            # instead of re-uploading.
            remote_state_dir = (
                self.artifact_root / "comparisons" / comparison_id / engine / "remote"
            )
            spec = {
                "comparison_id": comparison_id,
                "engine": engine,
                "attempt": attempt,
                "document_id": str(info["document_id"]),
                "source_path": str(source_path),
                "pages": pages,
                "artifact_root": str(self.artifact_root),
                "remote_state_dir": str(remote_state_dir),
            }
            kind, summary, error = self._execute_governed(
                spec,
                budget=float(self.execution_budget_seconds),
                lease_lost=lease_lost,
            )
            stop_beat.set()
            beat.join(timeout=5.0)

            if kind == "aborted":
                # H01/S01: the lease was taken while running — the tree is
                # already cleaned; publish NOTHING (the new holder owns the
                # state and a fenced write would be refused anyway).
                continue
            if kind == "timeout":
                # Budget expiry: renewals have stopped (heartbeat is off)
                # and the unit is gone/fenced; the completion write makes
                # the terminal state explicit exactly once.
                self._publish_side_result(
                    comparison_id,
                    engine,
                    lease_token,
                    status="failed",
                    error=(
                        "WorkflowError: EXECUTION_BUDGET_EXCEEDED — side did not finish "
                        f"within {self.execution_budget_seconds:.0f}s; execution unit "
                        "terminated, remote identity kept for a later attempt"
                    )[:500],
                )
            elif error is not None:
                self._publish_side_result(
                    comparison_id, engine, lease_token, status="failed", error=error[:500]
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

    def _execute_governed(
        self,
        spec: dict[str, Any],
        *,
        budget: float,
        lease_lost: "threading.Event | None" = None,
    ) -> tuple[str, dict[str, Any] | None, str | None]:
        """Run one side under the budget watchdog (G03+H01).

        Returns ``("done", summary, None)`` | ``("error", None, text)`` |
        ``("timeout", None, None)`` | ``("aborted", None, None)``.

        H01: the execution UNIT is the whole process TREE — on timeout and
        on lease loss the tree (including the engine's own inference child)
        is terminated, and every live tree is swept at interpreter shutdown.
        A late unit result can never publish: only the caller writes status.
        """

        if self.run_mode == "thread":
            box: dict[str, Any] = {}

            def _work() -> None:
                try:
                    box["summary"] = _execute_side_spec(spec)
                except BaseException as exc:  # noqa: BLE001 — failure is data
                    box["error"] = _side_error_text(exc)

            worker = threading.Thread(target=_work, daemon=True, name=f"cmp-side-{spec['engine']}")
            worker.start()
            worker.join(timeout=max(0.1, budget))
            if worker.is_alive():
                return "timeout", None, None
            if "error" in box:
                return "error", None, str(box["error"])
            return "done", box.get("summary"), None

        import multiprocessing as mp

        ctx = mp.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=False)
        proc = ctx.Process(
            target=_side_process_entry,
            args=(spec, child_conn),
            # NOT daemonic: the engines themselves spawn helper processes
            # (docling runs its inference in a supervised child); a daemonic
            # process is not allowed to have children.
            daemon=False,
            name=f"cmp-side-{spec['engine']}",
        )
        try:
            proc.start()
        except Exception as exc:  # noqa: BLE001 — spawning is environment-dependent
            try:
                parent_conn.close()
                child_conn.close()
            except Exception:  # noqa: BLE001
                pass
            return "error", None, _side_error_text(exc)
        child_conn.close()
        _register_live_child(int(proc.pid or 0), str(spec.get("engine") or ""))
        try:
            # H01: poll in short slices so a lease loss aborts (and cleans)
            # the tree mid-flight instead of running to the budget.
            import time as _time

            deadline = _time.monotonic() + max(0.1, float(budget))
            completed = False
            while _time.monotonic() < deadline:
                if parent_conn.poll(timeout=min(0.5, max(0.05, deadline - _time.monotonic()))):
                    completed = True
                    break
                if lease_lost is not None and lease_lost.is_set():
                    _kill_process_tree(int(proc.pid or 0))
                    proc.join(timeout=10)
                    return "aborted", None, None
            if completed:
                try:
                    message = parent_conn.recv()
                except EOFError:
                    message = {
                        "ok": False,
                        "error": "side process exited without reporting a result",
                    }
                proc.join(timeout=10)
                try:
                    parent_conn.close()
                except Exception:  # noqa: BLE001 — closing a dead pipe is best-effort
                    pass
                if isinstance(message, dict) and message.get("ok"):
                    return "done", message.get("summary"), None
                return (
                    "error",
                    None,
                    str((message or {}).get("error") or "side process failed without a reason"),
                )
        finally:
            _unregister_live_child(int(proc.pid or 0))

        # Budget exceeded: terminate the WHOLE TREE — the middle process AND
        # the engine's own inference child (H01) — then report the timeout;
        # the caller stops renewals and fences the side failed.
        _kill_process_tree(int(proc.pid or 0))
        proc.join(timeout=10)
        try:
            parent_conn.close()
        except Exception:  # noqa: BLE001
            pass
        return "timeout", None, None

    def _source_path_of(self, comparison_id: str) -> str:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT source_path FROM comparisons WHERE comparison_id = ?",
                (comparison_id,),
            ).fetchone()
        finally:
            conn.close()
        return str(row["source_path"]) if row else ""

    def _refresh_status(self, comparison_id: str) -> None:
        """G04/S04: derive AND write the overall status in ONE statement.

        The old code read the side statuses and wrote the derived value in a
        separate statement; during that gap another flow could finish a side
        and write 'done', after which the stale reader wrote 'running' back —
        both sides done, overall stuck running forever. Deriving the value
        inside the UPDATE removes the read-write gap entirely: whichever
        statement runs last derives from the CURRENT side rows."""

        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE comparisons SET
                    status = (
                        SELECT CASE
                            WHEN COALESCE(SUM(CASE WHEN cs.status != 'done' THEN 1 ELSE 0 END), 0) = 0
                                THEN 'done'
                            WHEN SUM(CASE WHEN cs.status IN ('running', 'queued') THEN 1 ELSE 0 END) > 0
                                THEN 'running'
                            ELSE 'failed'
                        END
                        FROM comparison_sides cs
                        WHERE cs.comparison_id = comparisons.comparison_id
                    ),
                    updated_at = ?
                WHERE comparison_id = ?
                """,
                (_now(), comparison_id),
            )
            conn.commit()
        finally:
            conn.close()

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

        E2/G03: each attempt writes into its OWN immutable directory
        (``comparisons/<cid>/<side>/attempt-<n>/``); this method is the
        thin in-process wrapper over the module-level
        :func:`_execute_side_spec`, which is also what the supervised child
        process runs.
        """

        spec = {
            "comparison_id": comparison_id,
            "engine": engine,
            "attempt": attempt,
            "document_id": document_id,
            "source_path": str(source_path),
            "pages": list(pages),
            "artifact_root": str(self.artifact_root),
            # G05/S05: side-level remote identity dir (shared across attempts).
            "remote_state_dir": str(
                self.artifact_root / "comparisons" / comparison_id / engine / "remote"
            ),
        }
        return _execute_side_spec(spec)

    def _domain_pass(
        self, normalized: dict[str, Any], *, document_id: str, engine: str
    ) -> dict[str, Any]:
        """The SHARED rules: ST.25 parse + inventory + table measurements.

        Entities/relations and evidence publishing are intentionally absent —
        the diff payload states this so a reader never assumes full-chain
        equivalence.
        """

        return _domain_pass_spec(normalized, document_id=document_id, engine=engine)


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


#: H01/S01: live comparison child PIDs → engine label. Registered while a
#: supervised side runs; the tree-kill sweeps them on timeout, lease loss
#: and interpreter shutdown (atexit).
_LIVE_SIDE_CHILDREN: dict[int, str] = {}
_LIVE_SIDE_LOCK = __import__("threading").Lock()


def _kill_process_tree(pid: int) -> None:
    """Terminate a process AND its descendants (H01/S01).

    ``Process.terminate()`` kills only the middle process: the chain here
    is comparison thread → spawn child → engine inference child (Docling
    runs its converter in its own subprocess), and the grandchild would
    keep burning CPU after the budget fired. ``taskkill /T`` walks the
    live tree from the given PID — so it must run BEFORE the middle
    process is gone."""

    if pid <= 0:
        return
    import subprocess as _sp
    import sys as _sys

    try:
        if _sys.platform == "win32":
            _sp.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=20,
            )
        else:
            _sp.run(["pkill", "-TERM", "-P", str(pid)], capture_output=True, timeout=20)
            _sp.run(["kill", "-TERM", str(pid)], capture_output=True, timeout=20)
    except Exception:  # noqa: BLE001 — best-effort cleanup, never fatal
        pass


def _register_live_child(pid: int, engine: str) -> None:
    with _LIVE_SIDE_LOCK:
        _LIVE_SIDE_CHILDREN[pid] = engine


def _unregister_live_child(pid: int) -> None:
    with _LIVE_SIDE_LOCK:
        _LIVE_SIDE_CHILDREN.pop(pid, None)


def _kill_all_live_children() -> None:
    """atexit: service shutdown cleans the same execution scope."""

    with _LIVE_SIDE_LOCK:
        pids = list(_LIVE_SIDE_CHILDREN)
        _LIVE_SIDE_CHILDREN.clear()
    for pid in pids:
        _kill_process_tree(pid)


import atexit as _atexit

_atexit.register(_kill_all_live_children)


def _side_error_text(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"[:500]
    details = getattr(exc, "details", None) or {}
    if isinstance(details, dict):
        code = details.get("code")
        if code and code not in text:
            text = f"{text} [code={code}]"
    return text


def _execute_side_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """G03/S03: run ONE comparison side to completion — no DB, no service.

    Module-level so the supervised child process (spawn) can execute it;
    resolves ``_side_engine`` at call time so in-process tests that patch
    the factory keep working in thread mode.
    """

    from bio_pdf.parsers.base import ParseRequest

    engine = str(spec["engine"])
    artifact_root = Path(str(spec["artifact_root"]))
    attempt = int(spec.get("attempt") or 1)
    source_path = Path(str(spec["source_path"]))
    side_dir = artifact_root / "comparisons" / str(spec["comparison_id"]) / engine / f"attempt-{attempt}"
    side_dir.mkdir(parents=True, exist_ok=True)
    request = ParseRequest(
        job_id=f"{spec['comparison_id']}:{engine}",
        document_id=str(spec["document_id"]),
        source_path=source_path,
        page_numbers=tuple(int(p) for p in spec["pages"]),
        profile="layout_native",  # type: ignore[arg-type]
        options_ref="configs/demo.toml",
        output_dir=side_dir,
        parser_options={},
        # G05/S05: cloud identity state lives at the side level, shared
        # across attempts; when absent the adapter keeps its legacy search.
        remote_state_dir=str(spec["remote_state_dir"]) if spec.get("remote_state_dir") else None,
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
    summary = _domain_pass_spec(
        normalized, document_id=str(spec["document_id"]), engine=engine
    )
    summary["artifact_dir"] = str(side_dir.relative_to(artifact_root))
    summary["manifest"] = str(artifacts.manifest_path.relative_to(artifact_root))
    summary["attempt"] = attempt
    return summary


def _side_process_entry(spec: dict[str, Any], conn: Any) -> None:
    """G03/S03: child-process entry for one supervised side execution."""

    try:
        summary = _execute_side_spec(spec)
        conn.send({"ok": True, "summary": summary})
    except BaseException as exc:  # noqa: BLE001 — report any child failure
        try:
            conn.send({"ok": False, "error": _side_error_text(exc)})
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _domain_pass_spec(
    normalized: dict[str, Any], *, document_id: str, engine: str
) -> dict[str, Any]:
    """Module-level domain pass (shared by the service method and the child)."""

    from bio_pdf.contracts import NormalizedDocument
    from bio_pdf.extract.activities.table_classifier import TableKind, classify_table
    from bio_pdf.extract.activities.tables import extract_table_measurements
    from bio_pdf.extract.evidence import EvidenceIndex, span_from_cell
    from bio_pdf.extract.sequences.inventory import build_sequence_inventory
    from bio_pdf.extract.sequences.st25 import parse_st25_blocks_with_scan
    from bio_pdf.workflow.handlers import (
        _page_blocks,
        _table_context_conditions as table_context_conditions,
        _table_rows,
    )

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
        # G11/P07: a distorted grid is skipped exactly like production —
        # never diffed as if it were a healthy matrix.
        if any(
            str(issue).startswith(
                ("GRID_SIZE_SUSPECT", "RAGGED_ROWS_SEVERE", "HEADER_SEMANTICS_BROKEN")
            )
            for issue in table.structure_issues
        ):
            continue
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
                # G09/P05: unverified table frame → page-level cell evidence.
                bbox=(
                    None
                    if getattr(table, "bbox_verified", None) is False
                    else cell.bbox
                ),
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
            table,
            document_id=document_id,
            evidences=cell_evidence,
            evidence_kind="undetermined",
            # G07/P02: the production chain resolves caption/note conditions
            # here too — the diff must see the REAL conditions of the table,
            # not a hand-filled field.
            context_conditions=table_context_conditions(table, document),
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
                    # G07/P02: an EMPTY conditions list is not evidence of an
                    # identical experiment setup — it is "未核对" and the
                    # pairing output says so instead of implying equality.
                    "conditions_status": (
                        "extracted"
                        if (getattr(record, "conditions", None) or [])
                        else "unverified"
                    ),
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
