"""Atomic human-review service (design §8.2).

One decision runs inside a single SQLite transaction:

1. validate review_id / record_id / base_revision against the live head,
2. whitelist and type-check the typed patch (document_id, original parse
   artifacts and evidence raw_text are never patchable; a corrected
   ``value.raw`` must appear in the record's actual evidence),
3. apply the patch and re-run the domain + evidence validators,
4. refuse acceptance while unresolved residues or missing evidence remain
   (clicking a button never jumps over an unresolved ambiguity),
5. write the immutable record version, move the head, append the decision,
   close the review item and bump the job's result revision.

A stale ``base_revision`` raises :class:`ReviewRevisionConflictError` →
HTTP 409 with both values; the client must re-check, never overwrite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bio_pdf.contracts import Measurement, ReviewDecision, SequenceRecord
from bio_pdf.errors import ReviewRevisionConflictError, WorkflowError
from bio_pdf.storage.repository import Repository
from bio_pdf.storage.result_reader import ResultReader
from bio_pdf.validation.activities import validate_measurement
from bio_pdf.validation.evidence import validate_evidence
from bio_pdf.validation.sequences import validate_sequence

#: Patchable fields per record kind. Everything else (ids, document_id,
#: evidence ids, checks, states) is system-owned.
_PATCHABLE: dict[str, set[str]] = {
    "measurement": {"metric", "unit_raw", "unit_normalized", "target_raw", "value"},
    # Sequence residues carry per-position provenance; correcting residue
    # content requires re-extraction, never an overwrite.
    "sequence": set(),
}


@dataclass
class DecisionOutcome:
    review_id: str
    action: str
    record_id: str
    new_revision: int
    validation_state: str
    acceptance_state: str
    open_reviews: int
    result_revision: int
    record: dict[str, Any]


class ReviewService:
    """Apply confirm/correct/reject decisions atomically."""

    def __init__(self, repository: Repository, artifact_root: Path) -> None:
        self.repository = repository
        self.artifact_root = artifact_root

    def apply_decision(
        self,
        *,
        review_id: str,
        action: str,
        base_revision: int,
        actor: str,
        reason: str | None = None,
        patch: dict[str, Any] | None = None,
        evidence_ids: list[str] | None = None,
    ) -> DecisionOutcome:
        if action not in ("confirm", "correct", "reject"):
            raise WorkflowError(
                f"unknown review action: {action}",
                details={"action": action},
            )
        if action == "reject" and not (reason or "").strip():
            raise WorkflowError(
                "reject requires a reason",
                details={"action": action},
            )

        conn = self.repository.connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            item = self._load_review_item(review_id)
            job_id = str(item["job_id"])
            record_id = str(item["record_id"])

            head = self.repository.get_record_head(job_id, record_id)
            current_revision, record_dict, kind = self._current_record(
                job_id, record_id, head
            )
            span_lookup = self._span_lookup(job_id)

            # Conflict check comes before the resolved-state check: a second
            # window submitting its stale view must see 409, not "resolved".
            if base_revision != current_revision:
                raise ReviewRevisionConflictError(
                    f"base_revision {base_revision} does not match live revision {current_revision}",
                    details={
                        "review_id": review_id,
                        "live_revision": current_revision,
                        "base_revision": base_revision,
                    },
                )
            self._ensure_open(item)

            if action == "correct":
                record_dict = self._apply_patch(record_dict, kind, patch or {}, span_lookup)
            record = self._validate_record(record_dict, kind, span_lookup)

            blockers = self._acceptance_blockers(record, kind)
            if action in ("confirm", "correct") and blockers:
                raise WorkflowError(
                    "cannot accept while blockers remain: " + "; ".join(blockers),
                    details={"blockers": blockers, "action": action},
                )

            if action == "reject":
                record["acceptance_state"] = "rejected"
            else:
                record["acceptance_state"] = "accepted"
                record["validation_state"] = "verified_human"

            new_revision = current_revision + 1
            decision = ReviewDecision(
                decision_id=f"dec-{uuid4_hex()}",
                review_id=review_id,
                actor=actor,
                action=action,  # type: ignore[arg-type]
                patch_json=self._decision_patch(patch or {}, reason),
                evidence_ids=evidence_ids or [],
                base_revision=base_revision,
            )

            self._write_version(job_id, record_id, new_revision, kind, record, decision.decision_id)
            self._move_head(job_id, record_id, new_revision, kind, record)
            self.repository.append_review_decision(decision)
            conn.execute(
                "UPDATE review_items SET status = 'resolved' WHERE review_id = ?",
                (review_id,),
            )
            conn.execute(
                """
                UPDATE jobs SET result_revision = result_revision + 1, updated_at = ?
                WHERE job_id = ?
                """,
                (_now_iso(), job_id),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        job = self.repository.get_job(job_id)
        return DecisionOutcome(
            review_id=review_id,
            action=action,
            record_id=record_id,
            new_revision=new_revision,
            validation_state=str(record.get("validation_state")),
            acceptance_state=str(record.get("acceptance_state")),
            open_reviews=self._open_review_count(job_id),
            result_revision=job.result_revision if job else 0,
            record=record,
        )

    def apply_batch(
        self,
        *,
        decisions: list[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        """Two-phase bulk decision (C06).

        Phase 1 is read-only: every item must exist, still be open, and its
        ``base_revision`` must equal the live revision. ANY mismatch returns
        the full conflict list and applies NOTHING (plan §3.3: precheck all,
        then submit; one conflict fails the whole batch). Phase 2 applies
        the decisions through :meth:`apply_decision`, so each record version,
        decision log entry and revision bump has the same guarantees as a
        single decision.
        """

        if not decisions:
            raise WorkflowError(
                "batch decision requires at least one item",
                details={"action": "batch"},
            )

        conflicts: list[dict[str, Any]] = []
        for d in decisions:
            review_id = str(d.get("review_id") or "")
            base_revision = int(d.get("base_revision", -1))
            try:
                item = self._load_review_item(review_id)
            except (WorkflowError, KeyError):
                conflicts.append({"review_id": review_id, "reason": "not_found"})
                continue
            job_id = str(item["job_id"])
            record_id = str(item["record_id"])
            head = self.repository.get_record_head(job_id, record_id)
            current_revision, _record, _kind = self._current_record(job_id, record_id, head)
            if base_revision != current_revision:
                conflicts.append(
                    {
                        "review_id": review_id,
                        "reason": "revision_conflict",
                        "base_revision": base_revision,
                        "live_revision": current_revision,
                    }
                )
                continue
            if str(item["status"]) != "open":
                conflicts.append({"review_id": review_id, "reason": "not_open"})

        if conflicts:
            return {"status": "conflict", "applied": [], "conflicts": conflicts}

        applied: list[dict[str, Any]] = []
        for d in decisions:
            outcome = self.apply_decision(
                review_id=str(d["review_id"]),
                action=str(d["action"]),
                base_revision=int(d["base_revision"]),
                actor=actor,
                reason=d.get("reason"),
                patch=d.get("patch"),
                evidence_ids=d.get("evidence_ids"),
            )
            applied.append(
                {
                    "review_id": outcome.review_id,
                    "record_id": outcome.record_id,
                    "new_revision": outcome.new_revision,
                    "acceptance_state": outcome.acceptance_state,
                }
            )
        return {"status": "applied", "applied": applied, "conflicts": []}

    # ---- internals --------------------------------------------------------

    def _load_review_item(self, review_id: str) -> Any:
        row = self.repository.connection.execute(
            "SELECT * FROM review_items WHERE review_id = ?", (review_id,)
        ).fetchone()
        if row is None:
            raise WorkflowError(
                f"review item not found: {review_id}",
                details={"review_id": review_id},
            )
        return row

    @staticmethod
    def _ensure_open(item: Any) -> None:
        if item["status"] == "resolved":
            raise WorkflowError(
                f"review item already resolved: {item['review_id']}",
                details={"review_id": item["review_id"]},
            )

    def _current_record(
        self, job_id: str, record_id: str, head: Any
    ) -> tuple[int, dict[str, Any], str]:
        """Load the current record dict and its revision.

        Reads through the head pointer when present, else from the job's
        published validation baseline via ResultReader (hash-verified when
        published; legacy fixed paths stay readable for old jobs).
        """

        if head is not None:
            version = self.repository.get_record_version(job_id, record_id, int(head["revision"]))
            if version is not None:
                return int(head["revision"]), json.loads(version["record_json"]), str(head["kind"])

        reader = ResultReader(self.artifact_root)
        data = reader.read_json(job_id, "validation")
        for seq in data.get("sequences", []):
            if seq["sequence_id"] == record_id:
                return 1, seq, "sequence"
        for meas in data.get("measurements", []):
            if meas["measurement_id"] == record_id:
                return 1, meas, "measurement"
        raise WorkflowError(
            f"record not found in job baseline: {record_id}",
            details={"record_id": record_id},
        )

    def _span_lookup(self, job_id: str) -> dict[str, dict[str, Any]]:
        reader = ResultReader(self.artifact_root)
        data = reader.read_json(job_id, "extract")
        return {span.get("evidence_id"): span for span in data.get("evidence_spans", [])}

    def _apply_patch(
        self,
        record: dict[str, Any],
        kind: str,
        patch: dict[str, Any],
        span_lookup: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        allowed = _PATCHABLE.get(kind, set())
        illegal = sorted(set(patch) - allowed)
        if illegal:
            raise WorkflowError(
                f"patch fields not allowed for {kind}: {illegal}",
                details={"fields": illegal, "kind": kind},
            )

        updated = dict(record)
        for field, value in patch.items():
            if field == "value":
                updated["value"] = self._validated_value_patch(value)
            elif field in ("metric", "unit_raw", "unit_normalized", "target_raw"):
                if value is not None and not isinstance(value, str):
                    raise WorkflowError(
                        f"patch field {field} must be a string",
                        details={"field": field},
                    )
                updated[field] = value
            else:
                updated[field] = value

        if "value" in updated:
            self._verify_raw_against_evidence(updated, span_lookup)
        return updated

    def _validated_value_patch(self, value: Any) -> dict[str, Any]:
        """Type-check a structured ActivityValue patch (no flat JSON strings)."""

        if not isinstance(value, dict):
            raise WorkflowError(
                "value patch must be an object with kind and fields",
                details={"got": type(value).__name__},
            )
        allowed_fields = {
            "kind", "operator", "number", "lower", "upper", "uncertainty", "qualifier", "raw",
        }
        unknown = sorted(set(value) - allowed_fields)
        if unknown:
            raise WorkflowError(
                f"unknown value fields: {unknown}",
                details={"fields": unknown},
            )
        kinds = {"scalar", "range", "plus_minus", "categorical", "unparsed"}
        if value.get("kind") not in kinds:
            raise WorkflowError(
                "value patch needs a valid kind",
                details={"kind": value.get("kind")},
            )
        kind = value["kind"]
        required = {
            "scalar": ("number",),
            "range": ("lower", "upper"),
            "plus_minus": ("number", "uncertainty"),
            "categorical": ("qualifier",),
            "unparsed": ("raw",),
        }[kind]
        missing = [f for f in required if not value.get(f)]
        if missing:
            raise WorkflowError(
                f"value kind {kind} requires fields: {missing}",
                details={"missing": missing},
            )
        # Numeric fields stay decimal strings — never floats.
        for field in ("number", "lower", "upper", "uncertainty"):
            if value.get(field) is not None and not isinstance(value[field], str):
                raise WorkflowError(
                    f"value.{field} must be a decimal string",
                    details={"field": field},
                )

        # B07: structured fields must AGREE with raw — a patch claiming
        # raw=">128" with number="12.8"/operator="=" is a typo or a lie,
        # and accepting it would corrupt the export while looking audited.
        raw = str(value.get("raw") or "").strip()
        if raw and kind != "unparsed":
            from bio_pdf.extract.activities.values import parse_activity_value

            parsed = parse_activity_value(raw)
            mismatches: list[str] = []
            if parsed.kind != kind:
                mismatches.append(f"kind: raw parses as {parsed.kind}, patch says {kind}")
            else:
                for field in ("operator", "number", "lower", "upper", "uncertainty"):
                    raw_value = getattr(parsed, field, None)
                    patch_value = value.get(field)
                    if (raw_value or None) != (patch_value or None):
                        mismatches.append(
                            f"{field}: raw gives {raw_value!r}, patch says {patch_value!r}"
                        )
            if mismatches:
                raise WorkflowError(
                    "value patch contradicts its raw expression: " + "; ".join(mismatches),
                    details={"raw": raw, "mismatches": mismatches},
                )
        return value

    def _verify_raw_against_evidence(
        self, record: dict[str, Any], span_lookup: dict[str, dict[str, Any]]
    ) -> None:
        """A corrected value.raw must exist in the record's actual evidence."""

        raw = str(record.get("value", {}).get("raw") or "")
        if not raw:
            return
        texts = []
        for eid in record.get("evidence_ids", []):
            span = span_lookup.get(eid)
            if span and span.get("raw_text"):
                texts.append(str(span["raw_text"]))
        if not texts:
            return  # no raw text captured — cannot verify; page anchor still present
        if not any(raw in t for t in texts):
            raise WorkflowError(
                "corrected value.raw does not appear in the record's evidence text",
                details={"raw": raw, "evidence_count": len(texts)},
            )

    def _validate_record(
        self, record: dict[str, Any], kind: str, span_lookup: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """Re-run domain + evidence checks; update the record's check list."""

        payload = {k: v for k, v in record.items() if k != "review_reasons"}
        if kind == "measurement":
            domain_record = Measurement.model_validate(payload)
            result = validate_measurement(domain_record)
        else:
            domain_record = SequenceRecord.model_validate(payload)
            result = validate_sequence(domain_record)
        result.checks.extend(validate_evidence(domain_record.evidence_ids, span_lookup).checks)

        updated = dict(record)
        updated["checks"] = [c.model_dump(mode="json") for c in result.checks]
        updated["validation_state"] = (
            "conflict"
            if any(c.outcome == "fail" for c in result.checks)
            else result.validation_state
        )
        updated["review_reasons"] = result.review_reasons
        return updated

    def _acceptance_blockers(self, record: dict[str, Any], kind: str) -> list[str]:
        """Conditions a button click must never wave through."""

        blockers: list[str] = []
        checks = record.get("checks", [])
        failed = [c.get("check_id") for c in checks if c.get("outcome") == "fail"]
        if failed:
            blockers.append(f"failed checks: {failed}")
        if kind == "sequence":
            if any(r.get("resolved") == "false" for r in record.get("residues", [])):
                blockers.append("sequence contains unresolved residues")
        return blockers

    def _write_version(
        self,
        job_id: str,
        record_id: str,
        revision: int,
        kind: str,
        record: dict[str, Any],
        decision_id: str,
    ) -> None:
        self.repository.connection.execute(
            """
            INSERT INTO record_versions (job_id, record_id, revision, kind, record_json, decision_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                record_id,
                revision,
                kind,
                json.dumps(record, ensure_ascii=False),
                decision_id,
                _now_iso(),
            ),
        )

    def _move_head(
        self,
        job_id: str,
        record_id: str,
        revision: int,
        kind: str,
        record: dict[str, Any],
    ) -> None:
        self.repository.connection.execute(
            """
            INSERT INTO record_heads (job_id, record_id, revision, kind, validation_state, acceptance_state, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, record_id) DO UPDATE SET
                revision = excluded.revision,
                kind = excluded.kind,
                validation_state = excluded.validation_state,
                acceptance_state = excluded.acceptance_state,
                updated_at = excluded.updated_at
            """,
            (
                job_id,
                record_id,
                revision,
                kind,
                str(record.get("validation_state", "unchecked")),
                str(record.get("acceptance_state", "candidate")),
                _now_iso(),
            ),
        )

    def _decision_patch(
        self, patch: dict[str, Any], reason: str | None
    ) -> dict[str, str | int | float | bool | None]:
        flat: dict[str, str | int | float | bool | None] = {}
        for key, value in patch.items():
            if isinstance(value, dict):
                for sub_key, sub in value.items():
                    flat[f"{key}.{sub_key}"] = sub
            else:
                flat[key] = value
        if reason:
            flat["reason"] = reason
        return flat

    def _open_review_count(self, job_id: str) -> int:
        row = self.repository.connection.execute(
            "SELECT COUNT(*) AS n FROM review_items WHERE job_id = ? AND status = 'open'",
            (job_id,),
        ).fetchone()
        return int(row["n"]) if row else 0


def uuid4_hex() -> str:
    from uuid import uuid4

    return uuid4().hex[:12]


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


__all__ = ["ReviewService", "DecisionOutcome"]
