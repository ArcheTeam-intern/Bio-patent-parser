# Bio PDF Workflow

Auditable, recoverable Python demo for extracting biological sequences and
activity evidence from scientific papers and patent PDFs.

The first phase (M1A) builds the project skeleton: contracts, ingestion,
storage, native PDF parsing and a live Docling adapter. Domain rules
(sequences, activities, relations, review) land in M1B and a baseline
human-in-the-loop benchmark in M1C.

See `docs/superpowers/specs/2026-10-13-bio-pdf-workflow-design.md` for the
full architecture and `docs/superpowers/plans/2026-10-13-bio-pdf-workflow-implementation.md`
for the phased execution plan.

## Quick start

```bash
# Python 3.12 required (matches core extra).
uv venv --python 3.12
uv sync --extra dev

# Run a non-destructive environment probe.
uv run bio-pdf doctor

# Show the CLI surface.
uv run bio-pdf --help
```

Heavy parser integrations (Docling, MinerU, LLM providers) are separate
optional extras:

```bash
uv sync --extra docling   # installs the real Docling adapter
uv sync --extra mineru    # installs the HTTP adapter for MinerU V1
uv sync --extra llm       # installs the HTTP LLM provider
```

## Layout

```
src/bio_pdf/        # business package (CLI, contracts, ingestion, parsers, …)
configs/            # TOML profiles (e.g. demo.toml)
tests/              # pytest tree (unit/, contracts/, integration/, fixtures/)
docs/superpowers/   # design specs and implementation plans
research/           # corpus inventory and parser comparisons
pdf/                # original corpus (NOT committed)
docling/            # upstream reference checkout (NOT committed)
data/               # local SQLite and artifact cache (NOT committed)
reports/            # generated reports and benchmark outputs
```

## Status

This is an early skeleton. The core CLI, contracts, ingestion, native parser,
storage and Docling adapter are exercised by pytest. Real-sample benchmarks
and downstream domain rules arrive in the next milestones.