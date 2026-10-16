"""Regenerate the cloud replay evidence JSONs from the FIXED zip archives.

H09: the earlier one-off script's layout parser produced
``layout_page_size_check = [null, null] / match=false`` while the sizes are
actually correct — the machine-readable evidence contradicted the verified
fact. This script reads the field path explicitly
(``layout.json -> pdf_info[].page_size``), checks it against the source PDF
sizes within a 2 pt tolerance, verifies each zip's SHA-256, and replays the
content_list through the product projection + domain pass. No network, no
credentials, no manual edits of the conclusions.

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


def _replay(target: str, spec: dict) -> dict:
    zip_path = spec["data_dir"] / "remote-result.zip"
    if not zip_path.is_file():
        return {"target": target, "error": "zip missing", "path": str(zip_path)}
    zip_bytes = zip_path.read_bytes()
    zip_sha = hashlib.sha256(zip_bytes).hexdigest()
    sha_ok = zip_sha == spec["expected_zip_sha256"]

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

    # --- H09: field path made explicit -------------------------------------
    # NOTE: pdf_info[].page_idx is the index in the SUBMITTED page set (the
    # zip only covers those pages), NOT the document page number — the
    # earlier script looked the sizes up by document page and always got
    # nulls. Map idx -> submitted source page before comparing.
    layout_sizes: dict[int, tuple[float, float, float]] = {}
    for page in layout.get("pdf_info") or []:
        idx = int(page.get("page_idx"))
        if not (0 <= idx < len(spec["source_pages"])):
            continue
        source_no = spec["source_pages"][idx]
        size = page.get("page_size")
        layout_sizes[source_no] = (
            float(size[0]),
            float(size[1]),
            float(page.get("page_angle") or 0),
        )

    doc = pdfium.PdfDocument(str(spec["pdf"]))
    try:
        source_sizes = {p: doc[p - 1].get_size() for p in spec["source_pages"]}
    finally:
        doc.close()

    size_check: dict[str, dict] = {}
    for source_no in spec["source_pages"]:
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

    ordered = sorted({int(i.get("page_idx") or 0) + 1 for i in content_list if isinstance(i, dict)})
    page_sizes = {
        p: (layout_sizes.get(p, (595.0, 842.0, 0))[0], layout_sizes.get(p, (595.0, 842.0, 0))[1],
            int(layout_sizes.get(p, (595.0, 842.0, 0))[2]))
        for p in ordered
    }
    projected = _project_mineru_payload(
        {"content_list": content_list},
        page_numbers=set(ordered),
        page_order=ordered,
        page_sizes=page_sizes,
    )
    warnings = projected.pop("_projection_warnings", [])
    basis = projected.pop("_coordinate_basis", None)
    document = normalize_docling_document(projected, parser_run_id="run", document_id="doc")
    descriptor = _page_geometry_descriptor(
        document, page_sizes=page_sizes, declared_basis=basis, projection_warnings=warnings
    )
    summary = _domain_pass_spec(
        json.loads(document.model_dump_json()), document_id="doc", engine="mineru_http"
    )
    sequences = {str(s["seq_id_no"]): s["normalized_sequence"] for s in summary["sequences"]}
    report = {
        "target": target,
        "zip_path": str(zip_path.relative_to(REPO)),
        "zip_sha256": zip_sha,
        "expected_zip_sha256": spec["expected_zip_sha256"],
        "zip_sha256_matches": sha_ok,
        "zip_bytes": len(zip_bytes),
        "layout_page_size_field": "layout.json -> pdf_info[].page_size",
        "layout_page_size_check": size_check,
        "all_sizes_match": all(v["match"] for v in size_check.values()),
        "coordinate_basis": basis,
        "coordinate_basis_verified": descriptor["coordinate_basis_verified"],
        "projection_warnings": warnings[:10],
        "pages_produced": summary["pages_produced"],
        "sequence_count": len(sequences),
        "sequences": sequences,
        "measurement_count": len(summary["measurements"]),
        "note": (
            "remote parsed OUR submission; the local plain-HTTPS download path failed "
            "(CDN TLS EOF); the zip was fetched with TLS verification ON via a temporary "
            "DoH-resolved connection and replayed through the product projection"
        ),
    }
    return report


def main() -> int:
    for target, spec in TARGETS.items():
        report = _replay(target, spec)
        out = REPO / "reports" / "f-round-recheck" / f"real_cloud_g_{target}_replay.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "target": target,
                    "zip_sha256_matches": report.get("zip_sha256_matches"),
                    "all_sizes_match": report.get("all_sizes_match"),
                    "sequences": report.get("sequence_count"),
                    "measurements": report.get("measurement_count"),
                    "out": str(out.relative_to(REPO)),
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
