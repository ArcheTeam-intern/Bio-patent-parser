"""C01: freeze the evaluation baseline (samples, versions, published artifacts).

Produces reports/baselines/<date>_eval_baseline.json so every later C-task can
be reviewed against the same source hashes, config snapshot and published
baseline ids. Read-only: it inspects the demo DB and the filesystem, writes no
job state.

Usage: .venv/Scripts/python.exe scripts/freeze_eval_baseline.py
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "post-m1-demo"
SAMPLES = [
    "pdf/paper_TAT-RasGAP_broad_antimicrobial_2017.pdf",
    "pdf/patent_EP3027649B1_TNFa_IL17_bispecific_antibodies.pdf",
    "pdf/patent_WO2020156367A1_rice_RAY1_zh.pdf",
]
OUT_DIR = ROOT / "reports" / "baselines"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def page_count(path: Path) -> int | None:
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(path)
        try:
            return len(doc)
        finally:
            doc.close()
    except Exception:  # noqa: BLE001
        return None


def versions() -> dict[str, str]:
    import platform

    out: dict[str, str] = {"python": platform.python_version()}
    for mod in ("docling", "docling_core", "rapidocr", "onnxruntime", "torch", "pypdfium2"):
        try:
            m = __import__(mod)
            out[mod] = getattr(m, "__version__", "unknown")
        except Exception:  # noqa: BLE001
            out[mod] = "not-installed"
    return out


def git_rev() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def main() -> None:
    db_path = DATA_DIR / "bio.sqlite"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    samples = []
    for rel in SAMPLES:
        path = ROOT / rel
        sha = sha256_of(path)
        row = conn.execute(
            "SELECT document_id FROM documents WHERE sha256 = ?", (sha,)
        ).fetchone()
        jobs = []
        if row:
            for j in conn.execute(
                "SELECT job_id, status, created_at, config_hash FROM jobs WHERE document_id = ? ORDER BY created_at",
                (row["document_id"],),
            ):
                pub = conn.execute(
                    "SELECT baseline_id, manifest_sha256, manifest_rel, published_at FROM publications WHERE job_id = ?",
                    (j["job_id"],),
                ).fetchone()
                stage_versions = {}
                for r in conn.execute(
                    "SELECT stage_name, artifact_manifest_ref, cache_key FROM stage_runs "
                    "WHERE job_id = ? AND status='succeeded'",
                    (j["job_id"],),
                ):
                    impl = "unknown"
                    ref = r["artifact_manifest_ref"] or ""
                    run_id = ref.split(":")[0]
                    if run_id:
                        manifest = (
                            DATA_DIR / "jobs" / j["job_id"] / "stage_runs" / run_id
                            / "stage.manifest.json"
                        )
                        if manifest.exists():
                            try:
                                impl = json.loads(manifest.read_text(encoding="utf-8")).get(
                                    "implementation_version", "unknown"
                                )
                            except Exception:  # noqa: BLE001
                                impl = "unreadable"
                    stage_versions[r["stage_name"]] = {
                        "implementation_version": impl,
                        "cache_key": (r["cache_key"] or "")[:16],
                    }
                jobs.append(
                    {
                        "job_id": j["job_id"],
                        "status": j["status"],
                        "created_at": j["created_at"],
                        "publication": dict(pub) if pub else None,
                        "stage_implementations": stage_versions,
                    }
                )
        samples.append(
            {
                "path": rel,
                "sha256": sha,
                "bytes": path.stat().st_size,
                "page_count": page_count(path),
                "document_id": row["document_id"] if row else None,
                "jobs": jobs,
            }
        )

    config_path = ROOT / "configs" / "demo.toml"
    report = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "git_rev": git_rev(),
        "data_dir": str(DATA_DIR.relative_to(ROOT)),
        "config": {
            "path": "configs/demo.toml",
            "sha256": sha256_of(config_path),
        },
        "versions": versions(),
        "mineru_token_present": bool(__import__("os").environ.get("MINERU_TOKEN")),
        "samples": samples,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "2026-10-16_eval_baseline.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written {out_path.relative_to(ROOT)}")
    for s in samples:
        pub = [j["publication"]["baseline_id"] for j in s["jobs"] if j["publication"]]
        print(
            f"  {s['path']}  sha={s['sha256'][:12]}…  pages={s['page_count']}  jobs={len(s['jobs'])}  published={len(pub)}"
        )


if __name__ == "__main__":
    sys.exit(main())
