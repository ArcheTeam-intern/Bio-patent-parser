# Research findings (2026-10-13)

## M1执行后核查（HEAD cab949a）
- 新job-95b61e00997d当前发布制品为5条sequence、20条measurement、47条evidence，报告70条不一致，extract SHA8784eae6eb3999f6c65675f353d684ae4678b17cbc033455b0b18e84b41e2a4e。
- 两真实短序列每条在第9页重复2次（Docling段落与native行）；报告说摘要与正文两处来源不符当前制品。
- 新normalized有229个重复block ID，native:rN跨页重复，最多12次；证据hash不包含page，存在跨页碰撞风险。
- span_from_block_match把整块bbox+字符offset标character，不等于字符级图像区域；两短序列Docling候选共享695字符方法段bbox。
- 前端完全未接getEvidence，继续从opaque hash ID反解page，定位回归；M1最小前端部分缺失。
- 20条metric全部unknown且只带value证据；Figure/table关键词整句判metadata、mean±整句判statistic、less/at least整句判claim，可能丢真实结果或错分类。
- 解析计划有OCR页时如果不是全部页都需OCR，仍运行layout_native；parser options未进入adapter真实执行，docling_serve/mineru也走DoclingSDK实现。
- runner缓存未核验stage manifest自身hash；lease guard只看心跳线程的lost标志，阶段结束/验证写review和publish没有条件事务fencing。
- export POST仍记入队revision而后台build重取current；报告固定快照已完成不成立。
- T09表格metric guard选择全表单一指标然后覆盖所有列，MIC/MBC混合表必有错列风险；table record证据ID与span_from_cell哈希ID不一致。
- 351 passed、4 skipped、1条Starlette弃用warning已实测；这些用例未覆盖上述接线与语义问题。
- 内存复现MIC/MBC表变成两个MBC、record表格eid与实际span hash不同；Figure1真实IC50、mean±SD测量、dose+response句均被整句排除，实验less-than被错归claim。
- 已输出reports/2026-10-14_post_m1_review.md与docs/superpowers/plans/2026-10-14-bio-pdf-post-m1-remediation.md；B01–B12分三轮，解除gold与真实表结构的错误前置依赖。

## 2026-10-14 深入优化发现
- 现有 InspectStage 会写页面分类结果，但 LayoutParseStage 只按全局 engine 判断，未消费逐页路由；NormalizeStage 只根据 docling 文件存在与否选择单路产物。
- inline.py 未使用已导入的 ALLOWED_ALPHABETS；实际规则仅要求任意大小写字母与空白、压缩后长度 >=15，造成作者/药物/菌名误提。
- prose.py 的单位表达式无右侧 token 边界，M 可匹配 May；所有 value+unit 都进入 Measurement，即便 metric unknown。
- tables.py 把几乎任意表头词当 metric，单物理表头行 + 第0列作实体；普通非活性表也会产出候选。
- 真实 Docling raw 仅识别第7页菌株用途表和第9页动物评分表，未识别第8页药敏表；83条活性中的55条来自前两张非活性表，指标包括 Used/Notes/Animal/Score/value。
- 第9页明确写有短序列 RRRQRRKKRG 与 DTRLNTVWMW（均10残基）；现有 >=15 且整块纯字母规则全部漏掉。文字说明 retro-inverso/D-amino-acids，不能将两段天然片段直接当作完整合成肽序列。
- 实际样例 relations=0、assay_id非空=0；主流程调用 link_explicit_relations([], sequences, measurements, [])，实体/assay提取没有进入流程。
- validate_evidence 用 getattr 读取 dict，误判 geometry；当前验证没有检查字段证据、坐标有效性和同文档归属。
- match_sequences 跳过所有无 seq_id_no 候选，作者名等误提不进入 false_positives；matching 的 wrong_value 计数也与文档所述 FP/FN 定义不一致。
- ReviewPanel 冲突后通过 Math.max 升高 baseRevision，没有强制刷新内容；PatchFields 缺 operator、序列修改表单，scalar 改 number 未参与 unchanged 比较。
- Runner 只核验第一制品，cache key 未包含上游制品hash/规则版本，resume 跳过阶段不累计原审核计数。
- 审查辅助命令一次以默认GBK读取JSON失败；已用 encoding='utf-8'重新读取相关制品，非业务故障。
- API使用同一SQLite连接跨并发请求（check_same_thread=False），BEGIN/COMMIT没有请求隔离；计划要求请求/后台/heartbeat独立连接。
- artifacts以内容hash为PK，register_artifact覆写relative_path；相同产物跨job登记可改掉旧job引用，计划拆分内容hash与occurrence ID。
- Source Range的suffix形式计算end错误；计划同时修有限读取与协议边界。
- 完整计划路径 docs/superpowers/plans/2026-10-14-bio-pdf-accuracy-optimization.md；17任务、5批次，含字段证据/人修/评测范围与可复制执行指令。

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
