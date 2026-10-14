# 补充修复方案执行报告：第三轮（B09–B12）与总结

方案：`docs/superpowers/plans/2026-10-14-bio-pdf-post-m1-remediation.md`。
前三轮报告：`round1_report.md`（B01–B05）、`round2_report.md`（B05–B08）。
执行矩阵：`execution_matrix.md`；制品清单：`artifact_inventory.json`。

**测试**：351（接手）→ **422 passed / 4 skipped**；vitest 4 passed；`npm run build` 通过。

## 第三轮提交

| 提交 | 任务 | 内容 |
|---|---|---|
| `b4faef7` | B09 | ST.25 纯续页（无 tag 的第 2 页有独立证据）、60 字符 DNA 行、零残基 listing→issue（不再崩溃）、同号冲突保留双方；`page_discovery` 全页扫描器 |
| `762d3c6` | B10 | CorpusScope/verified 负例/未标注→not_evaluated；inline 匹配（页码位置边、无页码不匹配）；同号错内容 FN+FP；作者误提进入 FP；`bio-pdf evaluate` CLI |
| `fbc70ad` | B11 | 对齐 diff（Needleman-Wunsch，修复 `conflicts` 越界）；ReparsePlanner 有界预算（10 区域/2 尝试/120s，耗尽→REPARSE_EXHAUSTED，页码从真实 source id 解析） |
| `3b2984b` | B12 | 表格/原文/问题三个真实视图；URL page 参数驱动；执行矩阵 + 制品清单 + 浏览器核验 |

## B09 的真实发现（已渲染核验）

三个本地专利样本（WO2017060493A1 167 页 / US20240026005A1 142 页 / WO2021207200A2 99 页）
**全部页面文本层为 0 字符**：纯扫描件。页发现器对全部页面输出 `needs_render`（下一步 OCR 的
输入），并对 p51 渲染人工核验（`data/patent_wo_page51_preview.png`，为专利正文页图像，非清单页）。
详见 `patent_page_discovery.md`。ST.25 解析算法本身已由确定性夹具完全验证，不依赖 OCR。

## 核查报告 12 项发现的最终状态

| # | 发现 | 状态 | 提交 |
|---|---|---|---|
| 1 | 前端从 ID 猜页码 | ✅ 修复（浏览器核验） | 1407e11 |
| 2 | 跨页 block ID 重复/证据 hash 缺页号 | ✅ 修复 | 6035161 |
| 3 | 段落 bbox 冒充字符精度 | ✅ 修复（精度拆分 + 诚实标签） | 6035161/1407e11 |
| 4 | 同位置双通道重复提取 | ✅ 修复（有序集合消费+来源合并） | 6035161 |
| 5 | 混合页未实际 OCR | ✅ 算法+分派证明；真实扫描端到端标记 environment_blocked | 4ffca0a |
| 6 | 配置未达 converter | ✅ 修复（tableformer/ocr_lang spy 证明） | 4ffca0a |
| 7 | 不可用引擎静默替换 | ✅ 修复（入队前 422） | 4ffca0a |
| 8 | 租约保护不完整 | ✅ 修复（token 条件事务，8 反例） | a34fa32 |
| 9 | manifest 无自校验 | ✅ 修复（hash+语义） | a34fa32 |
| 10 | 损坏发布清单可回退 | ✅ 修复（RESULT_CORRUPT） | a34fa32 |
| 11 | 导出 revision 竞态 | ✅ 修复（barrier 测试证明固定快照） | 7b1e3c0 |
| 12 | 表格串列/证据断链 | ✅ 修复（7 夹具+真论文 p7/p9 分类） | ec4303b |
| 附 | 整句否决误删结果 | ✅ 修复（10 反例） | 277fc1e |
| 附 | 片段永久无法确认 | ✅ 修复（B07） | 0b41a5a |
| 附 | Range 后缀语义错误/源被删 | ✅ 修复（B08） | 7b1e3c0 |

## environment_blocked（不虚构，均有接续路径）

1. **真实扫描件 OCR 端到端**：需运行 99–167 页 Docling OCR；ParsePlan→layout_ocr 分派已证明。
2. **第二通道区域复解析真实执行**：无 MinerU token/docling-serve；接口/预算/映射/耗尽语义已实现并测试。
3. **正式准确率分数**：`annotations/v2` 为人工标注模板；CLI 输出 not_evaluated + coverage（方案允许的延期项）。

## 遵守的约束（方案 §0/§14）

- 数据写 `data/post-m1-demo`；旧 `data/demo`、`data/optimization-demo` 保留可查。
- 未改写历史报告使其看似正确——两份原执行报告的差异在 `round1_report.md` 中逐项更正。
- 未通过过滤数字/伪造表/猜字段/静默回退引擎来"改善指标"；所有降级（缺证据、未检出、未标注）均为显式状态。
- 原始证据始终可查：证据 ID 内容化、字符偏移可验证、发布清单不可变且按 baseline 指针读取。
