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



def pair_measurements(ms_a: list[dict[str, Any]], ms_b: list[dict[str, Any]]) -> dict[str, Any]:
    """E7/P03 candidate pairing: full-identity key (no value), group based,
    order-invariant; only unique-on-both sides are compared; values compare
    semantically (Decimal + normalized unit)."""

    from collections import defaultdict
    from decimal import Decimal, InvalidOperation

    def measurement_key(m: dict[str, Any]) -> tuple:
        return (
            str(m.get("entity") or ""),
            str(m.get("metric") or ""),
            str(m.get("target") or ""),
            str(m.get("row_context") or ""),
            str(m.get("column_context") or ""),
            str(m.get("table_id") or ""),
            int(m.get("page_number") or 0),
        )

    def group(ms: list[dict[str, Any]]) -> dict[tuple, list[dict[str, Any]]]:
        out: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
        for m in ms:
            out[measurement_key(m)].append(m)
        return out

    def norm_unit(u: str | None) -> str:
        return (u or "").strip().lower().replace("µ", "u").replace(" ", "")

    def num(value: str | None) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            return None

    ga, gb = group(ms_a), group(ms_b)
    keys = set(ga) | set(gb)
    paired = 0
    ambiguous: list[dict[str, Any]] = []
    value_diffs: list[dict[str, Any]] = []
    only_a: list[list[str]] = []
    only_b: list[list[str]] = []
    for key in sorted(keys):
        ca, cb = ga.get(key, []), gb.get(key, [])
        if len(ca) > 1 or len(cb) > 1:
            ambiguous.append(
                {
                    "key": list(key),
                    "candidates_a": [m.get("value_raw") for m in ca],
                    "candidates_b": [m.get("value_raw") for m in cb],
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
            na, nb = num(ma.get("value_raw")), num(mb.get("value_raw"))
            if na is not None and nb is not None:
                values_differ = na != nb
            else:
                values_differ = str(ma.get("value_raw") or "") != str(mb.get("value_raw") or "")
            units_differ = norm_unit(ma.get("unit_normalized") or ma.get("unit_raw")) != norm_unit(
                mb.get("unit_normalized") or mb.get("unit_raw")
            )
            if values_differ or units_differ:
                value_diffs.append(
                    {
                        "entity": key[0],
                        "metric": key[1],
                        "target": key[2],
                        "condition": key[3] or key[4] or None,
                        "value_a": ma.get("value_raw"),
                        "unit_a": ma.get("unit_raw"),
                        "value_b": mb.get("value_raw"),
                        "unit_b": mb.get("unit_raw"),
                        "kind": "value" if values_differ else "unit",
                    }
                )
    return {
        "keys_only_a": only_a,
        "keys_only_b": only_b,
        "paired": paired,
        "ambiguous": len(ambiguous),
        "ambiguous_candidates": ambiguous[:50],
        "value_diffs": value_diffs[:200],
    }

@dataclass
class ComparisonService:
    repository: Repository
    artifact_root: Path
    #: tests patch this to a fake adapter factory
    engine_factory: Callable[..., Any] = staticmethod(_side_engine)
    #: E2: side execution lease. A running side whose lease has expired
    #: (its process died) becomes claimable again — this is what makes
    #: restarts recover without double-executing live work.
    lease_seconds: float = 900.0

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
        """E2: retry just re-arms execution — the claim SQL decides what is
        actually runnable (failed, queued, or lease-expired running). No
        bulk status rewrite, so a side running with a VALID lease is never
        re-executed by a retry from another thread or process."""

        self.start(comparison_id)
        return self.get(comparison_id)

    def recover_orphans(self) -> list[str]:
        """E2/S05: restart recovery — sides left 'running' by a dead
        process have expired leases and become claimable; 'queued' sides
        never reached by a vanished thread also restart. 'failed' stays
        manual (retry button) — a restart must not silently re-run
        something that legitimately errored."""

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
        """Atomically claim one side (E2/S02). Returns (lease_token,
        attempt) or None when not claimable.

        Claimable: queued, failed, or running with an EXPIRED lease (a dead
        worker). The attempt counter increments per execution so each run
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
                        status IN ('queued', 'failed')
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

    def _claimable_sides(self, comparison_id: str) -> list[tuple[str, str]]:
        """Side names a claim could currently grab (queued/failed/expired).

        Used only as a hint for the loop; the claim SQL remains the
        authority (a hint that went stale simply fails to claim)."""

        import datetime as _dt

        now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
        rows = self.repository.connection.execute(
            """
            SELECT side FROM comparison_sides
            WHERE comparison_id = ?
              AND (
                    status IN ('queued', 'failed')
                    OR (status = 'running' AND (lease_until IS NULL OR lease_until < ?))
              )
            ORDER BY side
            """,
            (comparison_id, now),
        ).fetchall()
        return [str(row["side"]) for row in rows]

    def _run_sides(self, comparison_id: str) -> None:
        """Claim-and-run pass: every side is executed by exactly one live
        lease holder; restart/recover needs no special path (expired leases
        simply become claimable again on the next start/retry)."""

        info = self.get(comparison_id)
        pages = [int(p) for p in info["pages"]]
        source_path = Path(self._source_path_of(comparison_id))
        # ONE pass over the claimable snapshot: a side that FAILS in this
        # run stays 'failed' (a fresh retry() re-arms execution) — no
        # self-spinning over the same failure.
        for engine in self._claimable_sides(comparison_id):
            claimed = self._claim_side(comparison_id, engine)
            if claimed is None:
                continue  # another worker holds a live lease
            lease_token, attempt = claimed
            try:
                summary = self._run_one(
                    comparison_id=comparison_id,
                    engine=engine,
                    attempt=attempt,
                    document_id=str(info["document_id"]),
                    source_path=source_path,
                    pages=pages,
                )
                published = self._publish_side_result(
                    comparison_id,
                    engine,
                    lease_token,
                    status="done",
                    summary_json=json.dumps(summary, ensure_ascii=False),
                    artifact_dir=summary.get("artifact_dir"),
                )
                if not published:
                    # Our lease was taken over while running: keep the
                    # artifacts (attempt dir) but publish NOTHING.
                    continue
            except Exception as exc:  # noqa: BLE001 — one side's failure is data
                err_msg = f"{type(exc).__name__}: {exc}"[:500]
                details = getattr(exc, "details", None) or {}
                if isinstance(details, dict):
                    code = details.get("code")
                    if code and code not in err_msg:
                        err_msg = f"{err_msg} [code={code}]"
                self._publish_side_result(
                    comparison_id,
                    engine,
                    lease_token,
                    status="failed",
                    error=err_msg[:500],
                )
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
            # E7/P03: a measurement's pairing must carry WHERE it came from.
            # Two results that differ only in an experiment condition
            # (different row: time/dose/sample) share entity/metric/target;
            # without context the diff would pair arbitrary candidates and
            # invent "field differences". row_context/column_context/table
            # make the candidate identity complete.
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
                        "unit_raw": record.unit_raw,
                        "unit_normalized": record.unit_normalized,
                        "table_id": table.table_id,
                        "page_number": table.page_number,
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

        # E7/P03: candidate grouping lives in pair_measurements (unit-
        # testable, order-invariant).
        measurements = pair_measurements(a["measurements"], b["measurements"])

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
        """Atomically claim one side (E2/S02). Returns (lease_token,
        attempt) or None when not claimable.

        Claimable: queued, failed, or running with an EXPIRED lease (a dead
        worker). The attempt counter increments per execution so each run
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
                        status IN ('queued', 'failed')
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

    def _claimable_sides(self, comparison_id: str) -> list[tuple[str, str]]:
        """Side names a claim could currently grab (queued/failed/expired).

        Used only as a hint for the loop; the claim SQL remains the
        authority (a hint that went stale simply fails to claim)."""

        import datetime as _dt

        now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
        rows = self.repository.connection.execute(
            """
            SELECT side FROM comparison_sides
            WHERE comparison_id = ?
              AND (
                    status IN ('queued', 'failed')
                    OR (status = 'running' AND (lease_until IS NULL OR lease_until < ?))
              )
            ORDER BY side
            """,
            (comparison_id, now),
        ).fetchall()
        return [str(row["side"]) for row in rows]

    def _run_sides(self, comparison_id: str) -> None:
        """Claim-and-run pass: every side is executed by exactly one live
        lease holder; restart/recover needs no special path (expired leases
        simply become claimable again on the next start/retry)."""

        info = self.get(comparison_id)
        pages = [int(p) for p in info["pages"]]
        source_path = Path(self._source_path_of(comparison_id))
        # ONE pass over the claimable snapshot: a side that FAILS in this
        # run stays 'failed' (a fresh retry() re-arms execution) — no
        # self-spinning over the same failure.
        for engine in self._claimable_sides(comparison_id):
            claimed = self._claim_side(comparison_id, engine)
            if claimed is None:
                continue  # another worker holds a live lease
            lease_token, attempt = claimed
            try:
                summary = self._run_one(
                    comparison_id=comparison_id,
                    engine=engine,
                    attempt=attempt,
                    document_id=str(info["document_id"]),
                    source_path=source_path,
                    pages=pages,
                )
                published = self._publish_side_result(
                    comparison_id,
                    engine,
                    lease_token,
                    status="done",
                    summary_json=json.dumps(summary, ensure_ascii=False),
                    artifact_dir=summary.get("artifact_dir"),
                )
                if not published:
                    # Our lease was taken over while running: keep the
                    # artifacts (attempt dir) but publish NOTHING.
                    continue
            except Exception as exc:  # noqa: BLE001 — one side's failure is data
                err_msg = f"{type(exc).__name__}: {exc}"[:500]
                details = getattr(exc, "details", None) or {}
                if isinstance(details, dict):
                    code = details.get("code")
                    if code and code not in err_msg:
                        err_msg = f"{err_msg} [code={code}]"
                self._publish_side_result(
                    comparison_id,
                    engine,
                    lease_token,
                    status="failed",
                    error=err_msg[:500],
                )
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
            # E7/P03: a measurement's pairing must carry WHERE it came from.
            # Two results that differ only in an experiment condition
            # (different row: time/dose/sample) share entity/metric/target;
            # without context the diff would pair arbitrary candidates and
            # invent "field differences". row_context/column_context/table
            # make the candidate identity complete.
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
                        "unit_raw": record.unit_raw,
                        "unit_normalized": record.unit_normalized,
                        "table_id": table.table_id,
                        "page_number": table.page_number,
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

        # ---- E7/P03: candidate-grouped activity pairing ------------------
        # The key is the full CANDIDATE identity — entity, metric, target,
        # condition context (row/column/table/page) — never the value (a
        # value difference must surface as a diff, not as two "missing"
        # rows). Pairing is positional and group-based, so it is invariant
        # to input order; a key with more than one candidate on either
        # side is reported as AMBIGUOUS and never force-paired. Value
        # comparison is semantic (Decimal + normalized unit), formatting
        # like "1.0" vs "1" is not a difference.
        from collections import Counter, defaultdict
        from decimal import Decimal, InvalidOperation

        def measurement_key(m: dict[str, Any]) -> tuple:
            return (
                str(m.get("entity") or ""),
                str(m.get("metric") or ""),
                str(m.get("target") or ""),
                str(m.get("row_context") or ""),
                str(m.get("column_context") or ""),
                str(m.get("table_id") or ""),
                int(m.get("page_number") or 0),
            )

        def group(ms: list[dict[str, Any]]) -> dict[tuple, list[dict[str, Any]]]:
            out: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
            for m in ms:
                out[measurement_key(m)].append(m)
            return out

        def norm_unit(u: str | None) -> str:
            return (u or "").strip().lower().replace("µ", "u").replace(" ", "")

        def num(value: str | None) -> Decimal | None:
            if value is None:
                return None
            try:
                return Decimal(value.strip())
            except (InvalidOperation, AttributeError):
                return None

        ga, gb = group(a["measurements"]), group(b["measurements"])
        keys = set(ga) | set(gb)
        paired = 0
        ambiguous: list[dict[str, Any]] = []
        value_diffs: list[dict[str, Any]] = []
        only_a: list[list[str]] = []
        only_b: list[list[str]] = []
        for key in sorted(keys):
            ca, cb = ga.get(key, []), gb.get(key, [])
            if len(ca) > 1 or len(cb) > 1:
                ambiguous.append(
                    {
                        "key": list(key),
                        "candidates_a": [m.get("value_raw") for m in ca],
                        "candidates_b": [m.get("value_raw") for m in cb],
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
                na, nb = num(ma.get("value_raw")), num(mb.get("value_raw"))
                values_differ = (
                    (na is not None and nb is not None and na != nb)
                    or (na is None or nb is None)
                    and (str(ma.get("value_raw") or "") != str(mb.get("value_raw") or ""))
                )
                units_differ = norm_unit(ma.get("unit_normalized") or ma.get("unit_raw")) != norm_unit(
                    mb.get("unit_normalized") or mb.get("unit_raw")
                )
                if values_differ or units_differ:
                    value_diffs.append(
                        {
                            "entity": key[0],
                            "metric": key[1],
                            "target": key[2],
                            "condition": key[3] or key[4] or None,
                            "value_a": ma.get("value_raw"),
                            "unit_a": ma.get("unit_raw"),
                            "value_b": mb.get("value_raw"),
                            "unit_b": mb.get("unit_raw"),
                            "kind": ("value" if values_differ else "")
                            + ("/" if values_differ and units_differ else "")
                            + ("unit" if units_differ else ""),
                        }
                    )

        measurements = {
            "keys_only_a": only_a,
            "keys_only_b": only_b,
            "paired": paired,
            "ambiguous": len(ambiguous),
            "ambiguous_candidates": ambiguous[:50],
        }

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