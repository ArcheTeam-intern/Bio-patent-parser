# Biological PDF parser research and source acquisition

## Goal
Compare open-source PDF parsers with Tencent WeKnora for biological papers and patents, acquire a suitable upstream repository locally, and propose an accuracy-oriented workflow.

## Phases
- [complete] Inspect official documentation and parser source; compare architecture, provenance, OCR, tables, and license constraints.
- [complete] Select and clone the preferred upstream into a separate directory in this workspace.
- [complete] Record the comparison, proposed workflow, and limits of available evidence.
- [complete] Verify checkout identity and report paths and next steps.

## Scope
Research, source acquisition, and design only. Implementation and performance benchmarking are later work.

## Detailed plan handoff requested by user
- [complete] Verify workflow framework choices and current parser API contracts.
- [complete] Write a self-contained architecture specification covering routing, evidence, sequences, activities, review, and evaluation.
- [complete] Write phased implementation tasks with file boundaries, concrete acceptance fixtures, and migration rules.
- [complete] Review the documents for consistent contracts and complete handoff; open the main plan.

## Errors
- 2026-10-14: 一次审查辅助JSON读取使用默认GBK失败，改显式UTF-8；一次文档patch因hunk顺序失败，调整顺序成功。均非业务代码问题。
- Initial planning skill path used the wrong root; resolved to C:/Users/SWQ/.agents/skills/planning-with-files/SKILL.md.
- GitHub API tree request hit an unauthenticated shared-IP rate limit; use raw files and normal Git transport instead.
- Guessed WeKnora requirements.txt and docling_parser.py paths returned 404. Official parsing documentation and existing source files supplied the necessary architecture evidence.
- A guessed Docling custom conversion URL returned 404; resolved via repository examples and current documentation.

## 2026-10-14 深入优化方案
- [complete] 核对实际阶段实现、抽取器、证据契约与样例制品，区分确定缺陷和待评估策略。
- [complete] 形成分阶段实施方案：可靠性、解析路由、领域提取、审核前端、评测，共17个任务。
- [complete] 检查路径、任务依赖、回归样例、兼容迁移与模型交接指令；17任务编号连续，代码围栏匹配，无未解决占位符。

## 2026-10-14 M1执行后补充方案
- [complete] 核对两份执行报告、提交cab949a及新job真实制品，复跑现有测试。
- [complete] 排查M0/M1完成项的接线、精度声明与测试覆盖缺口，编写B01–B12新优先级方案及独立核查报告。
- [complete] 自检新增任务依赖、端到端验收条件和执行交接；12任务编号连续、代码围栏闭合、无未解决占位符。

## 扫描件OCR恢复（继续执行）
- [complete] 确认25页任务job-6031752e8318失败在Tesseract缺失，RapidOCR已装但ONNX Runtime缺失。
- [complete] 终止取消后遗留的uv安装锁，补装ONNX Runtime；新增auto/显式OCR选择与语言映射，12项针对性测试通过。
- [complete] 真实扫描件解析成功：25/25页OCR出文本（20397字符）、389条证据、13条问题；修复parser名称映射与SEQ ID引用证据碰撞，提交71e7127；全量427 passed。
- [complete] 核查全量测试、运行服务/worker与最终解析结果，已写入progress.md（0条自动提取记录的原因如实记录）。

## Worker存活提示（继续执行）
- [complete] worker_presence表+心跳线程（每5s、独立连接、退出清理）；GET /api/v1/queue/status。
- [complete] 前端WorkerHint横幅（JobsPage/工作台）：无在线worker且存在排队/运行任务时显示启动命令，worker上线自动消失。
- [complete] 修正get_settings忽略create_app注入设置（请求此前回落data/demo默认值）。
- [complete] 真实验证横幅出现/消失与任务启动；431 passed、web build通过。

## 上传失败与页码联动（用户反馈）
- [complete] 查明失败哈希对应 HTML 专利网页；归档三个伪 PDF，补充两个真正 B1 PDF 并通过上传接口。
- [complete] 上传提示包含实际格式与原始文件名；前端内容预检和错误清理。
- [complete] 统一 URL/PDF/结果页码，默认当前页筛选，全文切换，真实证据页映射，手动翻页取消旧证据。
- [complete] PDF 适合宽度、证据自动滚入视野、标签与网格布局修正；构建及真实浏览器核对，报告见 reports/2026-10-14_frontend_upload_page_sync.md。

## 前端收尾验收（按 2026-10-14-frontend-acceptance-and-commit.md 执行）
- [complete] 阶段A：A1–A16 逐页浏览器验收（真实 API/制品）。A12 失败（运行详情为静态占位），其余 pass；A15 序列 typed patch 按设计不可用已如实记录。
- [complete] 阶段B：B1 BOM 分类；B2 record_pages 降级；B3/B4 records/上传测试；B5 useEvidence+Navigator 测试；B6 查询键共享；B7 运行详情真实数据（JobResponse 暴露 engine/config_hash）；B8 状态标签一致；B9 序列隐藏修正按钮。
- [complete] 阶段C：446 passed / vitest 14 passed / build 通过；服务重启复核；提交 c49124c、285a3cf 及本文件与报告；验收记录见 reports/2026-10-14_frontend_upload_page_sync.md。
