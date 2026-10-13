"""Bio PDF workflow package.

The package is split into submodules that mirror the implementation plan:

- :mod:`bio_pdf.contracts` — language-neutral Pydantic models and JSON
  schema export.
- :mod:`bio_pdf.errors` — domain error codes used by the CLI, API and
  downstream layers.
- :mod:`bio_pdf.ingest` — input validation, file inventory, attachments.
- :mod:`bio_pdf.storage` — immutable artifacts, SQLite repository, cache
  keys.
- :mod:`bio_pdf.pdf` — native PDF parsing primitives.
- :mod:`bio_pdf.parsers` — adapter contracts, Docling, MinerU.
- :mod:`bio_pdf.extract` — sequence and activity domain rules (M1B).
- :mod:`bio_pdf.workflow` — state-machine runner (M1B).
- :mod:`bio_pdf.export` — JSON/CSV/HTML exporters (M1B).
- :mod:`bio_pdf.review` — acceptance, plan, revision (M1B).
- :mod:`bio_pdf.evaluation` — annotation, matching, metrics (M1C).
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"