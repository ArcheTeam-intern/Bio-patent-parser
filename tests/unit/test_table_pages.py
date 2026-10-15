"""table_pages for the records overview (tables-tab distribution)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from bio_pdf.api.routes.results import _table_pages


def _service(root: Path):
    return SimpleNamespace(paths=SimpleNamespace(artifact_root=root))


def test_table_pages_counts_tables_per_page(tmp_path: Path) -> None:
    job = "job-t1"
    out = tmp_path / "jobs" / job
    out.mkdir(parents=True)
    (out / "normalized.json").write_text(
        json.dumps(
            {
                "pages": [
                    {"page_number": 3, "tables": [{}, {}]},
                    {"page_number": 9, "tables": [{}]},
                    {"page_number": 12, "tables": []},
                ]
            }
        ),
        encoding="utf-8",
    )
    pages = _table_pages(_service(tmp_path), job)
    assert pages == {3: 2, 9: 1}


def test_table_pages_degrades_to_empty_without_normalized(tmp_path: Path) -> None:
    # Missing/unreadable normalized output is an honest empty map, not an
    # error — the records response must keep working.
    assert _table_pages(_service(tmp_path), "job-none") == {}
