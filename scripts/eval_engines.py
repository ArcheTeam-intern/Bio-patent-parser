"""Engine-selection evaluation harness (2026-10-19).

Purpose: decide which engine becomes the PRIMARY parser and which is the
degraded fallback. The comparison page is an evaluation tool, not the
production shape — production runs ONE engine per job with the other as
a fallback.

Design:

- Sample the pages that actually carry the product's target content
  (ST.25 declarations + activity tables) instead of whole 195-page
  patents. MinerU accepts ``page_ranges``, so cost stays bounded.
- Per document record: page coverage, sequence recovery (declared /
  observed / complete), table detection vs table structural health,
  structure counts, wall clock, and any failure stage.
- Score with explicit, documented rules; never present coverage as
  accuracy (there is no human gold).

Usage::

    python scripts/eval_engines.py --out reports/eval_engines_<date>.json
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "http://127.0.0.1:8420/api/v1"

#: Evaluation sample. Pages are chosen by SCANNING the PDFs for ST.25
#: markers / MIC-IC50 headers (see out/eval_scan.json), so each page
#: carries the content under test rather than boilerplate.
SAMPLE = [
    {
        "file": "pdf/patent_CN114144196B_engineered_vaccinia_zh.pdf",
        "pages": [1, 25, 26, 27],
        "why": "Chinese patent with ST.25 declarations from p25",
    },
    {
        "file": "pdf/patent_EP3027649B1_TNFa_IL17_bispecific_antibodies.pdf",
        "pages": [4, 6, 30, 31, 32, 33],
        "why": "195 pages: activity tables (p4/p6) + 60 sequence pages from p30",
    },
    {
        "file": "pdf/patent_US20240026005A1_anti_PD-L2_antibodies.pdf",
        "pages": [1, 2, 30],
        "why": "142 pages, no ST.25 markers: structure/table coverage check",
    },
]


def _request(path: str, *, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {exc.read()[:400]!r}") from exc


def _upload(pdf: Path) -> str:
    boundary = "----evalboundary"
    payload = pdf.read_bytes()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{pdf.name}"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{API}/documents",
        method="POST",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read())["document_id"]


def _wait(comparison_id: str, *, timeout: float = 900.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = _request(f"/comparisons/{comparison_id}")
        if info["status"] in ("done", "failed"):
            return info
        time.sleep(5)
    raise TimeoutError(f"comparison {comparison_id} did not finish in {timeout}s")


def _score_side(side: dict, requested: list[int]) -> dict:
    """One side's outcome → the metrics the engine choice is based on."""

    summary = side.get("summary") or {}
    inventory = summary.get("inventory_summary") or {}
    structure = summary.get("structure") or {}
    figures = summary.get("figures") or []
    sequences = summary.get("sequences") or []
    produced = summary.get("pages_produced") or []

    declared = sum(int(s.get("declared_length") or 0) for s in sequences)
    observed = sum(int(s.get("observed_length") or 0) for s in sequences)
    return {
        "status": side.get("status"),
        "attempt": side.get("attempt"),
        "error_code": (side.get("error_info") or {}).get("code"),
        "error_stage": (side.get("error_info") or {}).get("stage"),
        "pages_produced": len(produced),
        "pages_missing": sorted(set(requested) - set(produced)),
        "sequences": len(sequences),
        "declared_residues": declared,
        "observed_residues": observed,
        "recovery_ratio": round(observed / declared, 3) if declared else None,
        "inventory": {
            "observed_declarations": inventory.get("observed_declarations"),
            "complete": inventory.get("complete"),
            "partial": inventory.get("partial"),
            "missing": inventory.get("missing"),
            "declared_only": inventory.get("declared_only"),
            "conflict": inventory.get("conflict"),
        },
        "structure": structure,
        "figures": len(figures),
        "measurements": len(summary.get("measurements") or []),
    }


def _tables_health(normalized_path: Path) -> dict:
    """Tables detected vs tables the structural gates refused.

    A table with GRID_SIZE_SUSPECT / RAGGED_ROWS_SEVERE /
    HEADER_SEMANTICS_BROKEN is DETECTED but not usable — counting it as
    a win would reward a parser that fabricates grids.
    """

    if not normalized_path.is_file():
        return {"tables": 0, "usable": 0, "broken": 0, "broken_reasons": []}
    doc = json.loads(normalized_path.read_text(encoding="utf-8"))
    total = broken = 0
    reasons: list[str] = []
    for page in doc.get("pages", []):
        for table in page.get("tables", []) or []:
            total += 1
            issues = [str(i) for i in (table.get("structure_issues") or [])]
            if any(
                i.startswith(
                    ("GRID_SIZE_SUSPECT", "RAGGED_ROWS_SEVERE", "HEADER_SEMANTICS_BROKEN")
                )
                for i in issues
            ):
                broken += 1
                reasons.extend(i.split(":")[0] for i in issues[:2])
    return {
        "tables": total,
        "usable": total - broken,
        "broken": broken,
        "broken_reasons": sorted(set(reasons)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("reports/eval_engines.json"))
    parser.add_argument("--reuse", action="store_true", help="reuse existing documents by SHA")
    args = parser.parse_args()

    existing = {
        d["sha256"]: d["document_id"]
        for d in (args.reuse and _request("/documents").get("documents", []) or [])
    }

    results = []
    for sample in SAMPLE:
        pdf = Path(sample["file"])
        started = time.time()
        print(f"[eval] {pdf.name} pages={sample['pages']}", flush=True)
        document_id = _upload(pdf)
        comparison = _request(
            "/comparisons",
            method="POST",
            body={
                "document_id": document_id,
                "pages": sample["pages"],
                "engines": ["docling", "mineru_http"],
            },
        )
        cid = comparison["comparison_id"]
        print(f"[eval]   comparison {cid}", flush=True)
        info = _wait(cid)
        elapsed = round(time.time() - started, 1)
        sides = {s["side"]: s for s in info["sides"]}

        entry = {
            "file": pdf.name,
            "pages": sample["pages"],
            "why": sample["why"],
            "comparison_id": cid,
            "status": info["status"],
            "wall_seconds": elapsed,
            "sides": {},
        }
        for name, side in sides.items():
            side_metrics = _score_side(side, sample["pages"])
            artifact = side.get("artifact_dir")
            if artifact:
                side_metrics["tables"] = _tables_health(
                    Path("data/live-mineru-test") / artifact / "normalized.json"
                )
            entry["sides"][name] = side_metrics
        results.append(entry)
        print(
            f"[eval]   {info['status']} in {elapsed}s | "
            + " | ".join(
                f"{n}: seq={m['sequences']} recov={m['recovery_ratio']} "
                f"tbl={m.get('tables', {}).get('usable')}/{m.get('tables', {}).get('tables')}"
                for n, m in entry["sides"].items()
            ),
            flush=True,
        )

    # ---- aggregate score -------------------------------------------------
    def aggregate(side_name: str) -> dict:
        docs = [r for r in results if r["status"] == "done"]
        ok = [r for r in docs if r["sides"][side_name]["status"] == "done"]
        declared = sum(r["sides"][side_name]["declared_residues"] for r in ok)
        observed = sum(r["sides"][side_name]["observed_residues"] for r in ok)
        tables = sum(r["sides"][side_name].get("tables", {}).get("tables", 0) for r in ok)
        usable = sum(r["sides"][side_name].get("tables", {}).get("usable", 0) for r in ok)
        complete = sum(
            (r["sides"][side_name]["inventory"].get("complete") or 0) for r in ok
        )
        observed_decls = sum(
            (r["sides"][side_name]["inventory"].get("observed_declarations") or 0)
            for r in ok
        )
        return {
            "runs": len(docs),
            "success": len(ok),
            "success_rate": round(len(ok) / len(docs), 3) if docs else None,
            "residue_recovery": round(observed / declared, 3) if declared else None,
            "complete_declarations": complete,
            "observed_declarations": observed_decls,
            "table_usable_rate": round(usable / tables, 3) if tables else None,
            "tables_detected": tables,
            "tables_usable": usable,
            "mean_wall_seconds": round(
                sum(r["wall_seconds"] for r in ok) / len(ok), 1
            ) if ok else None,
        }

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sample_rule": (
            "pages carrying ST.25 markers or MIC/IC50 headers (scanned locally); "
            "no full-document runs — MinerU page_ranges keeps cost bounded"
        ),
        "score_rule": (
            "residue_recovery = observed/declared residues (primary: sequence is the "
            "product's core value); table_usable_rate counts only tables that pass the "
            "structural gates (GRID_SIZE_SUSPECT / RAGGED_ROWS_SEVERE / "
            "HEADER_SEMANTICS_BROKEN are excluded); success_rate is the stability floor. "
            "No human gold exists: these are coverage metrics, NOT accuracy."
        ),
        "results": results,
        "aggregate": {"docling": aggregate("docling"), "mineru_http": aggregate("mineru_http")},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report["aggregate"], indent=2, ensure_ascii=False))
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
