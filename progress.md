# Progress

## 2026-10-13

### Biological PDF parser implementation (M1A)

执行计划：`docs/superpowers/plans/2026-10-13-bio-pdf-workflow-implementation.md`。

#### Task 0: 初始化业务项目与可重复环境 ✅

- 根目录初始化 Git 仓库。
- `.gitignore` 排除 `docling/`、`pdf/`、`.venv/`、`data/`、模型缓存、SQLite 制品、密钥文件。
- `pyproject.toml` 使用 hatchling 构建，Python 3.12 锁定，核心依赖 pydantic/typer/pypdfium2。
- 可选 extras：`dev`（pytest/reportlab/ruff）、`docling`（重型 SDK，按需）、`mineru`、`llm`。
- `uv sync --extra dev` 成功；锁定 pydantic=2.14.1 / pypdfium2=4.30.0 / typer=0.26.8 / pytest=9.1.1。
- `src/bio_pdf/cli.py` 注册 `doctor` 与 `inventory` 命令，Typer 0.26 需要 `@app.callback()` 才能路由子命令。
- `doctor` 输出 pydantic/typer/pypdfium2 模块可用，docling/httpx 未安装；`model_readiness=not_checked`，未隐式加载模型。

#### Task 1: 合同、状态枚举、Schema 导出 ✅

- `src/bio_pdf/contracts/` 实现 `common.py`/`document.py`/`records.py`/`job.py`/`export_schema.py`。
- 所有模型 `extra="forbid"`；坐标 `validate_bbox()` 拒绝 x0>x1、负页码、NaN/Inf、越界。
- 设计 §6 中的 SequenceRecord、Measurement、EvidenceSpan、ActivityValue、Job、Stage、ArtifactRef、ReviewItem、ReviewDecision 等全部落地。
- `export_schema.py` 输出 28 个 JSON Schema；`schemas/v1/` 已生成 29 个文件（含 bundle）。
- 回归测试 26 个：bbox 异常、unicode（µ/中文）保留、Decimal 字符串、JSON Schema required/additionalProperties=false、确定性。

#### Task 2: 输入校验、文件清单、页选择 ✅

- `src/bio_pdf/ingest/inspector.py` 实现 `inspect_pdf` / `inspect_directory` / `parse_page_selection`。
- HTML 文件伪装成 .pdf → `INPUT_NOT_PDF`；损坏 PDF → `INPUT_CORRUPT`（含错误页码或签名问题）。
- `parse_page_selection` 支持 `all`/`1-3,7` 等，去重排序；拒绝 0、负数、超范围、倒置。
- `bio-pdf inventory --input pdf --out reports/inventory.json` 对真实语料跑出 7 OK + 3 INPUT_NOT_PDF（与原签名审计完全一致）。
- 26 个 inspector 测试覆盖：HTML 伪装、空白 PDF、损坏 PDF、SHA-256、目录拒绝、批处理不写临时文件。

#### Task 3: 不可变制品、SQLite 仓库、缓存键 ✅

- `src/bio_pdf/storage/artifacts.py` 实现 `atomic_write_bytes/json`、`write_json_artifact`、`read_verified_*`、`make_cache_key`。
- 原子写入：tempfile + os.replace；写后崩溃不能留下 .tmp.* 临时文件；相同内容幂等，不同内容 → `ARTIFACT_CONFLICT`。
- 路径安全：拒绝 `..`、绝对路径；路径必须在 `artifact_root` 内。
- 缓存键：JSON canonical（sorted_keys + 无空格）SHA-256；测试证明 model_revision/pages/options 改变 → 键变；key 顺序不变 → 键不变。
- `src/bio_pdf/storage/repository.py` 实现 SQLite：6 表（documents/jobs/stage_runs/review_items/review_decisions/artifacts），外键开启、busy_timeout=5s、WAL 模式。
- 关键回归：版本不匹配 → `REVIEW_REVISION_CONFLICT`；迁移可重放；SQLite 文件重新打开结构一致。
- 29 个测试全部通过。

#### Task 4: 原生字符、几何、渲染、质量信号 ✅

- `src/bio_pdf/pdf/geometry.py`：`pdfium_bbox_to_top_left` / `top_left_to_pdfium_bbox` / `apply_rotation` / `map_pixel_to_page` / `clip_bbox_to_page`。
- `src/bio_pdf/pdf/quality.py`：基于 nonspace_chars / printable_ratio / replacement_ratio / image_coverage 的路由分类（native/blank/scan_suspect/text_suspect/mixed/unknown）。
- `src/bio_pdf/pdf/render.py`：`render_evidence_crop` 用 pypdfium2 `crop=(left, bottom, right, top)`（边距量，非 bbox 选 PDF 区域）；同时返回 `PageTransform` 用于像素→点反向。
- `src/bio_pdf/pdf/native.py`：`read_native_pages` 使用 `PdfTextPage.get_rect`（每视觉一行一个矩形），避免 per-character 拼接错误。
- ReportLab 构造 2 页夹具：左右栏分别有 ST.25 序列 + IC50 ≤ 1.2 nM；左右栏不交错拼成一条序列；"1 5 10" 独立成行。
- 50 个 PDF 测试通过：geometry（14）、quality（10）、native 集成（8）。

#### Task 5: Docling 适配器与规范化 ✅（离线部分）

- `src/bio_pdf/parsers/base.py`：ParserAdapter Protocol、ParseRequest、ParseArtifacts。
- `src/bio_pdf/parsers/native_adapter.py`：包装 `read_native_pages`，输出 raw/normalized/manifest。
- `src/bio_pdf/parsers/normalize_docling.py`：纯 JSON → NormalizedDocument；top-left bbox、block kind 分类（title/list/table/picture/formula/sequence_listing/text）、表格 cell/header。
- `src/bio_pdf/parsers/docling_adapter.py` + `docling_child.py`：在子进程中调用 SDK，避免主进程被模型加载阻塞。
- 11 个离线测试全部通过：bbox 接受 list/dict、缺 bbox 不伪造、表格 cell/header、无 pages 列表 → NormalizationLossError。
- `tests/fixtures/providers/docling/fixture_page_one.json` 已脱敏落盘。
- **Live 集成测试（`tests/integration/test_docling_live.py`）未运行**：docling SDK 重型，包含模型下载/OCR/可选 Tesseract 安装，本次未安装 `uv sync --extra docling`；待用户在本地确认安装后用 `BIO_PDF_LIVE_TESTS=1 uv run pytest tests/integration/test_docling_live.py -v` 触发。计划要求的真实 1-3 页解析、证据图、耗时记录需要 Live 测试。

#### 测试汇总

- 共 127 个测试通过，3 个 live 测试跳过（缺 `BIO_PDF_LIVE_TESTS=1`）。
- 涉及目录：`tests/unit/`、`tests/contracts/`、`tests/integration/`、`tests/fixtures/`。

#### 当前限制与下一阶段

- Live Docling 解析未跑；任务 5 在当前会话仅完成离线合同。
- 阶段 M1A 计划要求真实样本解析 → 在用户授权安装 Docling SDK 后再继续。
- 第一期 M1A 内剩余任务（Task 6-11）将在 Docling live 可用后再启动。

#### Task 5 追加：docling-serve HTTP 适配器 ✅

- 用户确认「本地 SDK + 可选 docling-serve」双路径。
- `src/bio_pdf/parsers/docling_serve_adapter.py`：走「上传 → 提交 job → 轮询 → 下载」契约，只依赖 httpx，业务机不需 SDK/模型。
- 结果形状守卫：failed → ENGINE_UNAVAILABLE；形状不可识别 → ENGINE_PROTOCOL_UNSUPPORTED；轮询超时 → ENGINE_TIMEOUT（只结束本地等待，不推断远端失败）。
- 12 个离线测试（httpx.MockTransport）覆盖生命周期/鉴权头/超时/失败/404/非 JSON/包裹形状。

### Biological PDF parser implementation (M1B) ✅

#### Task 6: ST.25、正文序列和引用抽取 ✅

- `amino_acids.py`：三字母→一字母映射（含 B/Z/J/U/O/X），未知三字母保留 unresolved，核酸 IUPAC 模糊字符。
- `st25.py`：状态机读取 <210>/<211>/<212>/<213>/<220>/<223>/<400>，三字母转一字母保存位置，位置数字移除，跨 block 续段，declared/observed 长度分离。
- `references.py`：SEQ ID NO:7 / NOs:7-9 只生成引用（范围展开），不虚构序列。
- `inline.py`：保守候选（≥15 残基、正文块、排除 title/identity 反例）。
- `variants.py`：A23G/Δ5-8 变体关系候选，无参考时保留 placeholder。
- 28 个测试通过。

#### Task 7: 数值语义、单位与活性表格 ✅

- `values.py`：parse_activity_value（categorical→±→比较符→范围→scalar→unparsed），Decimal 验证、字符串输出；normalize_unit（µ→u 等价书写，未知返回 None，不做跨单位转换）。
- `tables.py`：表头 metric/unit/target 解析 + 表格→measurement，每字段 evidence 锚定。
- `prose.py`/`assays.py`：正文测量候选与条件解析。
- 22 个测试通过（含黄金回归 >128 / 0.5–1.0 / ND）。

#### Task 8: 实体、链配对、关系 ✅

- `contracts.records` 增补 Entity / EntityKind / RelationResult。
- `relations.py`：link_explicit_relations 显式链配对（无笛卡尔积、无兄弟替换、document_id 命名空间）；classify_evidence_kind。
- 9 个测试通过。

#### Task 9: 检查规则、审核项、接受策略 ✅

- `validation/`：sequences/activities/evidence 结构化检查（pass/fail/unknown+reason）；acceptance 策略 validation_state 与 acceptance_state 分离（auto_accept 默认 false）。
- `review/plan.py`（有界审核计划）、`review/compare.py`（双路 diff）。
- 17 个测试通过（含黄金回归「长度/字母通过≠序列真值正确」）。

#### Task 10: 状态机、CLI、导出、审核修订 ✅

- `workflow/stages.py`（固定阶段顺序）+ `runner.py`（确定性状态机、per-attempt 持久化、resume 按 cache key 跳过已成功 stage）+ `worker.py`（子进程）。
- `export/`：json_export（accepted_only + counts）、csv_export（完整列）、review_html（转义）。
- `review/decisions.py`：correct/reject 追加决策，旧 base_revision → REVIEW_REVISION_CONFLICT。
- CLI 新增 `parse` 命令（native/docling/docling_serve 引擎）。
- 15 个测试通过（workflow resume、cli demo、review revision、export）。

#### M1B 测试汇总

- 全量 **230 passed, 3 skipped**（3 个仍为需要 BIO_PDF_LIVE_TESTS=1 的 Docling live 测试）。
- 关键夹具：`tests/fixtures/domain/activity_table.json`（3×3 表格→4 条测量）、`antibody_pairs.json`（2 抗体 4 链）。

#### 当前限制与下一阶段

- Live Docling 解析仍待用户部署 docling-serve 或安装 SDK 后跑真实样本。
- M1C（Task 11：人工标注 + 评测脚本 + baseline）是下一阶段，需要约 30 页人工确认样本。

### 调研与设计阶段（昨日）

- Inspected workspace: pdf/ is present; no upstream implementation is present yet.
- Browsed official repositories for WeKnora, Docling, MinerU, Marker.
- Created persistent research notes and phase tracking.
- GitHub API unavailable due to rate limiting; continue via raw documentation/source and Git.
- Downloaded selected WeKnora source/documentation snapshots into research/ for comparison.
- Cloned https://github.com/docling-project/docling.git into docling/ (shallow clone).
- Verified origin URL, current commit f0d90989b461656102da5d449db98e6be9285f93, and clean worktree.
- Read Docling AGENTS.md and relevant converter, options, backend, table model and confidence documentation. No upstream files edited, models installed or parsing benchmarks run.
- Audited PDF signatures: 7 true PDF signatures, 3 invalid HTML files remain from the earlier download turn. Later benchmarks must exclude them.
- Wrote research/pdf_parser_comparison_2026-10-13.md with seven-project comparison, primary selection, accuracy controls, evidence schema, evaluation design, and source references.
- Final check confirmed official origin, commit identity, clean upstream worktree, and existing nonempty report/README. Placeholder scan found none (rg exit 1 means no matches).
- Research and source-acquisition scope complete; biological parser implementation and measured accuracy remain later stages.