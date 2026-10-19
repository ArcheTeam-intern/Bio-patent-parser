# Bio-PDF 处理流水线

> 端到端讲述：用户上传一份 PDF 之后，代码里到底跑了什么、用的什么模型/什么 API、哪些步骤只靠 OCR、哪些步骤是真正的领域规则。
>
> 读者：第一次接触本仓库的工程师 / 实习生。

---

## 0. 一句话总览

1. 用户通过 **CLI**（`bio-pdf parse …`）或 **HTTP API**（`POST /v1/jobs`）上传 PDF。
2. **`JobService`** 把文档写库、生成 `WorkflowConfig`、创建 `Job` 行，然后交给 **`WorkflowRunner`**。
3. 跑 8 阶段状态机 — `ingest → inspect → native_parse → layout_parse → normalize → extract → validate → export`，全部跑完后再写入一个「不可变结果清单」到 SQLite。
4. 每个阶段都是纯 Python 的 `StageHandler`（`src/bio_pdf/workflow/handlers.py`）。阶段之间通过 **`ArtifactRef`** 引用上游产物，按"逻辑键"取，不读固定路径。
5. 真正重的活是两个 layout 解析适配器（Docling 本地 SDK / docling-serve HTTP / MinerU HTTP）和一个**手写的融合步骤**（把 pypdfium2 原生文字 + 版面结构按空间重叠对齐）。**OCR 是按页面选**，不是全文扫。
6. 抽取**不是**「OCR + 一条正则」—— 6 个独立规则模块：ST.25 序列表、三字母氨基酸、正文序列、SEQ ID 引用、抗体链配对、活性表格、活性正文、实体、关系，全部手写。
7. 校验阶段对每条记录跑结构化检查，决定 `acceptance_state`。**默认 `auto_accept = false`**，有冲突或证据缺失的进入人工审核队列。
8. 导出阶段写 `result.json` + `sequences.csv` + `measurements.csv`；独立的 review 服务和 API 让人用 `correct` / `reject` / `confirm` 修改记录并提升 `result_revision`。

---

## 1. PDF 从哪进

| 入口 | 文件 | 作用 |
|---|---|---|
| CLI | `src/bio_pdf/cli.py` | Typer 命令：`doctor` / `inventory` / `parse` / `batch` / `status` / `resume` / `review export` / `review apply` / `export` / `benchmark` |
| HTTP API | `src/bio_pdf/api/app.py` + `routes/` | `POST /v1/jobs`、`GET /v1/jobs/{id}`、`POST /v1/jobs/{id}/resume`、`GET /v1/jobs/{id}/result`、`GET /v1/jobs/{id}/reviews`、`POST /v1/jobs/{id}/reviews/{rid}/decisions` |

两边都调用同一个 **`JobService`**（`src/bio_pdf/application/job_service.py`）。该服务负责：

- 写 `documents` 表（`sha256`、源路径、字节数、元数据）
- 为每个 job 持久化一份 `WorkflowConfig`（引擎、页范围、抽取目标、`ocr_policy`、`document_type_hint`）
- **入库前预检引擎可用性**（B04：环境跑不动的引擎不进入队列，避免沉默替换）
- 颁发**租约**（plan T03/B03：崩溃 worker 的最后写不会盖掉新 worker 的结果）

---

## 2. 固定的 8 阶段状态机

定义在 `src/bio_pdf/workflow/stages.py`：

```
ingest → inspect → native_parse → layout_parse → normalize
       → extract → validate → export
```

`reparse` 和 `validate_after_reparse` 是第二期任务 14（带预算的二次 OCR）预留的，本期生产环境**不注册**。

`WorkflowRunner`（`runner.py`）驱动每个阶段。对每个阶段它做：

1. 构造**内容缓存键**：`(阶段名, 文档 sha256, 页范围, 上游产物 hash, 有效选项, 实现版本, 提供方版本)`
2. 如果 `resume=True`，**校验上一阶段的清单 + 每个产物文件 hash**，任意一个对不上就重跑
3. 在隔离目录 `jobs/<job>/stage_runs/<run_id>/` 调 `StageHandler`
4. 成功后用「带租约的 SQL 事务」调用 `complete_stage_fenced(...)`。如果中途租约被抢，写入直接被拒
5. 全部阶段跑完，第二个事务 `publish_result_fenced(...)` 写 `publications.manifest_sha256`——**只有这个点能更新发布基线**

### `data/` 目录布局

```
data/
  bio.sqlite                # 全部 jobs/stages/reviews/records/exports/publications
  jobs/<job_id>/
    stage_runs/<stage_run_id>/
      stage.manifest.json   # 输入/输出/告警/覆盖
      inspection.json       # ingest
      quality.json          # inspect（信号 + 解析计划）
      normalized.json       # 解析阶段
      raw_response.json     # 适配器输出
      manifest.json         # 适配器清单
      extract.json          # extract 阶段
      validation.json       # validate 阶段（审核计划 + 结构化检查）
      result.json           # export 阶段（最终领域产物）
    published/
      result-manifest-<ts>.json   # 不可变，按 hash 寻址
      current.json                # DB 指针对镜像
  evidence/                 # 逐页 PNG（render_evidence_crop）
```

---

## 3. 逐阶段

### 3.1 `ingest` — `IngestStage`

- 驱动：`src/bio_pdf/ingest/inspector.py` 的 `inspect_pdf(path)`
- 读文件头：不是 `%PDF-` 直接报 `INPUT_NOT_PDF`（HTML 伪装 PDF 在这里被拒）
- pypdfium2 打不开报 `INPUT_CORRUPT` / `INPUT_ENCRYPTED`
- 算**文档 SHA-256**（按原字节）
- 输出 `DocumentInspection`（`page_count`、`is_encrypted`、每页 `width_pt` / `height_pt`）

**没有任何模型、没有网络**。纯 PDF 头校验 + 哈希。

---

### 3.2 `inspect` — `InspectStage`

- 驱动：`inspect_page_signals`（`src/bio_pdf/pdf/quality.py`）+ `classify_page` + `build_parse_plan`（`src/bio_pdf/workflow/parse_plan.py`）

对每个请求页：

1. pypdfium2 打开页（**还没 OCR**）
2. 统计非空格字符数、可打印比例、替换字符比例
3. 用扫描线算法算**所有图块的并集面积**（重叠图块不会算出 >100% 覆盖率）
4. 页面分类：`native / scan_suspect / text_suspect / mixed / blank / unknown`，并记**为什么**
5. 产出 **`ParsePlan`**：每页一个 `PageTask(kind=…, reasons=[…])`
   - 扫面页 → `layout_ocr`
   - 文字页 → `layout_native`（**不调模型**）
   - 空白页 → 标记但保留（**不假装漏检**）
6. 尊重 `ocr_policy`（`auto` / `force_ocr` / `off`）

**这步决定每页用 OCR 还是 native**，而不是一次性给全文做 OCR。混合 PDF（数字标题 + 扫描表）会被拆开按页选。

---

### 3.3 `native_parse` — `NativeParseStage`

- 驱动：`NativeAdapter` → `read_native_pages`（`src/bio_pdf/pdf/native.py`）
- 纯 pypdfium2
- 每页一个 `PdfTextPage`，`get_rects` 拿所有矩形 → 按垂直带分组为行 → `TextBlock`（顶-左 bbox + 原文本）
- **字形和顺序原样保留**，`text_raw` 是真实字符。**无 OCR。无模型。**

这条通道是**字形的真相来源**；layout 通道提供结构，融合步骤负责对齐。

---

### 3.4 `layout_parse` — `LayoutParseStage`

- 驱动：`DoclingAdapter`（`src/bio_pdf/parsers/docling_adapter.py`）、`parse_plan.group_execution_runs`、`docling_child.py`
- **执行 `ParsePlan`** 而不是自己再判一次页面
- 连续同 `kind` 的页批量到一次 `converter.convert(...)`：
  - `layout_ocr` 页 → `PdfPipelineOptions.do_ocr = True`
  - `layout_native` 页 → `force_backend_text = True`（**不走模型**）
- 每次 run 自己的 `normalized.json` 放在 `stage_runs/<id>/run-<index>-<kind>/`，**按源页号合并回主 `normalized.json`**

可用的适配器：

| 适配器 | 模块 | 实际跑什么 |
|---|---|---|
| `NativeAdapter` | `native_adapter.py` | 仅 pypdfium2，不装 SDK |
| `DoclingAdapter`（本地 SDK） | `docling_adapter.py` | `docling.DocumentConverter` 在**子进程**跑（`docling_child.py`）。模型：`docling-layout-heron`（VLM）+ `EasyOcr` / `Tesseract` / `RapidOcr`（`ocr.py` 选）+ `TableStructureOptions(do_cell_matching=True)` |
| `DoclingServeAdapter`（HTTP） | `docling_serve_adapter.py` | `httpx` 调 `docling-serve` 容器，契约 `/v1/convert/file/async` + `/v1/status/poll/{id}` + `/v1/result/{id}` |
| `MinerUAdapter`（SaaS HTTP） | `mineru_adapter.py` | `httpx` 调 `https://mineru.net/api/v4/extract/task`（Bearer token，`model_version="vlm"`）。本地文件通过 `/file-urls/batch` 预签名 OSS PUT 上传，响应 zip 里解 `*_content_list.json` |

OCR 是**显式选择**。`ocr.py` 探测 `tesseract`（系统二进制）和 `rapidocr + onnxruntime`（纯 Python ONNX），都没装就报 `ENGINE_UNAVAILABLE`，**不静默降级**。

#### 稀疏页集合（B07）

请求 `{2, 4}` 时，`docling_child.py` 拆成 `[2,2]` 和 `[4,4]` 两次 run，**按源页号过滤合并结果**——`2` 和 `4` 出来，`3` 不出现。这是早期「多返回一页」bug 的修复。

#### 覆盖闸

如果某个 run 漏了一页，**报 `PageCoverageMismatchError` 失败**，不假装完成。空白页会**用 `inspection.json` 里的真实几何合成一个空 `Page`**，让"已检查而非漏页"是真的，不是猜的。

---

### 3.5 `normalize` — `NormalizeStage`

- 驱动：`normalize_docling_document`（`src/bio_pdf/parsers/normalize_docling.py`）、`fuse_documents`（`src/bio_pdf/parsers/fusion.py`）
- 产出**整个 job 的规范 `NormalizedDocument`**，所有后续阶段都从这里读

主结构源优先级：

- 有 `layout_parse.normalized.json` → 用它
- 否则用 `native_parse.normalized.json`
- 两者都有 → **`fuse_documents(primary, native)`** 按空间重叠（`overlap_ratio ≥ 0.5`）对齐：
  - 只有空白差异 → 用 native 字符，结构保留 layout
  - 一边多出上下文 → 保留**更长的文本**（不丢内容）
  - 真正冲突 → **不静默解决**。两个通道的全文都记成 `TEXT_CONFLICT`，记录**禁止自动接受**（plan T05）

输出 `normalized.json`：`schema_version`、`document_id`、`parser_run_id`、`pages[]`（`page_number`、`width_pt`、`height_pt`、`route`、`blocks[]`、`tables[]`）、`errors[]`。

---

### 3.6 `extract` — `ExtractStage`

- 驱动在 `src/bio_pdf/extract/`：

| 模块 | 作用 |
|---|---|
| `sequences/st25.py`（476 行） | **ST.25 状态机解析器**：`<210>`、`<211>`、`<212>`、`<213>`、`<220>`、`<400>`。新 `<210>` 结束上一条序列。跨页续段保留，`is_complete` 反映真实完成度 |
| `sequences/amino_acids.py` | 三字母→一字母表，**token→残基可追溯**（B02）。`Xaa`→`X`；未知三字母**保留 `unresolved`**，不假装正确 |
| `sequences/inline.py`（478 行） | ST.25 之外的正文序列候选（TAT-RasGAP 317-326 这种） |
| `sequences/references.py` | `SEQ ID NO: 7` / `SEQ ID NOs: 7-9` 引用。已解决 / 未解决分开记，**未解决的不伪造** |
| `sequences/variants.py` | `A23G`、`Δ5-8`、N-端乙酰化——**只在显式参考 + 编号体系**下重建，否则只存关系候选 |
| `activities/values.py`（155 行） | 活性值 tokenizer：`>128`、`0.5-1.0`、`1.2 ± 0.1`、`ND`、科学计数法。用 `Decimal` 不用 `float` |
| `activities/metrics.py`（261 行） | 指标名规范化（IC50/EC50/MIC/MBC/Kd/Ki/...） |
| `activities/assays.py` | AssayRecord：靶标、菌株/细胞、方法、条件（key/value/unit）、参考实体 |
| `activities/tables.py`（316 行） | 表格单元格抽取，行/列表头传播，**续表检测**（caption、列数、相邻页、实体一致） |
| `activities/table_classifier.py` | 表格分类：`INVENTORY / SCORING / SEQUENCE / ASSAY_CONDITIONS / UNCLASSIFIED`。**只有 `SCORING` 才产生活性记录**，其它类型只保留结构 |
| `activities/prose.py`（347 行） | 正文活性——"IC50 = 1.2 nM" 这种 |
| `activities/header_graph.py` | 表头→单元格连接图，处理多单位 / 合并表头 |
| `activities/mentions.py` | 原始 mention 捕获，按 role 标签 |
| `entities.py` | 实体抽取（peptide / antibody / variant / compound / control），显式带 `chain_map`（VH/VL→seq_id_no） |
| `relations.py` | `link_explicit_relations`——抗体链配对**只看显式 `chain_map`**，不看名字相似度。不做笛卡尔积 |
| `evidence.py`（304 行） | **`EvidenceIndex`**——集中证据表。span id 是不透明字符串；offset 在抽取时从真实匹配位置算，**T06 不变量** |

阶段实际做的事（按顺序）：

1. 把 `NormalizedDocument` 摊平成 `(text, kind, page_number, bbox, block_id, source_*)` 块。`source_parser` 从适配器元数据读，**不从 id 字符串猜**（B02）
2. **序列**：先扫 ST.25 块，剩下的过 `extract_inline_sequences`。diagnostics 和 raw_mentions 都收集，**不静默丢任何值**
3. **活性**：先正文后表格。每张表**先注册真实 cell span**，再把 `cell_id → evidence_id` 映射交给表格抽取器（B06）
4. **引用**：每条 `SEQ ID` mention 用它在源块的**真实字符 offset** 独立登记 evidence span
5. **实体 + 链接**：`extract_entities` 抽抗体/肽标签；`link_measurements_to_entities` **只接受精确名字匹配**——不匹配时 `entity_id` 留空、加审核原因（B07）
6. **关系**：抗体链配对**只看 `chain_map`**；未解决引用保留在 `unresolved_references`（B05）
7. **未知位置**：任何 record id 的 evidence span 没注册的→降级为 `UNLOCATED`，**绝不伪造页 1**（B02）。前端显示"缺少位置证据"

输出 `extract.json`：领域记录 + `evidence_spans` 索引 + `raw_mentions` + `diagnostics` + `review_reasons`。

---

### 3.7 `validate` — `ValidateStage`

- 驱动：`validation/sequences.py`、`validation/activities.py`、`validation/evidence.py`、`validation/acceptance.py`、`review/plan.py`

对每条记录：

- 跑结构化检查，返回 `{check_id, outcome ∈ {pass, fail, not_applicable, unknown}, details, evidence_ids}`
- 聚合成 `ValidationResult`，`validation_state ∈ {unchecked, checks_passed, conflict, verified_reference, verified_human}`
- 调 `decide_acceptance(record)` 设 `acceptance_state ∈ {candidate, needs_review, accepted, rejected}`

**默认 `auto_accept = false`**。`accepted` 只通过 `verified_reference`（匹配同 publication+version 的官方 ST.25 附件）或 `verified_human`（人工 `confirm`）达到。**LLM 自报置信度从来不是接受依据**（设计 §6.6）。

设计定义的检查 ID：

```
SEQ_DECLARED_LENGTH_MATCH      SEQ_ALLOWED_ALPHABET
SEQ_POSITION_CONTINUITY        SEQ_COMPLETENESS
SEQ_REFERENCE_MATCH            ACT_VALUE_PARSE
ACT_UNIT_PRESENT               ACT_ENTITY_LINK
ACT_TARGET_LINK                ACT_EVIDENCE_KIND
EVIDENCE_EXISTS                EVIDENCE_GEOMETRY_VALID
```

`build_review_plan` 生成 `ReviewItem` 队列：任何有 `reason_codes` 或 `text_conflict` 的记录，按严重度排序。**validate 阶段只"规划"队列**——真正的 `review_items` 行由 runner 在发布事务里写（B03：被抢租约的 worker 不能通过旁路写共享审核队列）。

---

### 3.8 `export` — `ExportStage`

- 驱动：`export/json_export.py`、`export/csv_export.py`
- 写三个产物到阶段 run 目录：
  - **`result.json`** —— job 的规范领域产物（sequences + measurements + entities + relations + review reasons + 每条记录 evidence）。schema `1.1.0`
  - **`sequences.csv`** —— 一行一条 `SequenceRecord`。列：`sequence_id, document_id, seq_id_no, molecule_type, raw_sequence, normalized_sequence, declared_length, observed_length, is_complete, evidence_ids, validation_state, acceptance_state`
  - **`measurements.csv`** —— 一行一条 `Measurement`。列含 `raw`、`normalized`、`operator`、`unit`、`assay`、`entity`、`target` + 校验/接受状态
- **CSV 不是两列（"一条序列 + 一个值"）**，是下游消费者真正想要的全字段

`--accepted-only` 标志（CLI / API）**是读时过滤**，不是破坏性操作。

---

## 4. 模型、API、组件清单

### 4.1 ML / VLM 模型

| 组件 | 位置 | 说明 |
|---|---|---|
| `docling-layout-heron`（VLM） | Docling 2.138.0 自动下 | 版面/阅读顺序模型，首次 `converter.convert(...)` 时加载（~172 MB） |
| `TableFormer` / 结构模型 | Docling 内置 | 表格单元格匹配、表头检测 |
| OCR 引擎（`ocr.py` 运行时选） | | |
| ├─ `tesseract`（CLI） | 系统二进制 | `apt-get install tesseract-ocr`。`ocr_engine="tesseract"` 或 `auto` 且无 rapidocr 时用 |
| └─ `rapidocr` + `onnxruntime` | PyPI | 纯 Python ONNX OCR。`ocr_engine="rapidocr"` 或 `auto` 且无 tesseract 时用。`language` 走别名表（`en` / `eng` / `chi_sim` / `ch` / `chinese` / ...）|
| `EasyOcr` | Docling 可选后端 | 装了可用，但**不自动选** |

### 4.2 远程服务（HTTP）

| 服务 | 端点 | 鉴权 | 适配器 |
|---|---|---|---|
| `docling-serve`（自托管或社区） | `/v1/convert/file/async`、`/v1/status/poll/{id}`、`/v1/result/{id}` | 可选 `X-Api-Key` | `DoclingServeAdapter` |
| MinerU V1 SaaS | `https://mineru.net/api/v4/file-urls/batch` + `/extract/task` + `/extract/task/{id}` | `Authorization: Bearer $MINERU_TOKEN` | `MinerUAdapter` |

### 4.3 纯 Python 模块（无外部服务）

| 模块 | 作用 |
|---|---|
| `pypdfium2 4.30.0` | PDF 渲染、文本页、字符/矩形坐标 |
| `pydantic 2.14` | 所有合同类型、校验 |
| `SQLite`（`sqlite3` 标准库） | 仓储、迁移、租约/心跳/发布 |
| `reportlab`（仅 dev） | 构造双栏测试夹具 PDF |
| `pytest` | 测试运行器 |

### 4.4 配置项（`configs/demo.toml` + `WorkflowConfig`）

| 键 | 默认 | 作用 |
|---|---|---|
| `parser` | `native` | `native` / `docling_sdk` / `docling_serve` / `mineru_http` |
| `pages` | 空（= 全部） | 逐页选择，支持稀疏集合 |
| `targets` | `("sequences", "activities")` | extract 阶段跑哪些 |
| `document_type_hint` | `auto` / `paper` / `patent` | 引导逐页质量启发式 |
| `ocr_policy` | `auto` / `force_ocr` / `off` | layout 是否走 OCR 通道 |
| `ocr_engine` | `auto` | `auto` / `tesseract` / `rapidocr` |
| `ocr_lang` | `en` | 走别名表解析 |
| `auto_accept` | `false` | 记录接受的总开关 |

---

## 5. OCR vs 真正的领域规则

明确区分——项目名字和 Docling 依赖都让"OCR"听起来像唯一的事，但 OCR 只是入口。

| 步骤 | OCR 类？ | 说明 |
|---|---|---|
| `inspect_page_signals` | **不是。** 纯 pypdfium2 + 计数 |
| `read_native_pages` | **不是。** pypdfium2 直接读 PDF 自己的字形层 |
| `classify_page`（路由决策） | **不是。** 文本/图像占比启发式 |
| `LayoutParseStage` 的 layout-native run | **不是。** `force_backend_text = True`，热路径不调模型 |
| `LayoutParseStage` 的 layout-OCR run | **是——但只在 inspect 标记的扫面页跑。** 调 `tesseract` 或 `rapidocr`，**加上** layout VLM 拿结构 |
| `DoclingAdapter`（本地 SDK） | **可选。** OCR 后端可插 |
| `DoclingServeAdapter`（HTTP） | **可选**——看服务端 profile |
| `MinerUAdapter`（HTTP） | **是。** MinerU 永远跑完整流水线（VLM + 表格 + 按需 OCR） |
| **ST.25 解析器** | **不是。** 手写状态机，无 ML |
| **三字母氨基酸规范化** | **不是。** 纯字典 + `unresolved` 回退 |
| **正文序列检测** | **不是。** 保守正则 + IUPAC 字符表 |
| **SEQ ID 引用抽取** | **不是。** 正则 + mention 规范化 |
| **变体重建** | **不是。** 记成关系，不重建完整序列 |
| **活性值解析** | **不是。** tokenizer + Decimal |
| **单位规范化** | **不是。** 只做显式等价（`µ`/`μ`/`u`），未知单位保留原文 |
| **活性表格抽取** | **不是。** 逐单元格 + 表头传播，**不再调 OCR** |
| **正文活性抽取** | **不是。** 跟表格同一套代码路径 |
| **抗体链配对** | **不是。** 只看显式 `chain_map` |
| **实体链接** | **不是。** 精确名字匹配，无最近似 |
| **校验 / 接受** | **不是。** 在已抽取的值上跑纯规则 |
| **证据索引** | **不是。** 抽取时记 offset |
| **审核计划 / 决策** | **不是。** 纯队列 + 版本检查 `base_revision` |

**OCR 工作量只限于启发式标记的扫面页**。纯文字 PDF 全程不调任何模型（除了 Docling 在 `force_backend_text` 模式下跑的 layout/table VLM）。

---

## 6. 代码里还没写的（按设计该在哪）

| 缺失 | 设计位置 | 现状 |
|---|---|---|
| ST.25 TXT 附件 | Task 13 | `extract/sequences/st26.py` 和 `ingest/attachments.py` 只有占位 |
| 二次预算 OCR | Task 14 | `review/reparse_plan.py` 和 `ocr.py` 的 `resolve_ocr_options` 存在，但 `reparse` / `validate_after_reparse` 阶段没注册 |
| 可选 LLM 候选 | Task 15 | `providers/base.py`、`providers/http_provider.py`、提示词只有占位，没人调 |
| FastAPI 审核 UI | Task 16 | `api/app.py` + `routes/` 已搭好，页面静态资源没提交 |
| Go/TS 合同迁移 | Task 17 | `schemas/v1/*.schema.json` 已导出并提交，Go/TS 消费者没写 |

---

## 7. 失败隔离与恢复

每个阶段只往 `jobs/<job>/stage_runs/<run_id>/` 写。中途崩溃的 run 留下清单 + 产物文件，下次 `resume=True` 时由 runner 校验（`runner._cache_is_intact`）：

1. 阶段 `stage.manifest.json` 的 hash
2. 清单里 `stage_name` / `cache_key` / `stage_run_id` 三个字段
3. **每个产物文件的 hash**

任意一个对不上就重跑，新的产物注册后，下游阶段的缓存键重算。

**审核队列的写入只在 runner 的 `publish_result_fenced` 事务里发生**。被抢租约的 worker 不能通过任何其它路径写 `review_items`。同一事务还写 `publications.manifest_sha256`，所以发布基线要么是完整验证过的 run，要么根本没写。

---

## 8. 文件地图

```
src/bio_pdf/
  cli.py                    # Typer 入口
  config.py                 # WorkflowConfig + load
  errors.py                 # 错误码词汇表
  application/
    job_service.py          # CLI/API 共用生命周期
    export_service.py
    result_snapshot.py
    review_service.py
  api/
    app.py, routes/, settings.py
  contracts/                # Pydantic 模型 + JSON Schema 导出
  ingest/inspector.py       # 文件级校验
  pdf/                      # pypdfium2 原语（native、geometry、render、quality）
  parsers/
    base.py                 # ParseRequest/ParseArtifacts/Adapter 协议
    native_adapter.py
    docling_adapter.py + docling_child.py + normalize_docling.py
    docling_serve_adapter.py
    mineru_adapter.py
    ocr.py
    fusion.py               # native ↔ layout 双通道融合
  extract/
    sequences/              # ST.25、amino acids、inline、references、variants
    activities/             # values、metrics、assays、tables、prose、header_graph、mentions、table_classifier
    entities.py、relations.py、evidence.py
  validation/               # sequences、activities、evidence、acceptance
  review/                   # plan、compare、decisions、reparse_plan
  workflow/
    stages.py、runner.py、handlers.py、parse_plan.py
    queue_worker.py、worker.py
  storage/                  # 原子制品 + SQLite 仓储 + 迁移
  evaluation/               # 标注、匹配、指标、报告（M1C）
  export/                   # JSON + CSV 写入
configs/demo.toml           # 默认 WorkflowConfig
schemas/v1/                 # 导出的 JSON Schema（Go/TS 合同）
annotations/v1/             # 标注清单 + 指南
data/                       # 运行期 SQLite + 制品（gitignore）
reports/                    # 人读的运行报告
```

---

## 9. 给最忙的人的两行

- **OCR 只跑在扫面页**（inspect 启发式判定），其它路径走 pypdfium2 原生字形 + Docling backend text（不调模型）。
- **抽取** = 6 个手写规则模块（ST.25、三字母氨基酸、正文序列、SEQ ID 引用、表格+正文活性、抗体链配对/实体/关系/证据），**不是**「OCR + LLM」；接受策略默认 `auto_accept = false`，只有 `verified_reference` 或 `verified_human` 才会 `accepted`。
