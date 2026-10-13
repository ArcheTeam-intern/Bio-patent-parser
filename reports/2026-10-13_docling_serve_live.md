# Docling Serve (community) Live Run Report — 2026-10-13

> Real end-to-end parse of a biological paper against a community-hosted
> `docling-serve` instance. Smoke run; **no accuracy claim**, auto-accept off.

## Service

- **Base URL**: `https://bibyutatsu-hf-docling-serve.hf.space`
- **Server**: community `docling-serve` v1.13.0 (official Docling Docker image on HuggingFace Spaces)
- **Source**: [Bibyutatsu/HF-docling-serve](https://huggingface.co/spaces/Bibyutatsu/HF-docling-serve)
- **Rate limits**: free tier (~2 req/min on heavy endpoints)

## Document

- **Path**: `pdf/paper_TAT-RasGAP_broad_antimicrobial_2017.pdf`
- **document_id**: `sha256:d927d1c3d8f80fe396d1090190bac25e944ade61b74683fffcab382cc596da35`
- **Pages requested**: `[1, 2]`
- **Pages returned**: `12` (the community `/v1/convert/file/async` does not
  honour a page-range restriction, so the whole 12-page PDF came back — the
  normaliser kept all 12; a page-range follow-up is a Task 5 refinement)

## Timing

- **Elapsed**: **130.8 s** (includes HF Space cold start + upload + parse)

## Spot-checked content

- Page 1: `ORIGINAL RESEARCH published: 07 June 2017 doi: 10.3389/fmicb.2017.0099`
- Page 2: `Heulot et al. — TAT-RasGAP 317-326 Has Antimicrobial Activities`

## Artifacts

- `data/runs/2026-10-13_docling_serve_paper1/raw_response.json`
- `data/runs/2026-10-13_docling_serve_paper1/normalized.json` (12 pages)
- `data/runs/2026-10-13_docling_serve_paper1/manifest.json`

## What this proves

The business-side HTTP client
([`DoclingServeAdapter`](../../src/bio_pdf/parsers/docling_serve_adapter.py))
works against a real docling-serve instance — upload → submit → poll →
download → unwrap `{document → ConvertDocumentResponse → json_content}` →
normalise to the in-house `NormalizedDocument` contract. The same adapter is
used for a self-hosted `docling-serve` (default `http://localhost:5001`).

## Honest limits

- No human gold; coverage/timing reported, not accuracy.
- The community instance is rate-limited and returns the full document
  rather than a page subset.
- Docling's bbox still lives in each block's `prov[]`; surfaced as text
  only by the current normaliser (Task 5 follow-up).
