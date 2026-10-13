# PDF Demo 前端实施入口

目标用户：研发调试与人工审核。设计稿已形成，尚未开始业务前端/API 实现。

阅读顺序：

1. `superpowers/specs/2026-10-13-bio-pdf-demo-ui-design.md`：页面、交互、API、证据坐标、版本审核与 D0–D5 分解。
2. `../reports/2026-10-13_implementation_review.md`：现有实现的 13 项缺口与复现证据。
3. `superpowers/specs/2026-10-13-bio-pdf-workflow-design.md`：原领域合同与工作流设计。
4. `superpowers/plans/2026-10-13-bio-pdf-workflow-implementation.md`：原后端实施任务。

## 核心选择

- React + TypeScript + Vite，PDF.js 预览，FastAPI API，现有 Python 状态机 + SQLite + 不可变制品。
- 页面：任务列表、解析工作台、审核队列、同源解析器对比。
- 默认布局：原 PDF、领域结果、证据详情/审核；审核聚焦模式用于连续人工处理。
- 不增加 LangChain；不直接改造 WeKnora；前端通过 API 保留后端迁移边界。

## 实施顺序

先 D0 修复后端并接通完整提取流程，再 D1 API/队列/前端框架，D2 PDF 证据联动，D3 审核持久化与版本冲突，D4 导出闭环，最后 D5 对比与人工准确率基线。

静态布局可以先用明确标识的 fixtures 完成，但真实演示必须使用真实 API、源 PDF、结果与证据。不能将候选数量、解析成功或截图布局作为提取准确率。

## 必须保持的约束

- 源文件、document_id、截图、解析内容均来自同一 PDF。
- 不删除未知残基，不把区间/误差截成单标量，不混淆实测与 claim 阈值。
- 现有 bbox 是 points、左下原点；显式处理源裁切/旋转、PDF.js viewport 和 devicePixelRatio。
- 缺坐标就降级提示，不猜框；原始解析制品不可被人工修改覆盖。
- 审核提交需要原子写入、base_revision 冲突保护、typed patch、重校验、新版本和导出快照。
- 耗时解析在独立 worker 执行，HTTP 创建快速返回；恢复依赖持久状态与制品哈希。
- 无人工 gold 就显示“尚无准确率基线”；同源、同页、同规则才比较指标。

可发送给后续执行模型：

> 请按 docs/BIO_PDF_DEMO_UI_HANDOFF.md 指向的设计，实施面向研发调试和人工审核的生物 PDF Demo。先修复审查报告中影响完整工作流、统一合同、源身份和证据的缺口，再按 D1–D4 完成上传、任务状态、PDF/结果联动、人工修正与导出。保留 Python 解析核心，新增独立 React/TypeScript 前端与 FastAPI 应用服务。所有审核必须真实持久化并有版本冲突保护；不要伪造结果、进度和准确率。交付可重复的真实样本演示以及明确标注的未完成项。D5 对比与 baseline 在 matching 修正、同源检查和人工标注具备后执行。
