"""Regenerate the cloud replay evidence JSONs from the FIXED zip archives.

H09 + I07: the earlier one-off script checked layout page sizes by source
page, but built the actual projection with SUBSET indices (1/2) and fell
back to a default A4 paper for missing sizes — the numeric check and the
projection lived in different namespaces, and a default page could still be
reported as verified. This version:

- maps content_list ``page_idx`` through the SUBMITTED source-page order
  (``source_pages``) for BOTH the size check and the projection;
- passes ONLY real layout sizes to the projection — a missing size keeps
  the page unverified (no fabricated A4 geometry);
- verifies the zip's SHA-256 before replaying;
- records the exact page order/sizes used by the projection so the
  machine-readable evidence matches what actually ran.

No network, no credentials.

Usage: python scripts/regen_cloud_replay.py
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

TOLERANCE_PT = 2.0

TARGETS = {
    "ep": {
        "data_dir": REPO / "data" / "acceptance-g-live-ep",
        "pdf": REPO / "pdf" / "patent_EP3027649B1_TNFa_IL17_bispecific_antibodies.pdf",
        "source_pages": [30, 31, 32, 33],
        "expected_zip_sha256": "f42bbb7a684d814ea255495a9f196197c2dee2921bb1591310f00c4fe465f930",
    },
    "paper": {
        "data_dir": REPO / "data" / "acceptance-g-live-paper",
        "pdf": REPO / "pdf" / "paper_TAT-RasGAP_broad_antimicrobial_2017.pdf",
        "source_pages": [8, 9],
        "expected_zip_sha256": "50703ac6669a2b1696e1f691e7e88af103259ec37b2b62e4d0dd50dfda21d1dd",
    },
}


def replay_zip(
    zip_path: Path,
    pdf_path: Path,
    source_pages: list[int],
    *,
    expected_zip_sha256: str | None = None,
) -> dict:
    """Replay one fixed zip through the product projection + domain pass.

    ``source_pages`` is the SUBMITTED page order: content_list ``page_idx i``
    corresponds to ``source_pages[i]``. The same mapping is used for the
    layout size check AND the projection — never a subset-index shortcut.
    """

    zip_bytes = zip_path.read_bytes()
    zip_sha = hashlib.sha256(zip_bytes).hexdigest()

    import pypdfium2 as pdfium

    from bio_pdf.application.comparison_service import _domain_pass_spec
    from bio_pdf.parsers.mineru_adapter import (
        _page_geometry_descriptor,
        _project_mineru_payload,
    )
    from bio_pdf.parsers.normalize_docling import normalize_docling_document

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        content_name = next(m for m in zf.namelist() if m.endswith("_content_list.json"))
        content_list = json.loads(zf.read(content_name).decode("utf-8"))
        layout = json.loads(zf.read("layout.json").decode("utf-8"))

    # --- I07: ONE shared subset-index -> source-page mapping ----------------
    layout_sizes: dict[int, tuple[float, float, float]] = {}
    for page in layout.get("pdf_info") or []:
        try:
            idx = int(page.get("page_idx"))
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(source_pages)):
            continue
        source_no = source_pages[idx]
        size = page.get("page_size")
        if not size or len(size) != 2:
            continue
        layout_sizes[source_no] = (
            float(size[0]),
            float(size[1]),
            float(page.get("page_angle") or 0),
        )

    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        source_sizes = {p: doc[p - 1].get_size() for p in source_pages}
    finally:
        doc.close()

    size_check: dict[str, dict] = {}
    for source_no in source_pages:
        layout_size = layout_sizes.get(source_no)
        source = source_sizes[source_no]
        match = bool(
            layout_size
            and abs(layout_size[0] - source[0]) <= TOLERANCE_PT
            and abs(layout_size[1] - source[1]) <= TOLERANCE_PT
        )
        size_check[str(source_no)] = {
            "layout_json_page_size": [layout_size[0], layout_size[1]] if layout_size else None,
            "layout_json_page_angle": layout_size[2] if layout_size else None,
            "source_pdf_size_pt": [round(source[0], 2), round(source[1], 2)],
            "tolerance_pt": TOLERANCE_PT,
            "match": match,
        }

    # The projection receives the SUBMITTED order and ONLY real sizes — a
    # page without a layout size stays unverified instead of inheriting A4.
    page_order = list(source_pages)
    projection_page_sizes = {
        page_no: layout_sizes[page_no]
        for page_no in source_pages
        if page_no in layout_sizes
    }
    projected = _project_mineru_payload(
        {"content_list": content_list},
        page_numbers=set(page_order),
        page_order=page_order,
        page_sizes=projection_page_sizes,
    )
    warnings = projected.pop("_projection_warnings", [])
    basis = projected.pop("_coordinate_basis", None)
    document = normalize_docling_document(projected, parser_run_id="run", document_id="doc")
    descriptor = _page_geometry_descriptor(
        document,
        page_sizes=projection_page_sizes,
        declared_basis=basis,
        projection_warnings=warnings,
    )
    produced_pages = [page.page_number for page in document.pages]
    pages_without_live_size = [
        page_no for page_no in produced_pages if page_no not in projection_page_sizes
    ]
    blocks_without_verified_frame = sum(
        1
        for page in document.pages
        for block in page.blocks
        if getattr(block, "bbox_verified", None) is False
    )
    summary = _domain_pass_spec(
        json.loads(document.model_dump_json()), document_id="doc", engine="mineru_http"
    )
    sequences = {str(s["seq_id_no"]): s["normalized_sequence"] for s in summary["sequences"]}
    bbox_extents = [
        round(max(abs(v) for v in block.bbox), 2)
        for page in document.pages
        for block in page.blocks
        if block.bbox
    ]
    sha_ok = expected_zip_sha256 is None or zip_sha == expected_zip_sha256
    return {
        "zip_path": str(zip_path),
        "zip_sha256": zip_sha,
        "expected_zip_sha256": expected_zip_sha256,
        "zip_sha256_matches": sha_ok,
        "zip_bytes": len(zip_bytes),
        "layout_page_size_field": "layout.json -> pdf_info[].page_size (idx -> submitted page)",
        "source_pages_submitted": list(source_pages),
        "layout_page_size_check": size_check,
        "all_sizes_match": all(v["match"] for v in size_check.values()),
        "projection_page_order": page_order,
        "projection_page_sizes": {
            str(page_no): [size[0], size[1], size[2]]
            for page_no, size in projection_page_sizes.items()
        },
        "pages_produced": produced_pages,
        "pages_without_live_size": pages_without_live_size,
        "blocks_without_verified_frame": blocks_without_verified_frame,
        "max_abs_bbox_extent": max(bbox_extents, default=None),
        "coordinate_basis": basis,
        "coordinate_basis_verified": descriptor["coordinate_basis_verified"],
        "page_size_verified": descriptor["page_size_verified"],
        "projection_warnings": warnings[:10],
        "sequence_count": len(sequences),
        "sequences": sequences,
        "measurement_count": len(summary["measurements"]),
        "note": (
            "remote parsed OUR submission; the local plain-HTTPS download path failed "
            "(CDN TLS EOF); the zip was fetched with TLS verification ON via a temporary "
            "DoH-resolved connection and replayed through the product projection"
        ),
    }


def main() -> int:
    for target, spec in TARGETS.items():
        zip_path = spec["data_dir"] / "remote-result.zip"
        if not zip_path.is_file():
            print(json.dumps({"target": target, "error": "zip missing"}))
            continue
        report = {
            "target": target,
            "data_rel": str(zip_path.relative_to(REPO)),
            **replay_zip(
                zip_path,
                spec["pdf"],
                spec["source_pages"],
                expected_zip_sha256=spec["expected_zip_sha256"],
            ),
        }
        report["zip_path"] = str(Path(report["zip_path"]).relative_to(REPO))
        out = REPO / "reports" / "f-round-recheck" / f"real_cloud_g_{target}_replay.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "target": target,
                    "zip_sha256_matches": report["zip_sha256_matches"],
                    "all_sizes_match": report["all_sizes_match"],
                    "projection_page_order": report["projection_page_order"],
                    "coordinate_basis_verified": report["coordinate_basis_verified"],
                    "sequences": report["sequence_count"],
                    "measurements": report["measurement_count"],
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
