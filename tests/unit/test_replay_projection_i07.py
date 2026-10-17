"""I07/P03: the replay projection must use the SAME source-page mapping as
the size check — no subset-index page numbers, no default-A4 geometry that
still claims verified.

Synthetic zips point at NON-A4 pages and a non-contiguous submitted set so
the mapping and the paper size are provable from the output itself.
"""

from __future__ import annotations

import importlib.util
import io
import json
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "regen_cloud_replay.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("regen_cloud_replay_i07", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _pdf(path: Path, pages: int, size: tuple[float, float]) -> Path:
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=size)
    for _ in range(pages):
        c.drawString(10, 10, "x")
        c.showPage()
    c.save()
    return path


def _zip(path: Path, *, content_items: list[dict], layout_pages: list[dict]) -> Path:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("x_content_list.json", json.dumps(content_items))
        zf.writestr("layout.json", json.dumps({"pdf_info": layout_pages}))
        zf.writestr("full.md", "# x")
    path.write_bytes(buf.getvalue())
    return path


_CONTENT = [
    {"type": "text", "text": "Hello", "page_idx": 0, "bbox": [100, 100, 900, 900]},
    {"type": "text", "text": "World", "page_idx": 1, "bbox": [50, 50, 400, 600]},
]


def test_projection_uses_submitted_source_pages_and_real_paper(tmp_path: Path) -> None:
    module = _load_script()
    pdf = _pdf(tmp_path / "small.pdf", pages=5, size=(500.0, 700.0))
    zip_path = _zip(
        tmp_path / "r.zip",
        content_items=_CONTENT,
        layout_pages=[
            {"page_idx": 0, "page_size": [500, 700]},
            {"page_idx": 1, "page_size": [500, 700]},
        ],
    )
    report = module.replay_zip(zip_path, pdf, [2, 3], strict=True)
    # I07: content idx 0/1 maps to the SUBMITTED pages 2/3 — not 1/2.
    assert report["pages_produced"] == [2, 3]
    assert report["projection_page_order"] == [2, 3]
    assert report["projection_page_sizes"] == {
        "2": [500.0, 700.0, 0.0],
        "3": [500.0, 700.0, 0.0],
    }
    assert report["all_sizes_match"] is True
    assert report["coordinate_basis_verified"] is True
    assert report["pages_without_live_size"] == []
    # Converted boxes live in the REAL 500x700 frame (an A4 fallback would
    # scale to 842 and could exceed 700).
    assert report["max_abs_bbox_extent"] is not None
    assert report["max_abs_bbox_extent"] <= 700 + 2


def test_missing_layout_size_is_never_marked_verified(tmp_path: Path) -> None:
    """Strict mode REFUSES (J06); diagnostic mode continues with the page
    unverified — never a fabricated A4 frame."""

    import pytest as _pytest

    module = _load_script()
    pdf = _pdf(tmp_path / "small.pdf", pages=5, size=(500.0, 700.0))
    zip_path = _zip(
        tmp_path / "r.zip",
        content_items=_CONTENT,
        layout_pages=[{"page_idx": 0, "page_size": [500, 700]}],  # page 3 missing
    )
    with _pytest.raises(module.ReplayGeometryError):
        module.replay_zip(zip_path, pdf, [2, 3], strict=True)

    report = module.replay_zip(zip_path, pdf, [2, 3], strict=False)
    assert report["mode"] == "diagnostic"
    assert report["all_sizes_match"] is False
    assert report["pages_without_live_size"] == [3]
    assert report["mismatched_pages"] == [3]
    assert report["source_geometry_verified"] is False
    assert report["coordinate_basis_verified"] is False, "an unknown size must not verify"
    assert report["blocks_without_verified_frame"] >= 1, "page-3 boxes keep raw + unverified"
    assert report["projection_page_sizes"] == {"2": [500.0, 700.0, 0.0]}
    # Both trust axes are reported separately, with hashes kept for audit.
    assert report["coordinate_unit_known"] is True
    assert report["hashes"]["source_pdf_sha256"]
    assert report["hashes"]["layout_sha256"]


def test_fixed_zips_replay_with_their_real_source_pages() -> None:
    """The two committed cloud zips: projection order and sizes must be the
    REAL submitted pages (EP 30–33, paper 8–9), not subset indices."""

    module = _load_script()
    for target, pages, height in (
        ("ep", [30, 31, 32, 33], 842.0),
        ("paper", [8, 9], 779.0),
    ):
        data = REPO / "data" / f"acceptance-g-live-{target}" / "remote-result.zip"
        if not data.is_file():
            continue
        spec = module.TARGETS[target]
        report = module.replay_zip(
            data,
            spec["pdf"],
            spec["source_pages"],
            expected_zip_sha256=spec["expected_zip_sha256"],
            strict=True,
        )
        assert report["zip_sha256_matches"] is True
        assert report["projection_page_order"] == pages
        assert set(report["projection_page_sizes"]) == {str(p) for p in pages}
        for entry in report["projection_page_sizes"].values():
            assert abs(entry[1] - height) <= 2, entry
        assert report["coordinate_basis_verified"] is True
        assert report["max_abs_bbox_extent"] is None or report["max_abs_bbox_extent"] <= height + 2


def test_size_mismatch_is_refused_strict_and_downgraded_in_diagnostic(tmp_path: Path) -> None:
    """J06 recheck repro: source PDF 500x700 but layout claims 595x842 —
    strict mode refuses; diagnostic mode keeps the page's coordinates RAW
    and unverified (the converted box would exceed the real page)."""

    import pytest as _pytest

    module = _load_script()
    pdf = _pdf(tmp_path / "small.pdf", pages=5, size=(500.0, 700.0))
    zip_path = _zip(
        tmp_path / "r.zip",
        content_items=[{"type": "text", "text": "Hello", "page_idx": 0, "bbox": [100, 100, 900, 900]}],
        layout_pages=[{"page_idx": 0, "page_size": [595, 842]}],
    )
    with _pytest.raises(module.ReplayGeometryError) as excinfo:
        module.replay_zip(zip_path, pdf, [1], strict=True)
    assert "height" in str(excinfo.value), str(excinfo.value)

    report = module.replay_zip(zip_path, pdf, [1], strict=False)
    assert report["all_sizes_match"] is False
    assert report["mismatched_pages"] == [1]
    assert report["source_geometry_verified"] is False
    assert report["coordinate_basis_verified"] is False
    # The mismatched frame never enters the projection mapping…
    assert report["projection_page_sizes"] == {}
    assert report["pages_without_live_size"] == [1]
    # …so the block keeps RAW coordinates (0-1000 protocol values), flagged
    # unverified — not silently converted into a wrong-size frame.
    assert report["blocks_without_verified_frame"] >= 1
    assert report["max_abs_bbox_extent"] == 900.0
    reasons = report["layout_page_size_check"]["1"]["reasons"]
    assert any("height" in r for r in reasons) and any("width" in r for r in reasons)


def test_rotation_mismatch_is_recorded_and_blocks_trust(tmp_path: Path) -> None:
    """J06: rotation is checked separately — a layout page_angle that
    contradicts the source rotation cannot verify the frame."""

    import pytest as _pytest

    module = _load_script()
    pdf = _pdf(tmp_path / "small.pdf", pages=5, size=(500.0, 700.0))
    zip_path = _zip(
        tmp_path / "r.zip",
        content_items=[{"type": "text", "text": "Hello", "page_idx": 0, "bbox": [100, 100, 900, 900]}],
        layout_pages=[{"page_idx": 0, "page_size": [500, 700], "page_angle": 90}],
    )
    with _pytest.raises(module.ReplayGeometryError):
        module.replay_zip(zip_path, pdf, [1], strict=True)
    report = module.replay_zip(zip_path, pdf, [1], strict=False)
    entry = report["layout_page_size_check"]["1"]
    assert entry["width_match"] is True and entry["height_match"] is True
    assert entry["rotation_match"] is False
    assert entry["source_pdf_rotation_deg"] == 0.0
    assert report["coordinate_basis_verified"] is False


def test_tolerance_boundary_is_respected(tmp_path: Path) -> None:
    """±2 pt is accepted; beyond it is not (boundary probe, not a snapshot)."""

    import pytest as _pytest

    module = _load_script()
    pdf = _pdf(tmp_path / "b.pdf", pages=3, size=(500.0, 700.0))
    within = _zip(
        tmp_path / "within.zip",
        content_items=[{"type": "text", "text": "x", "page_idx": 0, "bbox": [100, 100, 900, 900]}],
        layout_pages=[{"page_idx": 0, "page_size": [501.5, 701.5]}],
    )
    report = module.replay_zip(within, pdf, [1], strict=True)
    assert report["all_sizes_match"] is True

    beyond = _zip(
        tmp_path / "beyond.zip",
        content_items=[{"type": "text", "text": "x", "page_idx": 0, "bbox": [100, 100, 900, 900]}],
        layout_pages=[{"page_idx": 0, "page_size": [503.0, 700.0]}],
    )
    with _pytest.raises(module.ReplayGeometryError):
        module.replay_zip(beyond, pdf, [1], strict=True)
