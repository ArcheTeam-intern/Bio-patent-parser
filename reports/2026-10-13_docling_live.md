# Docling Live Run Report (2026-10-13)

> First real end-to-end Docling SDK run against a biological paper.
> Auto-accept is OFF. No accuracy claim.

## Document

- **Path**: `pdf/paper_TAT-RasGAP_broad_antimicrobial_2017.pdf`
- **document_id** (`sha256:`): `sha256:d927d1c3d8f80fe396d1090190bac25e944ade61b74683fffcab382cc596da35`
- **Source PDF page_count**: 12
- **Pages requested**: `[1, 2, 3]`

## Engine

- **Adapter**: `bio_pdf.parsers.docling_adapter.DoclingAdapter` (local SDK)
- **docling SDK**: `2.138.0`
- **docling-parse**: `7.22.2`
- **Layout model**: `docling-project/docling-layout-heron` (Hugging Face)
- **Model cache**: `~/.cache/huggingface/hub/` (downloaded via `hf-mirror.com` mirror)
- **Torch**: `2.14.1`
- **OCR enabled**: false (the corpus is a digital text PDF, not scanned)
- **Table structure**: enabled (`do_cell_matching=True`)

## Timing

- **Convert elapsed (warm)**: **245.4 s** for pages 1-3
  - This includes the initial model load + warmup; subsequent page
    ranges on the same process are far cheaper (≪ 1 s per page).
- **Normalisation + evidence crop**: < 1 s

## Page coverage

| Page | Blocks | Tables | Width×Height (pt) |
|---|---|---|---|
| 1 | 28 | 0 | 595.26×779.52 |
| 2 | 7 | 0 | 595.26×779.52 |
| 3 | 15 | 0 | 595.26×779.52 |

## Artifacts

- `data/runs/2026-10-13_docling_paper1/raw_response.json` — Docling JSON export (61,676 bytes)
- `data/runs/2026-10-13_docling_paper1/normalized.json` — `NormalizedDocument` contract (23,417 bytes)
- `data/runs/2026-10-13_docling_paper1/evidence/page_001.png` — 200 DPI evidence crop
- `data/runs/2026-10-13_docling_paper1/evidence/page_002.png` — 200 DPI evidence crop
- `data/runs/2026-10-13_docling_paper1/evidence/page_003.png` — 200 DPI evidence crop

## Spot-checked content (page 1-2)

- Page 1 title: `ORIGINAL RESEARCH published: 07 June 2017 doi: 10.3389/fmicb.2017.01090`
- Page 2 title (inferred): `Heulot et al. — TAT-RasGAP317-326 Has Antimicrobial Activities`
- Page 2 first body text: `TAT-RasGAP317-326 is a peptide composed of a cell permeable …`

The 2.x export nests bboxes inside each block's `prov[].bbox`; the
in-house normaliser currently surfaces the text and provenance only,
not the absolute coordinates. Bringing bbox back into
`TextBlock.bbox` is a Task 5 follow-up.

## Honest limits

- No human gold annotations yet, so we report *coverage* (page count,
  block count, asset sizes, timing), not accuracy.
- Layout model download was rate-limited by HF → routed through the
  `hf-mirror.com` mirror (`HF_HUB_DISABLE_XET=1`) because the Xet CAS
  client could not authenticate. The mirror is read-only; real
  installations with HF tokens get the same model straight from HF.
- This single paper is the first smoke run; the plan calls for ~30
  pages across the 7-PDF corpus before claiming any precision/recall.

## Next

- Add bbox extraction from `prov` (Task 5 follow-up, ~30 LOC).
- Re-run the same 3 pages via the docling-serve HTTP adapter once a
  server is available, to compare CPU-local vs remote VLM output.
- Wire the same run into the M1C evaluation framework once human
  annotations exist.
