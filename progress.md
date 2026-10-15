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
