"""Run the DoclingServeAdapter against a hosted docling-serve instance.

Usage:
    uv run python scripts/run_docling_serve.py \
        --base-url https://bibyutatsu-hf-docling-serve.hf.space \
        --local-source pdf/paper_TAT-RasGAP_broad_antimicrobial_2017.pdf \
        --pages 1-3 \
        --out data/runs/2026-10-13_docling_serve_paper1 \
        --report reports/2026-10-13_docling_serve_paper1.md
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bio_pdf.ingest.inspector import inspect_pdf, parse_page_selection  # noqa: E402
from bio_pdf.parsers.base import ParseRequest  # noqa: E402
from bio_pdf.parsers.docling_serve_adapter import DoclingServeAdapter  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--local-source", type=Path, required=True)
    parser.add_argument("--pages", default="1-3")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--max-polls", type=int, default=120)
    args = parser.parse_args()

    inspection = inspect_pdf(args.local_source)
    page_numbers = parse_page_selection(args.pages, page_count=inspection.page_count)

    request = ParseRequest(
        job_id=f"docling-serve-{int(time.time())}",
        document_id=inspection.document_id,
        source_path=args.local_source,
        page_numbers=page_numbers,
        profile="layout_native",
        options_ref="options.json",
        output_dir=args.out,
    )

    print(f"[run_docling_serve] base_url={args.base_url}", flush=True)
    print(
        f"[run_docling_serve] document_id={inspection.document_id} "
        f"pages={list(page_numbers)}",
        flush=True,
    )

    adapter = DoclingServeAdapter(
        base_url=args.base_url,
        api_key=args.api_key,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
        max_polls=args.max_polls,
    )

    t0 = time.time()
    artifacts = adapter.parse(request)
    elapsed = time.time() - t0

    print(f"[run_docling_serve] elapsed={elapsed:.1f}s", flush=True)
    print(f"[run_docling_serve] normalized={artifacts.normalized_path}", flush=True)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Docling Serve Live Report",
            "",
            f"- **base_url**: `{args.base_url}`",
            f"- **document_id**: `{inspection.document_id}`",
            f"- **pages requested**: `{list(page_numbers)}`",
            f"- **elapsed (s)**: {elapsed:.1f}",
            f"- **artifacts**: `{artifacts.normalized_path}` / `{artifacts.raw_response_path}` / `{artifacts.manifest_path}`",
            "",
            "> Smoke run only; no accuracy claim.",
        ]
        args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[run_docling_serve] report={args.report}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
