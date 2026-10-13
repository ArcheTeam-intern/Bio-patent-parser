"""Run a real MinerU V1 SaaS parse and emit the standard artifact set.

This is the user-facing entry point. It:
  1. Reads the Bearer token from ``MINERU_TOKEN`` (or the --token flag).
  2. Submits a public PDF URL to the SaaS endpoint.
  3. Polls until the task finishes (or the local timeout fires).
  4. Writes raw_response.json, normalized.json, manifest.json.
  5. Renders one evidence PNG per requested page.
  6. Emits a Markdown report in ``reports/`` (no accuracy claims).

Usage:
  MINERU_TOKEN=sk-... uv run python scripts/run_mineru_live.py \\
      --source-url https://your-bucket/x.pdf \\
      --pages 1-3 \\
      --out data/runs/2026-10-13_mineru \\
      --report reports/2026-10-13_mineru_live.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Ensure ``src`` is importable without installing the package.
THIS_DIR = Path(__file__).resolve().parent
ROOT = THIS_DIR.parent
sys.path.insert(0, str(ROOT / "src"))

from bio_pdf.ingest.inspector import inspect_pdf  # noqa: E402
from bio_pdf.parsers.base import ParseRequest  # noqa: E402
from bio_pdf.parsers.mineru_adapter import MinerUAdapter  # noqa: E402
from bio_pdf.pdf.render import render_evidence_crop  # noqa: E402


def _parse_pages(spec: str, page_count: int) -> tuple[int, ...]:
    """Re-use the inspector's selector. Inline-imported to keep the script
    standalone-executable."""

    from bio_pdf.ingest.inspector import parse_page_selection

    return parse_page_selection(spec, page_count=page_count)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-url",
        default=None,
        help=(
            "A public http(s) URL pointing at the PDF. Mutually exclusive with "
            "--local-only; if neither is set, --local-source is uploaded via "
            "the MinerU file-urls endpoint."
        ),
    )
    parser.add_argument(
        "--local-source",
        type=Path,
        default=None,
        help=(
            "Local PDF. If --source-url is omitted, this is uploaded to "
            "MinerU's file-urls endpoint and the resulting public URL is "
            "submitted. Always used for inspection + evidence rendering."
        ),
    )
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Run only the local inspection + report; skip MinerU submission.",
    )
    parser.add_argument(
        "--pages",
        default="1-3",
        help="Page selection: 'all', '1-3,7', etc. (default: 1-3).",
    )
    parser.add_argument("--out", type=Path, required=True, help="Output directory for artifacts.")
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Optional Markdown report path. Defaults to <out>/report.md.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("MINERU_TOKEN"),
        help="MinerU Bearer token (default: $MINERU_TOKEN).",
    )
    parser.add_argument(
        "--model-version",
        default="vlm",
        help="MinerU model version (default: vlm).",
    )
    parser.add_argument("--poll-interval", type=float, default=3.0)
    parser.add_argument("--max-polls", type=int, default=600)
    args = parser.parse_args()

    if not args.token:
        print("ERROR: MinerU token not set. Pass --token or MINERU_TOKEN.", file=sys.stderr)
        return 2

    if not args.local_source and not args.source_url:
        print("ERROR: provide --local-source and/or --source-url.", file=sys.stderr)
        return 2

    document_id = "sha256:live"
    page_count = 0
    if args.local_source:
        inspection = inspect_pdf(args.local_source)
        document_id = inspection.document_id
        page_count = inspection.page_count

    pages = _parse_pages(args.pages, page_count=page_count) if page_count else (1,)
    print(f"[run_mineru_live] document_id={document_id} pages={list(pages)}")

    request = ParseRequest(
        job_id=f"mineru-{int(time.time())}",
        document_id=document_id,
        source_path=args.local_source or Path("placeholder.pdf"),
        page_numbers=pages,
        profile="layout_native",
        options_ref="options.json",
        output_dir=args.out,
    )

    adapter = MinerUAdapter(
        token=args.token,
        model_version=args.model_version,
        poll_interval=args.poll_interval,
        max_polls=args.max_polls,
    )

    if args.local_only:
        print("[run_mineru_live] --local-only set; skipping MinerU submission.")
        return 0

    t0 = time.time()
    artifacts = adapter.parse(
        request,
        source_url=args.source_url,
        local_file=args.local_source,
    )
    elapsed = time.time() - t0

    # Render one evidence crop per page if we have a local source.
    crops: list[dict[str, object]] = []
    if args.local_source and args.local_source.exists():
        evidence_dir = args.out / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        for page_no in pages:
            try:
                ref = render_evidence_crop(
                    args.local_source,
                    page_number=page_no,
                    bbox=(40.0, 40.0, 555.0, 760.0),
                    dpi=200,
                    output_path=evidence_dir / f"page_{page_no:03d}.png",
                    document_id=document_id,
                )
                crops.append(
                    {
                        "page": page_no,
                        "artifact_ref": ref.artifact_ref,
                        "pixel_width": ref.pixel_width,
                        "pixel_height": ref.pixel_height,
                    }
                )
            except Exception as exc:  # rendering is best-effort
                crops.append({"page": page_no, "error": str(exc)})

    report_path = args.report or (args.out / "report.md")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(artifacts.manifest_path.read_text(encoding="utf-8"))
    lines = [
        "# MinerU V1 Live Report",
        "",
        f"- **document_id**: `{document_id}`",
        f"- **local_source**: `{args.local_source}`",
        f"- **source_url**: `{args.source_url}`",
        f"- **model_version**: `{args.model_version}`",
        f"- **pages requested**: `{list(pages)}`",
        f"- **pages produced**: `{manifest['page_coverage']['produced']}`",
        f"- **remote_task_id**: `{manifest['remote_task_id']}`",
        f"- **elapsed (s)**: {elapsed:.1f}",
        f"- **artifacts** (under `{args.out.relative_to(ROOT) if args.out.is_absolute() else args.out}`):",
        f"  - normalized: `{artifacts.normalized_path}`",
        f"  - raw_response: `{artifacts.raw_response_path}`",
        f"  - manifest: `{artifacts.manifest_path}`",
    ]
    if crops:
        lines += ["", "## Evidence crops", ""]
        for crop in crops:
            if "error" in crop:
                lines.append(f"- page {crop['page']}: error {crop['error']}")
            else:
                lines.append(
                    f"- page {crop['page']}: {Path(crop['artifact_ref']).name} "
                    f"({crop['pixel_width']}x{crop['pixel_height']}px)"
                )
    lines += [
        "",
        "> No accuracy claim: 1-3 page smoke run. Auto-accept remains off.",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[run_mineru_live] wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())