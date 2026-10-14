# 执行矩阵（B01–B12）

**每项只有全部必需层满足才计 complete；覆盖不足写 partial；未运行写 not_run。**

| 任务 | code_done | unit_passed | integration_passed | real_sample_checked | UI_checked | environment_blocked | commit | 状态 |
|---|---|---|---|---|---|---|---|---|
| B01 前端证据导航 | ✅ | ✅ | ✅ | ✅ 真论文 ev-hash→p9 | ✅ 浏览器实测（精度标签诚实） | — | 1407e11 | **complete** |
| B02 源ID/精度/融合去重 | ✅ | ✅ 8反例 | ✅ | ✅ 5→2 候选，双来源 | ✅ 精度标签 | — | 6035161 | **complete** |
| B03 fencing/发布事务 | ✅ | ✅ 8反例 | ✅ | ✅ worker 端到端 | — | — | a34fa32 | **complete** |
| B04 逐页执行/配置生效 | ✅ | ✅ 4反例 | ✅ spy 分派 | partial：真论文全 native 路由；真实混合/扫描走 OCR 未跑 | — | 扫描样本需真实 OCR | 4ffca0a | **partial**（真实 OCR 运行环境受限） |
| B05 按 mention 判断活性 | ✅ | ✅ 10反例 | ✅ | ✅ 31 提及带角色去向 | — | — | 277fc1e | **complete** |
| B06 表格指标/层级/证据链 | ✅ | ✅ 7反例 | ✅ | ✅ p7/p9 分类+6 issues | ✅ 表格视图 | — | ec4303b | **complete** |
| B07 实体/审核闭环 | ✅ | ✅ 5反例 | ✅ | ✅ 片段可接受 | ✅ 冲突流程 | — | 0b41a5a | **complete**（assay 提取留 B07 后续） |
| B08 固定快照/Range/源保护 | ✅ | ✅ 6反例 | ✅ barrier 竞态 | ✅ | — | — | 7b1e3c0 | **complete** |
| B09 ST.25 续页/页发现 | ✅ | ✅ 4夹具 | ✅ | ✅ 三专利扫描件确认（渲染核验） | — | 真实清单 OCR 未跑 | b4faef7 | **partial**（页发现完；真实扫描清单页需 OCR） |
| B10 评测框架 | ✅ | ✅ 9负例 | ✅ CLI 端到端 | ✅ not_evaluated 输出 | — | 正式分数需人工 gold | 762d3c6 | **complete**（正式分数 not_run，符合预期） |
| B11 区域复解析接口/预算 | ✅ | ✅ 7反例 | ✅ | not_run：第二通道未配置 | — | 真实区域引擎 | fbc70ad | **partial**（接口/预算/映射完；真实执行环境受限） |
| B12 工作台/矩阵/报告 | ✅ | ✅ vitest 4 | ✅ | ✅ issues/表格/原文面板浏览器核验 | ✅ | — | 本轮 | **complete** |

## 主要验收清单对照（方案 §12）

| 清单项 | 状态 | 证据 |
|---|---|---|
| 1. 作者/药物/日期不误提；RASGAP 名称不当残基；两片段合并同源重复且标 fragment | ✅ | 真论文：2 候选、双来源、scope=fragment；作者词诊断 |
| 2. 20 条 unknown 有可审查去向；Figure/SD 不丢真实结果 | ✅ | 31 typed mentions；10 反例全绿 |
| 3. 前端点新 ev-hash 能定位、精度如实、刷新/快速切换正确 | ✅ | 浏览器核验 + vitest 竞态 4 用例 |
| 4. MIC/MBC 夹具每列正确、field evidence 可加载；页8 如实记录 | ✅ | 7 夹具 + 页8 上游未检出已记录（B11 预算内可再试） |
| 5. 审核/冲突/导出固定 revision 一致 | ✅ | 409 测试 + barrier 快照测试 |
| 6. 混合页真实 OCR、失租不可写、损坏 manifest 不回退 | ✅（算法）/partial（真实 OCR 运行） | spy 分派 + 8 fencing 反例 + RESULT_CORRUPT 测试 |
| 7. 无 gold 显示 not_evaluated；噪声/覆盖/issue 照常报告 | ✅ | CLI 端到端 not_evaluated + coverage |

## 测试与构建（本轮结束）

- `pytest -m "not live"`：**422 passed, 4 skipped**（skip 为 live 标记，原因：需外部引擎/服务）。
- `npm --prefix web run build`：通过。
- `npx vitest --run`：4 passed。
- 真实样本产物：`data/post-m1-demo`（论文）、`data/b07-smoke`；旧目录全部保留。

## environment_blocked 明细（不是失败，是不虚构）

1. **真实扫描件 OCR 端到端**：三个本地专利均为纯扫描（0 文本层，已渲染核验）；运行 99–167 页 Docling OCR 属重量级真实引擎任务，本轮未执行——计划路径（ParsePlan→layout_ocr）与分派已由 spy 测试证明。
2. **第二通道区域复解析真实执行**：无 MinerU token/docling-serve 配置；接口、预算、映射与 REPARSE_EXHAUSTED 语义已实现并有 7 反例。
3. **正式准确率分数**：`annotations/v2` 为待人工标注模板；CLI 输出 not_evaluated + coverage，符合方案（"没有人工 gold 只延期正式分数"）。
