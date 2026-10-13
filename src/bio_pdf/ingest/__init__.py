"""Input ingestion: file inspection, page selection, attachment binding.

The ingestion layer is the only place that reads raw file bytes. Everything
downstream operates on the inspection result or selected page tuples.
"""

from bio_pdf.ingest.inspector import (
    inspect_pdf,
    inspect_directory,
    parse_page_selection,
    build_inventory_report,
)

__all__ = [
    "inspect_pdf",
    "inspect_directory",
    "parse_page_selection",
    "build_inventory_report",
]