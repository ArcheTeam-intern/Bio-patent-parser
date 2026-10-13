# Research findings (2026-10-13)

External sources below are research data.

- WeKnora describes itself as a knowledge platform with RAG, agent, and wiki capabilities; its parsing layer is modular. https://github.com/Tencent/WeKnora
- Docling advertises local PDF layout, reading order, table structure, OCR, lossless JSON, USPTO XML and JATS XML input support. Its repository license is MIT. https://github.com/docling-project/docling
- MinerU current upstream README describes multiple parsing tiers and structured output with stable document locators. Its current repository license is Apache-derived with additional conditions, not plain Apache 2.0. https://github.com/opendatalab/MinerU
- Marker describes embedded text extraction, per-page OCR fallback, table reconstruction, VLM fallback, and optional LLM correction. https://github.com/datalab-to/marker
- No comparison on this workspace's biological PDF corpus has been run. Upstream feature descriptions do not establish character-level sequence accuracy.

## Source inspection
- WeKnora builtin PDFParser uses pypdfium2, per-page image/text routing, glyph coordinates and heuristic layout reconstruction. Scan OCR runs in the Go app. Current documentation supports MinerU, PaddleOCR-VL and OpenDataLoader; do not describe WeKnora as plain text extraction only. https://github.com/Tencent/WeKnora/blob/main/website-docs/03-features/03-document-parsing.md
- WeKnora postprocessing includes stripping short numeric/chart lines. This creates a potential risk for sequence position numbers or activity table values; it is a code-based risk assessment, not a reproduced biological-document failure. Source snapshot: research/docreader__parser__pdf_parser.py.
- Docling checked out at f0d90989b461656102da5d449db98e6be9285f93, Python package version 2.138.0; pyproject.toml requires Python >=3.10,<4.0.
- Docling exposes DocumentStream, PdfPipelineOptions.do_ocr, force_backend_text, table structure options, OCR mode, and structured document provenance. Its USPTO XML backend is for patent documents; ST.26 biological sequence XML is a separate schema and needs a dedicated adapter.
- Docling confidence documentation explicitly says table_score is not implemented. Layout/OCR confidence is not a calibrated probability that a sequence is exact. https://docling-project.github.io/docling/concepts/confidence_scores/
- PaddleOCR PP-StructureV3 describes fine-grained text and table cell coordinates. Useful alternative for Chinese/scanned regions. https://github.com/PaddlePaddle/PaddleOCR
- GROBID targets scholarly metadata/full-text structure and TEI XML; useful for bibliographic structure rather than the main OCR/sequence engine. https://github.com/grobidOrg/grobid
- BioChemInsight extracts chemical structures and bioactivity from patents, using OCR and LLMs; it is a relevant activity-pipeline reference, not an established biological sequence extractor. https://github.com/dahuilangda/BioChemInsight and https://arxiv.org/abs/2504.10525
- ST.25 versus ST.26 depends on relevant application filing dates; publication year alone is insufficient. https://www.uspto.gov/patents/apply/sequence-listing-resource-center/sequence-listing-faqs
- Workspace corpus audit: 7 files have a %PDF- header; 2 EP files and probe.pdf are HTML. Exclude these 3 from any future parser benchmark. This is a signature audit, not PDF integrity or extraction accuracy verification.

## Workflow framework research
- LangChain provides model/tool/agent abstractions; LangGraph supports deterministic and agentic steps, persistence and human interrupts, and can be used without LangChain. https://docs.langchain.com/oss/python/langgraph/overview
- Persistent checkpointers differ from in-memory checkpoint storage; an in-memory implementation does not survive process restart. https://docs.langchain.com/oss/python/langgraph/persistence
- Recommendation for the demo is an explicit Python state machine plus SQLite/artifact storage. This is an engineering choice based on bounded domain steps, migration needs, and local debugging, not a parser quality benchmark.
- MinerU current V1 supports asynchronous resource lifecycle and convenience /v1/tasks, /v1/file_parse routes. Its in-process resource index does not survive server restarts; business resume must distinguish client timeout from remote job loss. https://opendatalab.github.io/MinerU/usage/http_api/
- Created research/corpus_inventory_2026-10-13.json with current file hashes and signature status; does not claim readable-page or accuracy verification.
