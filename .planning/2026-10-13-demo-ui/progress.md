# 进度

2026-10-13：阅读项目合同、状态机和存储；开始官方资料核对。用户确认研发与审核定位。只创建方案和设计制品，不修改应用源码。

2026-10-13：保存完整设计与实施入口，包含四视图、两种工作台布局、API、worker、坐标、审核版本和 D0–D5 分阶段验收；创建内联可交互草图。正在检查文档一致性和草图结构。

2026-10-13：完成设计自审，明确修正记录 value.raw 与不可变原始证据的边界。草图用已安装 Edge 完成浏览器检查：活性区间修正后展示 range，示例确认可更新状态，未知序列禁止确认；无 JS 错误，1024/736/320 宽度无水平溢出，截图已查看。仅检查设计草图，没有运行或修改应用测试。设计稿和实施入口准备交付，业务前端/API 尚未实现，未提交 Git。

2026-10-13（实施轮）：按设计稿完成 D0–D4 实施并分阶段提交（0968fd9 / c866439 / 0064867 / 4d9c812 / d193e9c）。
D0 修复审查报告 13 项缺口（gitignore、完整工作流 handlers + application 层、CheckResult 统一、
未知残基保留、区间/误差完整解析、Docling prov bbox、MinerU 表格/页范围/源身份、状态机
必要阶段/审核聚合/恢复哈希校验、评测一对一完整元组匹配）。D1 FastAPI + 持久队列 worker。
D2 React 工作台（浏览器验证 PDF 渲染与证据降级）。D3 原子审核（409 冲突、未知残基阻断、
拒绝留痕）。D4 固定 revision 导出（CSV 独立列、FASTA 拒绝空导出）。298 passed, 4 skipped。
D5 对比页实现"不可比较"提示，完整对比按设计待人工 gold 后交付。未实现：跨页序列证据
1/N 遍历、表格视图完整 UI、原文双栏对比视图——工作台对应标签为占位说明。

2026-10-14（精度优化轮）：按 docs/superpowers/plans/2026-10-14-bio-pdf-accuracy-optimization.md
执行 M0（T01–T03）与 M1（T04–T08），7 个提交（8a2740b…c9b5fd7）。351 tests 全绿。
真实论文验证：待审核 95→25、序列 12→5（两段真实短片段从漏提变为找到）、活性 83→20；
证据全部字符精度。报告见 reports/2026-10-14_m1_sample_before_after.md 与
reports/2026-10-14_optimization_execution.md。T09–T17 未执行（原因见执行报告第 3 节：
专利样本缺失、人工 gold 缺失、前端轮次、依赖链）。

2026-10-14（post-M1 修复轮，第一轮+B05）：按 reports/2026-10-14_post_m1_review.md 与
docs/superpowers/plans/2026-10-14-bio-pdf-post-m1-remediation.md 执行。
提交 1407e11(B01 前端证据导航)/6035161(B02 源ID+精度拆分+融合去重)/a34fa32(B03 fencing+发布事务)/
4ffca0a(B04 逐页执行+配置生效)/277fc1e(B05 按mention判断活性)。384 tests。B06-B12 未开始。
报告：reports/post_m1/round1_report.md。
