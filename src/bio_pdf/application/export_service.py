"""Export service (design §14).

An export reads ONE consistent snapshot: the record heads overlaid on the
validation baseline, read inside a single transaction, with the job's
``result_revision`` recorded in the manifest. Formats:

- ``json``  — full domain records, checks, evidence refs, review states.
- ``csv``   — activities + sequences; range/uncertainty/comparator stay in
  separate columns (never collapsed into one number).
- ``fasta`` — only *accepted*, *complete*, determinable sequences; every
  excluded sequence is listed with its reason.

A job with no results fails the export instead of producing an empty file
disguised as success.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bio_pdf.errors import WorkflowError
from bio_pdf.storage.artifacts import atomic_write_bytes, atomic_write_json
from bio_pdf.storage.repository import Repository

FORMATS = ("json", "csv", "fasta")


@dataclass
class ExportResult:
    export_id: str
    status: str
    format: str
    result_revision: int
    record_count: int
    artifact_rel: str | None
    error: str | None


class ExportService:
    """Build exports from an atomic snapshot of one job's records."""

    def __init__(self, repository: Repository, artifact_root: Path) -> None:
        self.repository = repository
        self.artifact_root = artifact_root

    # ---- snapshot ---------------------------------------------------------

    def snapshot(self, job_id: str) -> dict[str, Any]:
        """Read baseline + heads atomically; return records and revision."""

        conn = self.repository.connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            job = self.repository.get_job(job_id)
            if job is None:
                raise WorkflowError(f"job not found: {job_id}", details={"job_id": job_id})
            validation_path = self.artifact_root / f"jobs/{job_id}/validate/validation.json"
            if not validation_path.is_file():
                raise WorkflowError(
                    "job has no validation results to export",
                    details={"job_id": job_id},
                )
            data = json.loads(validation_path.read_text(encoding="utf-8"))
            sequences = data.get("sequences", [])
            measurements = data.get("measurements", [])
            heads = {row["record_id"]: row for row in self.repository.list_record_heads(job_id)}
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        def _overlay(records: list[dict[str, Any]], id_field: str) -> list[dict[str, Any]]:
            out = []
            for record in records:
                head = heads.get(record[id_field])
                if head is not None:
                    version = self.repository.get_record_version(
                        job_id, record[id_field], int(head["revision"])
                    )
                    if version is not None:
                        record = json.loads(version["record_json"])
                out.append(record)
            return out

        return {
            "result_revision": job.result_revision,
            "document_id": job.document_id,
            "sequences": _overlay(sequences, "sequence_id"),
            "measurements": _overlay(measurements, "measurement_id"),
        }

    # ---- formats ----------------------------------------------------------

    def build(
        self, job_id: str, fmt: str, *, accepted_only: bool = False
    ) -> tuple[str, int]:
        """Build the export artifact; return (relative_path, record_count)."""

        snap = self.snapshot(job_id)
        sequences = snap["sequences"]
        measurements = snap["measurements"]
        revision = snap["result_revision"]

        # Provenance: everything a downstream consumer needs to reproduce
        # and audit this export.
        job = self.repository.get_job(job_id)
        cfg_row = self.repository.get_job_config(job_id)
        effective_cfg = cfg_row.get("effective_config") or {}
        manifest = {
            "schema_version": "1.1.0",
            "job_id": job_id,
            "document_id": snap["document_id"],
            "source_sha256": snap["document_id"].removeprefix("sha256:"),
            "result_revision": revision,
            "pipeline_version": cfg_row.get("pipeline"),
            "effective_config_hash": job.config_hash if job else None,
            "effective_config": effective_cfg,
            "rule_versions": (effective_cfg.get("rule_versions") or {}),
            "format": fmt,
            "accepted_only": accepted_only,
            "coverage": snap.get("coverage"),
            "warnings": snap.get("warnings", []),
            "counts": {
                "sequences": len(sequences),
                "measurements": len(measurements),
            },
        }

        if fmt == "json":
            body = {
                **manifest,
                "sequences": _filtered(sequences, accepted_only),
                "measurements": _filtered(measurements, accepted_only),
            }
            text = json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True)
            count = len(body["sequences"]) + len(body["measurements"])
        elif fmt == "csv":
            lines = ["record_type,record_id,metric,entity,value_kind,operator,number,lower,upper,uncertainty,qualifier,unit_raw,target_raw,evidence_kind,validation_state,acceptance_state"]
            count = 0
            for m in _filtered(measurements, accepted_only):
                v = m["value"]
                lines.append(
                    ",".join(
                        _csv_cell(x)
                        for x in (
                            "measurement", m["measurement_id"], m["metric"],
                            m.get("entity_id") or "", v["kind"],
                            v.get("operator") or "", v.get("number") or "",
                            v.get("lower") or "", v.get("upper") or "",
                            v.get("uncertainty") or "", v.get("qualifier") or "",
                            m.get("unit_raw") or "", m.get("target_raw") or "",
                            m.get("evidence_kind"), m.get("validation_state"),
                            m.get("acceptance_state"),
                        )
                    )
                )
                count += 1
            for s in _filtered(sequences, accepted_only):
                lines.append(
                    ",".join(
                        _csv_cell(x)
                        for x in (
                            "sequence", s["sequence_id"], "", "",
                            "", "", "", "", "", "", "",
                            "", "", "",
                            s.get("validation_state"), s.get("acceptance_state"),
                        )
                    )
                )
                count += 1
            # Formula-injection guard: cells starting with = + - @ are the
            # spreadsheet's problem only if we let them; quote everything.
            text = "\n".join(lines) + "\n"
        elif fmt == "fasta":
            included, excluded = [], []
            for s in sequences:
                reasons = _fasta_exclusion_reasons(s)
                if reasons:
                    excluded.append({"sequence_id": s["sequence_id"], "reasons": reasons})
                else:
                    included.append(s)
            text = "".join(
                f">{s['seq_id_no'] or s['sequence_id']} {s['molecule_type']}\n{s['normalized_sequence']}\n"
                for s in included
            )
            if not included:
                raise WorkflowError(
                    "no sequence satisfies FASTA criteria (accepted + complete + determinable); refusing to write an empty export",
                    details={"excluded": len(excluded)},
                )
            body = {
                **manifest,
                "fasta_included": len(included),
                "fasta_excluded": excluded,
            }
            # The FASTA file itself stays pure; the manifest travels beside it.
            atomic_write_json(
                self.artifact_root / f"jobs/{job_id}/export",
                f"export-r{revision}-fasta-manifest.json",
                body,
            )
            count = len(included)
        else:
            raise WorkflowError(f"unknown export format: {fmt}", details={"format": fmt})

        out_dir = self.artifact_root / f"jobs/{job_id}/export"
        ref = atomic_write_bytes(out_dir, f"export-r{revision}.{fmt}", text.encode("utf-8"))
        return f"jobs/{job_id}/export/{ref.relative_path}", count


def _filtered(records: list[dict[str, Any]], accepted_only: bool) -> list[dict[str, Any]]:
    if not accepted_only:
        return records
    return [r for r in records if r.get("acceptance_state") == "accepted"]


def _fasta_exclusion_reasons(s: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if s.get("acceptance_state") != "accepted":
        reasons.append(f"acceptance_state={s.get('acceptance_state')}")
    if not s.get("is_complete"):
        reasons.append("incomplete")
    if s.get("representation") in ("unresolved", "modified"):
        reasons.append(f"representation={s.get('representation')}")
    if any(r.get("resolved") == "false" for r in s.get("residues", [])):
        reasons.append("contains unresolved residues")
    if not s.get("normalized_sequence"):
        reasons.append("empty sequence")
    return reasons


def _csv_cell(value: Any) -> str:
    text = str(value if value is not None else "")
    # Formula-injection guard (design §14): prefix a quote to spreadsheet
    # formula starters, then quote every cell for comma safety.
    if text.startswith(("=", "+", "-", "@")):
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


__all__ = ["ExportService", "ExportResult", "FORMATS"]
