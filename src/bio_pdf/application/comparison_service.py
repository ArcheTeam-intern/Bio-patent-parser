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


@dataclass
class ComparisonService:
    repository: Repository
    artifact_root: Path
    #: tests patch this to a fake adapter factory
    engine_factory: Callable[..., Any] = staticmethod(_side_engine)

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
        """Run pending/failed sides on a daemon thread (DB-backed status)."""

        thread = threading.Thread(
            target=self._run_sides, args=(comparison_id,), daemon=True
        )
        thread.start()

    def retry(self, comparison_id: str) -> dict[str, Any]:
        conn = self.repository.connection
        conn.execute(
            """
            UPDATE comparison_sides SET status = 'queued', error = NULL
            WHERE comparison_id = ? AND status IN ('failed', 'queued')
            """,
            (comparison_id,),
        )
        conn.execute(
            "UPDATE comparisons SET status = 'queued', updated_at = ? WHERE comparison_id = ?",
            (_now(), comparison_id),
        )
        conn.commit()
        self.start(comparison_id)
        return self.get(comparison_id)

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

    def _run_sides(self, comparison_id: str) -> None:
        info = self.get(comparison_id)
        pages = [int(p) for p in info["pages"]]
        conn = self.repository.connection
        for side in info["sides"]:
            if side["status"] != "queued":
                continue
            engine = side["side"]
            conn.execute(
                "UPDATE comparison_sides SET status = 'running', started_at = ? WHERE comparison_id = ? AND side = ?",
                (_now(), comparison_id, engine),
            )
            conn.commit()
            try:
                summary = self._run_one(
                    comparison_id=comparison_id,
                    engine=engine,
                    document_id=str(info["document_id"]),
                    source_path=Path(self._source_path_of(comparison_id)),
                    pages=pages,
                )
                conn.execute(
                    """
                    UPDATE comparison_sides SET status = 'done', summary_json = ?,
                        artifact_dir = ?, ended_at = ? WHERE comparison_id = ? AND side = ?
                    """,
                    (
                        json.dumps(summary, ensure_ascii=False),
                        summary.get("artifact_dir"),
                        _now(),
                        comparison_id,
                        engine,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — one side's failure is data
                err_msg = f"{type(exc).__name__}: {exc}"[:500]
                details = getattr(exc, "details", None) or {}
                if isinstance(details, dict):
                    code = details.get("code")
                    if code and code not in err_msg:
                        err_msg = f"{err_msg} [code={code}]"
                conn.execute(
                    """
                    UPDATE comparison_sides SET status = 'failed', error = ?, ended_at = ?
                    WHERE comparison_id = ? AND side = ?
                    """,
                    (err_msg[:500], _now(), comparison_id, engine),
                )
            conn.commit()
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
    ) -> dict[str, Any]:
        """One side: adapter parse → shared domain rules → side summary."""

        from bio_pdf.parsers.base import ParseRequest

        side_dir = self.artifact_root / "comparisons" / comparison_id / engine
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
            for record in extract_table_measurements(
                table, document_id=document_id, evidences=cell_evidence, evidence_kind="undetermined"
            ):
                measurements.append(
                    {
                        "entity": record.entity_raw,
                        "metric": record.metric,
                        "target": record.target_raw,
                        "value_raw": record.value.raw,
                        "unit_raw": record.unit_raw,
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

        def measurement_key(m: dict[str, Any]) -> tuple:
            # Per plan §5.5: value/unit are COMPARED fields, never part of
            # the match key — otherwise a value difference surfaces as
            # "missing on both sides".
            return (m.get("entity"), m.get("metric"), m.get("target"))

        # Candidate counting per side (multiple candidates stay ambiguous).
        from collections import Counter

        cand_a = Counter(measurement_key(m) for m in a["measurements"])
        cand_b = Counter(measurement_key(m) for m in b["measurements"])
        keys = set(cand_a) | set(cand_b)
        measurements = {
            "keys_only_a": [list(k) for k in sorted(keys - set(cand_b))],
            "keys_only_b": [list(k) for k in sorted(keys - set(cand_a))],
            "paired": sum(1 for k in keys if cand_a.get(k, 0) == 1 and cand_b.get(k, 0) == 1),
            "ambiguous": sum(
                1 for k in keys if cand_a.get(k, 0) > 1 or cand_b.get(k, 0) > 1
            ),
        }
        value_diffs: list[dict[str, Any]] = []
        by_a = {measurement_key(m): m for m in a["measurements"]}
        by_b = {measurement_key(m): m for m in b["measurements"]}
        for key in sorted(set(by_a) & set(by_b)):
            ma, mb = by_a[key], by_b[key]
            if ma["value_raw"] != mb["value_raw"] or ma["unit_raw"] != mb["unit_raw"]:
                value_diffs.append(
                    {
                        "entity": key[0],
                        "metric": key[1],
                        "target": key[2],
                        "value_a": ma["value_raw"],
                        "unit_a": ma["unit_raw"],
                        "value_b": mb["value_raw"],
                        "unit_b": mb["unit_raw"],
                    }
                )

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
            "measurements": {**measurements, "value_diffs": value_diffs[:200]},
            "evaluation": {
                "gold": None,
                "status": "not_evaluated",
                "note": "no verified human gold exists; agreement is not accuracy",
            },
        }