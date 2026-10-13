# 设计依据

- 用户明确选择研发调试和人工审核。
- 当前已有 Python contracts/workflow/storage/parser/domain 模块，没有 HTTP API 和前端依赖。
- ReviewAction 已有 confirm/correct/reject，ReviewDecision 含 base_revision；现有 append_review_decision 仅追加历史，界面需要完整原子提交语义。
- ActivityValue 已有 scalar/range/plus_minus/categorical/unparsed，数值使用字符串；前端应忠实展示。
- BoundingBox 现有合同约定 PDF points、左下原点；不能设计为默认左上坐标。
- EvidenceSpan 的坐标和页码可为空；缺少位置时 UI 必须明确降级，不能伪造高亮。
- 2026-10-13 审查发现 13 项缺口；前端方案须包含接线与修复依赖。
- 官方 Vite 有 react-ts 模板；PDF.js getViewport 返回页面变换；FastAPI 重型计算提醒支持本项目独立 worker 选择。
- 源坐标显式增加 canonical_to_pdf_user 变换，避免 PDF.js 二次旋转/裁切；overlay 用 CSS pixels。
- 首版轮询 1.5 秒，持久 worker 单并发；审核原子提交新版本，导出固定 revision。
- PDF.js PageViewport 类文档 URL 打开失败；采用官方源码和 PDFPageProxy 文档核对。
