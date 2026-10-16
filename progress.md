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

### Biological PDF parser implementation (M1C) ✅

#### Task 11: 人工标注、评测脚本、第一期交付 ✅

- `evaluation/annotation.py`：Annotation 模型（sequence/measurement/unreadable 三类，human_verified + status 分离，draft 排除正式准确率）。
- `evaluation/metrics.py`：safe_ratio（0/0 → None 不是 100%）、字符错误率（Levenshtein）。
- `evaluation/matching.py`：序列按 (document_id, seq_id_no) 匹配（一个 gold 不被重复计 TP，SEQ ID 相同内容不符不算 exact）；测量按 (entity,target,metric) tuple 匹配（数字正确但实体串行 → miss）。
- `evaluation/report.py`：Markdown 报告（分母、覆盖、待审核率，不声称统计证明）。
- `annotations/v1/manifest.json` + `annotation_guidelines.md`：dev/holdout 划分（≥1/3 留出），标注规则与诚实原则。
- 20 个测试通过（metrics 黄金回归、annotation schema、matching 反例）。

#### M1C 交付状态

- 评测**框架**已完成并可离线运行；**实际基准分数**需要人工标注 gold（manifest 当前 `annotations: []`，`status: pending_human_annotation`）。
- 第一期报告中的真实解析命令/制品/人工确认标记：受限于 Docling 环境未就绪 + 无人工标注，尚未产出真实 baseline。已诚实记录 coverage gap（无真实扫描页已验证，不能把渲染文字 PDF 当扫描）。

#### 全量测试汇总（M1A + M1B + M1C）

- **250 passed, 3 skipped**（3 个为需要 BIO_PDF_LIVE_TESTS=1 的 Docling live 测试）。

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
### 2026-10-14 深入修改方案
- 用户要求给其他模型执行的具体修改方案，当前只写规划文档。
- 已读取 writing-plans、brainstorming、planning-with-files-zh；沿用现有技术栈，准备分阶段任务、接口与验收夹具。
- 已识别路由未接入、单路归一化、序列字母表未使用、单位边界与通用表头误识别。
- 深入抽查：两段10残基片段被漏提；55条非目标表格候选；缺实体/assay流程；评测遗漏无SEQ ID候选。
- 已写17任务优化计划，补连接隔离、制品occurrence身份、恢复与快照规则、实际回归输入输出及执行指令。
- 本轮未修改业务代码、未重跑实现测试；只核验规划文档及当前制品，前一轮298 passed仍是当前已知基线。
- 规划自检通过：17任务，641行，覆盖确定缺陷、配置/迁移、前后端证据、专利及评测；无未解决占位符，已修正M1高亮和M4复解析依赖。

### M1执行后补充方案核查
- 已读两份执行报告、源码与当前published extract/normalized/validation。
- 当前HEAD cab949a；已有未提交规划文件，业务源码未改。
- 复跑非live测试：351 passed、4 skipped；新增补充方案将优先修接线回归、真实字段证据、OCR执行、fencing和导出快照。
- 已在内存直接复现表格串列/证据ID断链及角色分类漏提，未写应用数据。
- 已写独立核查报告与12任务差量方案，含真实新job的artifact hash、延期判断、文件范围及验收条件。
- 自检通过：12任务，315行；三轮依赖与验收覆盖完整，业务代码未改，交付文档供其他模型执行。

### 扫描PDF的OCR恢复
- 当前修复分支codex/scan-ocr-recovery；保留用户已有jobs.py/job_service.py兼容修正。
- 发现已取消uv进程残留占用虚拟环境，已停止并安装onnxruntime1.31.0（rapidocr3.10.0/docling2.138.0）。
- 新增OCR选择回归先5项失败，修复后OCR/路由12项通过；SDK规范化英语为iso:en-Latn。
- 沙箱临时目录限制曾导致pytest setup权限错误，获准在外部重跑后通过；当前环境已放开权限。
- 正准备真实一页OCR与25页任务恢复，未宣称扫描件解析成功。

### 扫描任务完成与实时运行缺陷修复（提交71e7127）
- job-6031752e8318（25页真实扫描件）：25/25页OCR出文本（20397字符），389条证据、39条mentions、13条问题；无ST.25序列标签与注册指标表，故0条自动提取记录（不伪造）。
- 修复三处真实运行缺陷：API parser名称docling_sdk被映射回legacy导致配置校验失败；OCR引擎写死tesseract（改为自动选择已安装引擎并写入手册版本）；SEQ ID引用证据ID碰撞（同一号码不同拼写共用ev-id，改为锚定真实块偏移）。
- 全量427 passed。

### Worker 存活提示（排队任务不再静默等待）
- 现象：用户上传后任务停在“等待解析”；空转的worker不持有任务租约，队列无从区分“无进程”与“解析慢”。
- 新增worker_presence心跳表（worker每5s写入，独立SQLite连接，退出清理，崩溃行由API判为过期）；新增GET /api/v1/queue/status（在线worker、排队/运行数、data_dir、过期阈值）。
- 前端WorkerHint横幅（JobsPage与工作台）：存在排队/运行任务且无在线worker时显示启动命令（含与API一致的--data-dir），worker上线后自动消失。
- 同时修正API依赖get_settings一直忽略create_app注入设置的问题（请求此前回落到环境默认data/demo）。
- 真实验证：停止worker→新任务job-b6e0ef8e9d92排队并显示横幅与命令；启动worker→横幅自动消失、任务被领取并完成（2条待审核）。全量431 passed、web build通过。


### 前端逐页验收与收尾修复（提交 c49124c / 285a3cf）
- 按批准方案执行 A1–A16 逐页实测：页码联动/范围切换/URL恢复/证据高亮/导出闭环/审核决定(含409)/扫描件表格与问题全部通过；发现并修复三处：运行详情原是静态占位（现为引擎+配置哈希+阶段表+21条制品清单真实渲染）、序列记录暴露了服务端必然拒绝的“修正并确认”按钮（现隐藏并说明需重新提取）、succeeded_with_review 在0条待审核时仍显示“待审核”。
- 补齐上轮缺口：inspector BOM 归类、record_pages 对缺失 extract 的降级、上传415 details、records 过滤一致性、useEvidence clear/selectionKey、EvidenceNavigator 组件测试。
- 环境事实：IAB webview 不触发 rAF，pdf.js 渲染需截图驱动帧（测试环境行为，非应用缺陷）；浏览器会缓存旧 bundle，验收新前端需绕过缓存。
- 446 passed（+15）、vitest 14 passed（+7）、build 通过；服务以新代码运行。

### 前端视觉美化（用户要求）
- 设计令牌扩充（阴影/圆角/浅色分隔线/更柔和的灰阶），面板加投影与表头底色，表格行悬浮、选中行左侧强调条。
- 工作台主标签改为分段控件；PDF 工具栏单行化（翻页/缩放各自分段组合，适合宽度与查看源文件右置）。
- 右栏详情：序列/原始序列放入浅色代码框，检查结果行分隔线，字段标签统一 .section-label/细化 .detail-grid。
- 对话框：投影+标题/操作区底色+入场动画+毛玻璃遮罩；文件选择按钮样式统一；状态胶囊加描边；横幅加左侧强调条；自定义滚动条与 focus ring。
- 仅样式与分类型名调整，行为不变；vitest 14 通过、构建通过。

### 用户反馈四项 + 停止解析（提交 d7845d0 / 56cb406 / 53d76cb）
- 导出弹窗被 PDF 覆盖：改用原生 showModal() 顶层弹窗（共享 Modal，Esc/遮罩关闭同步状态）。
- 停止解析：POST /jobs/{id}/cancel 单事务标记 cancelled+删租约；worker 每阶段边界重验租约行（不等心跳）；阶段完成/进度写入带租约围栏，取消后不可能复活或发布；cancelled 可 resume（复用缓存）。真实验证：40页 docling 任务中途点停止→立即已取消、worker 存活；resume 后缓存跳过、最终成功。
- 解析进度：runner 每阶段前（带围栏）写 current_stage；工作台横幅显示“解析中：结构解析（第 3/7 步）”。
- 结果分布总览：序列/活性 tab 顶部 PageDistribution——每页计数+比例条、当前页高亮、点击跳页；无页码记录如实计数不猜测。
- 顺带修复两个真实崩溃（EP3027649B1 首次暴露）：docling 空文本组违反 TextBlock 合同（跳过，不造占位）；OCR "10-8" 区间 lower>upper 违反 ActivityValue（降级 unparsed 保留原文）。
- 454 passed、vitest 18、build 通过；40页任务最终 succeeded_with_review（16序列+24活性+40待审核，第12页最密集）。

### 用户第二轮反馈五项（提交 2a71208 / e20faf7）
- 导出换格式后可重新导出：完成导出后切换格式/范围显示"当前下载仍是上次结果"提示+重新导出按钮；浏览器实测 JSON→CSV 重导闭环。
- 表格 tab 加入结果分布总览（新增 table_pages 接口字段，蓝色"表格 N"系列）；EP 任务实测 19 页分布，第 7/8/26-29/32 页有表格。
- 证据绿框点击→放大镜弹窗（showModal 顶层，画布裁剪放大 + 如实的精度标签与原文）；实测段落证据放大清晰可读。
- 运行详情失败原因：新失败保存 WorkflowError details（含引擎 stderr 尾部），可展开"详细诊断"；旧失败只有标题行时如实不显示展开（单测锁定）。EP 专利的真实根因（OCR 空文本组违反 TextBlock 合同）已在 56cb406 修复。
- 原文 tab 从逐行块表改为阅读视图：连续同角色文本合并成段落、表格碎片（R/S/编号）渲染为胶囊、可切回逐块查看；第 9 页实测 3 段正文 + 119 个单元格胶囊。
- 458 passed、vitest 24、build 通过、服务已重启并逐项浏览器验证。

### 第9页左半表：药敏矩阵几何重建（提交 96c106b + 后续修复）
- 用户问:第9页左半边(Table 3 药敏热图)是什么、为何没提取。确认:13行抗生素×菌株列的 R/S/I 矩阵,无框线+旋转表头,docling不识别为表;文本块齐全但规则禁止从散块猜结构→0记录是如实的。
- 实现 b+c:normalize 用块几何确定性重建(call-cell 聚类、中位单元格尺寸容差、稀疏行列裁剪+报告、被解析表区域排除),Table.source=geometry_reconstructed,extract 挂 TABLE_GEOMETRY_RECONSTRUCTED issue;_extract_susceptibility 按药名/菌株词表判轴(药在行=翻转,证据不足不翻转),指标 antimicrobial_susceptibility 走 categorical,全部 needs_review。
- 真实验证(job-1f9fd9ee5386):143 条药敏记录(59@第9页,84@第8页),0 条证据不匹配,0 条无页码;表格 tab 分布含"表格 N"列。
- 踩坑修复:固定12pt容差把11.7pt密列合并→整表被拒;噪声单格行使规整性检查否决全表。各补回归测试。

### 热图导出一张图 + CLSI 判读按惯例落地（提交 df05d4a）
- 调研实证:论文第8页图例原文与 CLSI/Kirby-Bauer 一致(S绿=敏感/I黄=部分耐药/R橙=完全耐药/空格=未测);现有 categorical 提取语义正确,颜色是字母的视觉冗余。
- 新增 GET /jobs/{id}/pages/{n}/figure.png?bbox=…:用 render_evidence_crop 把源PDF该区域裁成独立PNG(颜色全貌保真);表格tab每表加"导出图像"链接。真实目检:第8页TABLE 2整张热图(30行×16列+旋转菌株表头)一张图完整导出。
- 重建器捕获图例→footnote_refs,表格面板底部显示"图例：…";重建表加"几何重建"标记(候选结构,需人工确认)。
- 原文阅读视图不再把被表格吸收的块吐成字母气泡(实测第8页隐藏121块并提示去向);R/S/I显示词(耐药/敏感/中介)仅前端注释,存储保持证据原字母。
- 端到端测试:合成矩阵PDF→全管线→16条记录+图例footnote+PNG有效+issue;469 passed、24 vitest。

### 统计图/曲线图作为独立图片提取（提交 d706f75 / e52ce88）
- 用户澄清:要的是论文里的统计图/曲线图(Figure),不是热矩阵表格。查管线发现 docling 把这些当 pictures 导出,但子进程只保留 texts/tables,把 pictures 整个丢弃→所以从未有图可导。
- 修:子进程透传 pictures;normalize 新增 Figure(区域bbox BOTTOMLEFT→左上、caption 经 $ref 从正文解析、kind 由图题关键词优先于标签判定,如"Growth curve"归 plot),NormalizedPage.figures;layout 阶段实现 v6-figures 失效旧缓存。
- API:/pages 带 figures;GET /jobs/{id}/figures 汇总并在空结果时如实区分"原生引擎无检测"vs"未检出";figure.png 支持 ?figure=服务端解析bbox。
- 前端:新「图表」tab 按页分组真实裁切缩略图+类别胶囊+图题,点开全分辨率PNG,页头可跳页;原生引擎无图如实说明。
- 真实验证(docling 论文 job-878e3a8443b7):检出11图区(7带FIGURE图题,2曲线图判为plot),FIGURE 2 八联生长抑制曲线干净裁出1443x706;浏览器缩略图确认。
- 470 passed、24 vitest、build 通过、服务已重启。

### 图表 tab 两项反馈修复（提交 f1c2cb2 / 7b51fa0）
- 点开 500 + 部分图不显示:根因是缩略图并发请求把进程级 PDFium 全局态竞态污染(之后所有打开报 Data format error,单开文件正常)。render.py 全部 PDFium 操作加进程锁串行化;并发回归测试;API 进程需重启一次(已重启)。
- "文字内容不该出图":新增 filter_exportable_figures——<2.5% 页面积(刊徽/徽章)、被表格覆盖≥50%(含几何重建的热矩阵)、被已提取文字覆盖≥60% 的图区全部剔除;子进程与 normalize(重建后)两处生效;阶段版本 layout v7/normalize v6。
- 真实验证(job-5ccbaf5f28fe):11 检出→保留 7(六个 FIGURE + 1 无题面板,2 个自动判曲线图),10 路并发裁图全 200、事后进程健康。472 passed。

### 审核队列真实数量与可达性（用户提问引出）
- 发现 /reviews 默认取 50 条且把截断数谎报为 total(库内 open 771,本任务 145);第 51 条之后的审核项在队列页不可见。修复:真 COUNT(*) + offset 分页 + record_id 精确过滤 + rowid 倒序。
- ReviewPanel 原来"拉一页再找本记录"——143 条活性审核项里超出前 50 的会误显示"没有待审核项";改为按 record_id 服务端精确查询(API 已验证最后一条活性可命中)。
- 队列页:真总数、任务筛选、加载更多;工作台 "145 条待审核 ›" 可点跳筛选队列(浏览器实测 771→筛选后145)。原因标签补"缺少单位/实体未关联/长度不符/序列不完整"。
- 474 passed(+2 队列 API 测试)、vitest 24、build 通过;服务已重启。

### 表格重建准确性两处根因修复（用户实测比对引出）
- 现象:第8页重建表比原图"满"且行标签串位。诊断:①layout通道把相邻字母合并成"SSS"块,融合保留布局粒度→192个字母只有91个进入种子;②行标签靠"最近吸附",缺行时标签滑到邻居行。
- 修复:种子改用 native 字形层(逐字、精确坐标,融合块id保留来源);行骨架=左侧药名标签(全部成行,空行保留,不再串位);重叠重复字形去重;列带取最长连续链+x守卫(游离字母不再否决全表);残留散格如实计数。
- 真实验证(job-62f430801980,叠加图逐字母核验):TABLE 2 44×16/43行/192格全部归位,逐字母红框覆盖;TABLE 3 恢复 17×7/63格;药敏记录 143→255;第9页矩阵图重新被表吸收。477 passed(+3 网格测试)。

### 后续待办已登记（供会议与排期）
- MinerU/Docling 对测〔主项〕：前置=人工金标样本集 + MinerU 本机/内网自建（SaaS 外发不合规）；做法=同输入同页、字段级准确率+回源率+耗时对比；引擎可换不改业务代码（设计文档"第二期"已登记）。详见桌面文档 `唯信计算/Bio-patent-parser实现文档.md` 第六节。
- 算力升级（纯提速）：本机 RTX 4060 8GB 未启用（torch 2.14.1+cpu、onnxruntime 仅 CPU provider）；换 CUDA 版依赖即可数倍提速，模型与结果不变。
- OCR 升档评估：RapidOCR 现为 PP-OCRv6 small；server 级权重已在本地 HF 缓存，扫描件样本上对比收益后再决定。

### Docling 满血模型资源实测（用户提问引出）
- 实测口径：docling 2.138 的下载器为整仓拉取（`snapshot_download` 无 allow_patterns），"选中某模型"即整包体积；数据经 HF 镜像 API（huggingface.co 直连不通、hf-mirror.com 可达）+ ModelScope 实下（RapidOCR）核实，脚本 `scripts/docling_model_inventory.py` 可复跑。
- 档位（实测）：当前生产组合（heron 172MB + TableFormer v1 accurate/fast 358MB + RapidOCR small 32MB）≈0.55GB；官方默认全开（+公式 CodeFormulaV2 640MB、图片分类器 34MB、RapidOCR 另一后端）≈1.3GB；本机可用满血全集（+TableFormer v2 + 全部 VLM/OCR：granite-docling/SmolDocling/SmolVLM/granite-vision/chart2csv/Nanonets/Nemotron/MinerU2.5-Pro）≈40GB；含互斥备选与 Mac 专用 MLX 的全部仓库 ≈42.3GB。
- 运行开销（估算）：主链路 CPU 约 1.5–3GB 内存、无 GPU 需求；VLM 显存 256M 级≈0.5–1GB、1.2B≈2.5–3GB、2B≈5–6GB、3B≈6–7GB、4B≈8–9GB（超本机 8GB）。现状 torch 2.14.1+cpu、onnxruntime 仅 CPU provider → VLM 现在只能 CPU 推理；先做算力升级再评估 VLM 路线。
- 修正一处旧记录错误：HF 缓存里的 PaddlePaddle PP-OCRv5_server_* 目录只有 refs/元数据（~1MB）并无权重；且 PP-OCRv6 实际档位为 small/medium（v6 无 server）。桌面文档第六节第 3 条（OCR 升档）与上一轮进度中的对应表述已同步修正，升档路径改为 medium 档（det 62MB + rec 77MB 实测可下载）。

### 满血模型资源补充：本地磁盘实占与运行内存实测（用户追问引出）
- 磁盘口径澄清：满血 ≈40GB 就是磁盘占用（Docling 整仓拉取，下载量=磁盘量）。本地实占：模型 0.56GB（HF 缓存 530MB=TableFormer 342MiB+heron 164MiB，RapidOCR 33MB）+ 后端运行时（.venv）1.35GB（torch 506MiB 等）≈ 合计 1.9GB；另 data/ 约 186MB、node_modules 约 184MB。
- 运行内存实测（scripts/docling_mem_probe.py，按线上同款 options：heron+TableFormer ACCURATE、force_backend_text）：空闲常驻 API 33MiB + worker 7MiB；解析子进程导入后 331MiB、模型加载完 0.7–0.8GiB、12 页论文全跑峰值 1.5GiB（22.5s）、扫描页+RapidOCR 2 页峰值 1.4GiB（12.1s）→ 最坏 ≈2GiB，15GiB 内存余量充足、无需 GPU。
- 桌面文档"满血资源清单"子节已补齐：表头改为"下载量（=磁盘占用）"、新增"本地已下载磁盘"与"运行内存（实测）"两块；口径备注指向两个可复跑脚本。

### 轻量性对照：Docling 主链路 vs MinerU 本地部署（用户提问引出）
- 主链路为什么轻：角色拆分——版面=RT-DETRv2 小检测器（本机 config 核实，~43M 参数/172MB，判别式不出文本）；文本=字形层零模型（pypdfium2）；OCR 仅扫描页且 small 档（32MB）；表格=TableFormer（213MB 判别式）；提取=规则。全链路 0.55GB、纯 CPU、峰值 1.5GiB、~2s/页。
- MinerU 本地（HF 镜像 API 实测）：VLM 路线（2.5 主推）单模型 ≈2.3GB（MinerU2.5-Pro/2509-1.2B 均 ~2.32GB），强依赖 GPU（生成式逐页解码）；pipeline 路线模型包 PDF-Extract-Kit-1.0 整包 15.1GB（含 UniMERNet/StructEqTable 等多种备选，按需子集量级几 GB）。
- 已补进桌面文档第六节 MinerU 待办条目（作为对测的成本侧对照），供主引擎选型参考。

### 引擎切换成本评估（用户提问"换 MinerU 是否只是换版面分析"）
- 代码事实：layout_parse 是唯一引擎切换点（ParserAdapter 接口 → 统一 normalized 文档，其余 7 阶段不感知）。该阶段内部已按页分组（native/ocr profile）成 run、逐 run 调适配器、按页码合并——"难页分流 MinerU、其余页 Docling"的混合产物合并逻辑天然兼容；缺的只是 run 循环里的适配器选择 + 引擎放行 + 计划里的路由标记。
- 但非零成本：现有 mineru_adapter.py 是 SaaS 远程版（848 行、原文外发、available=False）；本地切换 = 新写"本地子进程 + 输出翻译（HTML 表格/图区/bbox → 统一格式）"适配器（主要工作量），外加本地部署（GPU/CPU 慢）。行为差异三处需回归：全页像素识别（文字页不再免费）/扫描页 OCR 换 MinerU 自带/表格 HTML 形态。
- 注：用户正在手工精简桌面文档（删掉了算力升级/OCR 升档条目与资源清单附块），本轮不再写入该文档。

### 用户三项反馈修复（提交本轮）
- ①审核"重复"：实测根因两条——(a) 同一篇论文 d927d1c3 被解析 7 次，7 个 job 各出一份审核队列（全库 0 组真重复行；771 条大头是这 7 份叠加）；(b) 同一任务内药敏矩阵几十个细胞行，原因列全同（缺单位+实体未关联+需人工核对）且无区分信息，观感即"重复审核请求"。修复：队列行新增"记录"摘要列（值/单位/靶标、序列片段、矩阵坐标"第9页 矩阵r14c6"从 record_id 解析），每行唯一可辨；配合 ② 可删掉旧解析任务清理重复队列。
- ②任务删除：后端 DELETE /jobs/{id}（repository.delete_job 单事务清 11 张依赖表 + artifacts 按 jobs/<id>/ 前缀清 + review_decisions 子查询；活动中任务 409 JOB_ACTIVE；源文档内容寻址保留）+ JobService 连带清磁盘 jobs/<id>/ 目录（resolve 防symlink逃逸）+ 4 个 API 测试；前端任务列表"删除"按钮两段确认（4s 自动过期，避开 webview 原生对话框）。浏览器 E2E 实测：新建任务→删除→列表消失→API 404→磁盘目录/DB 行全清、document 保留。
- ③结果分布统一：PageDistribution 增加图表序列（绿色；图表计数来自 figures API）；序列/活性/表格/图表四个 tab 现在渲染同一份完整分布（此前表格 tab 独有表格计数、图表 tab 无分布）。浏览器实测两 tab 分布文本完全一致（含"图表 1/2"、"表格 1/2"、"活性 192"、"序列 2"计数）。481 backend + 28 vitest + build 全绿。

### C01 完成：评测基线冻结（plan 2026-10-16_domain_review_display_comparison_plan）
- scripts/freeze_eval_baseline.py → reports/baselines/2026-10-16_eval_baseline.json：三样本（论文 d927d1c3/12 页、EP3027649B1 ffb2e5bb/195 页、WO2020156367A1 e25a5881/25 页）SHA-256、git rev、docling/rapidocr/onnxruntime 版本、configs/demo.toml 哈希、各样本已登记 jobs 与已发布基线 id、逐阶段 implementation_version。只读脚本，不写任务状态。MINERU_TOKEN 未配置（云实测按方案记为未完成）。

### C02 完成：序列清点表与范围状态（真实验收）
- 新增 `extract/sequences/inventory.py`（声明级清点：每个 `<210>` 一行，状态 declared/missing/partial/complete/conflict/outside_scope，理由含"需 OCR"候选）+ ST.25 状态机补 `scan_entries`/`saw_400`/同块多标签切分（`<210> 1 <211> 4 <212> PRT` 不再让 SEQ ID 吞标签）+ ExtractStage v4-inventory 写 `sequence_inventory` + `/records` 透出 + 前端"序列清单"面板（统计 chips+逐条表格，非完整项默认置顶，点击跳页）。
- 真实验收（EP3027649B1，195 页全文 native 任务 job-10fbff9f5374）：observed_declarations=82 = `<160>` 82，unseen=0；complete 0 / partial 39 / missing 43 —— SEQ ID 7（p32, DNA, 声明 364）如实为 missing"文本层为空需区域 OCR"（正是 C03 目标）。40 页范围任务（job-0b05fe714374）：范围内 32 声明（与方案核查一致）/ 全文 82 / 未覆盖 50 / covers_full_document=false —— 不冒充全文。
- 发现并修复运行期陷阱：worker/API 在改码后未重启导致新字段缺失（runbook 已记）。18 单测全绿（新增 8）。

### C03 完成：标签分词、页眉过滤、混合区域 OCR（真实验收）
- **标签分词**：同块多标签切分（"<210> 1 <211> 4 <212> PRT" 一行内联）→ SEQ ID 不再吞标签。
- **页眉/行号过滤**：跨页重复行（≥3 页，含数字或极短）从残基流剔除——EP 全文验证 EPB1 型误提 **归零**（修复前候选含 EPB1/EPB1EPB1）。修复过程中捕获关键回归：真实专利里 "<212> DNA"/"<211> 364" 会跨多页重复（多条同长度序列），过滤器必须先排除标签行，否则声明长度/分子类型全被吞（已加回归测试）。
- **DNA 行启发式**：≤8 字符旧规则误杀 60 字符 DNA 块行 → 改为">8 字符的 token 必须全部是核酸字母（或整行单 token 连续串）"，SEQ 9/13 DNA 由此可解。
- **混合区域 OCR（两次修复）**：①inspect 新增 `st25_ocr` 路由（`<400>` 无残基文本的页强制 OCR，不管字符数）；②extract 阶段定向兜底：对声明仍缺失的页整页 300dpi 渲染 + RapidOCR 直调（预算=reparse_max_regions，默认 10 页），**按页内 y 坐标与原生块交织合并**（整页追加会造成残基串位——SEQ 7 的 DNA 曾被错挂到 SEQ 8）+ 模糊去重（防 nativ 已有文本重复计数）。空归一化（纯行号列 OCR）如实记为 missing 不再崩溃。
- 真实验收（job-819fab3181d3，EP3027649B1 全文 195 页 native + 区域 OCR）：**82/82 声明清点**；complete 8、partial 19（真实序列如 SQTTHAPFT 9/9、KVSNRFS 7/7、DIVMTQSPLSLPVTPGEPASISC 23/23）；SEQ 7 DNA 恢复 90 字符（原 0）；EPB1=0。剩余 54 missing 集中在 OCR 预算（10 页）外的声明页，如实标注（提高预算或 OCR 升档可扩）。501 测试全绿（+4 新回归）。

### C04 完成：实体、构建体、变体与真实证据关系（真实验收）
- 实体来源扩展：`#134/#136` 专利构建体编号、变体记法 `W317A`（实体≠自动改写序列，坐标待人工核对）、ST.25 `<223>` 特征注释（构建体+链+CDR）。EntityKind 增 "construct"。
- 证据真实化：实体与关系证据全部注册为**可解析 span**（替换旧的拼串 id）——验收中关系证据经 /evidence 成功解析出原文 "Synthetic sequence: #134 and 136 VH CDR 1 amino acid sequence"（note 定位为 unlocated 兜底时也保留 raw_text，precision=none 如实标注）。
- 关系：note 驱动 实体→序列 关系（has_cdr/heavy_chain/light_chain，真实 note 证据）；SequenceRelation 新增 `link_status` 词表（linked/ambiguous/missing_sequence/outside_scope/not_applicable/no_evidence），未恢复序列 → missing_sequence 或按清点表判 outside_scope，绝不猜配。
- API `/records` 返回 entities/relations/relation_summary；前端序列 tab 新增"关联表"（被测对象｜角色｜序列编号｜状态｜证据按钮）、活性详情显示实体名称+证据按钮。验收（job-f4e73151ce60, EP 30-33）：2 实体（#134/#136 construct）+ 4 关系（has_cdr×1, heavy_chain×2, light_chain×1）全部 linked。503 后端 + 28 vitest 全绿。

### C07 完成：逐页图表与范围切换
- 后端 `GET /figures?page_number=N`（保留无参全文兼容；新增 total 文档总数 + page_number 回显 + 空页说明"第 N 页没有图表（全文共 M 个）"）。
- 前端图表 tab：默认"当前页"（URL scope 参数同步），显式切换"全文"；显示"第 N 页 X 图 · 全文共 M 图"双计数；页标题点击跳页；卡片=大图/下载（独立动作）。缓存键含 job+scope+页码。
- 验收：job-62f430801980 → 全文 total 7；?page_number=2 → total 7 / 1 图；?page_number=8（无图页）→ 0 图 + 说明。504 后端（+1 API 测试）+ 28 vitest 全绿。服务已重启（API 单实例 48224）。

### C06 完成：审核幂等、分组与批量决定
- 幂等确认+回归：review_id 稳定派生（job|record|revision 哈希）；写入 ON CONFLICT 且不重开已决项——新增 2 个单测锁定（重复 upsert 不重复、已决项不被重开）。
- 分组：/reviews 响应新增 groups（对整条筛选队列聚合，不是当前页）：由 record_id 推导 第N页·药敏矩阵/表格/正文、序列记录等。实测 job-62f430801980 → 250 条 = 2 组（第8页药敏矩阵 192 + 第9页药敏矩阵 58）。
- 批量决定：POST /reviews/decisions/batch——两阶段（先全量预检记录存在/仍 open/base_revision==live，任一冲突返回 409 全部冲突清单且不落任何决定；通过后逐项经 apply_decision 提交，逐条保留版本/决定日志/修订号）。前端队列：分组行（组头=全选/折叠计数）、行勾选、"批量确认所选(N)"、冲突横幅。浏览器实测通过。
- 508 后端（+4）+ 28 vitest 全绿。

### C10 完成（协议与分派；真实云实测未完成——无 MINERU_TOKEN）
- v4 批量协议替换错误流程（原实现把 PUT 签名 URL 当公开 PDF URL 再提交）：新 request_upload_slots（POST /file-urls/batch→PUT，凭据不转发给签名 URL）+ poll_batch_entry（GET /extract-results/batch/{batch_id} 按 file_name 定位本文件）+ 身份持久化 remote_identity.json（重试/续跑只轮询不重建远端任务，MockTransport 测试锁定"resume 不再上传"）。源 SHA-256 校验先于上传；401/403→EngineUnavailable；失败态带 err_msg；超时→EngineTimeout。
- 归一化：markdown-only 结果不再伪造 612×792 单页（改抛 MARKDOWN_ONLY_NO_PAGE_STRUCTURE）；zip 结果下载改为裸客户端（not token）。
- 引擎分派：_LAYOUT_ENGINES 增 mineru_http；run 循环按配置调对应 adapter，缺 MINERU_TOKEN 时显式失败（绝不静默回退 Docling）；可用性在任务创建时同样拦截（既有）。
- 测试：+5 MockTransport 协议测试（全流程/鉴权失败/远端失败/resume 免重提交/hash 不符先拒）；513 后端全绿。
- **未完成**：真实 MinerU 云实测（本环境无 MINERU_TOKEN；按方案如实标注未实测，不伪造结果）。

### C08-C12 收尾与四份报告（如实标注）
- C09 完成并提交：问题卡带 severity（错误/警告/提示词表）+ 真实页码（仅从 detail/source 解析）+ 为什么/影响/下一步模板 + severity/page/state 过滤与计数；API 测试锁定。
- C08（阅读顺序/来源对照）与 C11（对比任务/API/页面）未实现——原因：前者是多栏识别+三视图的大改造、后者依赖 C10 云实测环境且任务编排独立；为避免半成品入仓，未动工（已在执行记录中说明）。
- C12：无人工金标 → 全部评测口径标注 not_evaluated；四份报告已交付：执行记录/序列前后/审核展示前后/引擎对比（云实测未完成——无 MINERU_TOKEN）。
- 论文刷新任务（job-8205561af0cc，docling+区域OCR）真实验证：缺少单位原因 255→**0**；论文实体发现 #519/#520/W317A-变体。
EOF
git add reports/2026-10-16_C01_C12_execution.md reports/2026-10-16_sequence_inventory_before_after.md reports/2026-10-16_review_display_before_after.md reports/2026-10-16_docling_mineru_comparison.md progress.md && git commit -m "docs: C01-C12 execution record + before/after + engine comparison reports (honest partial status)" 2>&1 | tail -1
__zcode_status=$?
if [ "$__zcode_status" -eq 0 ]; then pwd -P > '/c/Users/SWQ/AppData/Local/Temp/zcode-a1fb42d5-8ab8-46e2-be9c-251722c0486c-cwd'; fi
exit "$__zcode_status"

### C08-C12 收尾与四份报告（如实标注）
- C09 完成并提交：问题卡带 severity（错误/警告/提示词表）+ 真实页码（仅从 detail/source 解析）+ 为什么/影响/下一步模板 + severity/page/state 过滤与计数；API 测试锁定。
- C08（阅读顺序/来源对照）与 C11（对比任务/API/页面）未实现——原因：前者是多栏识别+三视图的大改造、后者依赖 C10 云实测环境且任务编排独立；为避免半成品入仓，未动工（已在执行记录中说明）。
- C12：无人工金标 → 全部评测口径标注 not_evaluated；四份报告已交付：执行记录/序列前后/审核展示前后/引擎对比（云实测未完成——无 MINERU_TOKEN）。
- 论文刷新任务（job-8205561af0cc）真实验证：缺少单位原因 255->0；论文实体发现 #519/#520/W317A-变体。
