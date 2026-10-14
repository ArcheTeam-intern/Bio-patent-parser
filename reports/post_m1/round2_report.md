# 补充修复方案执行报告：第二轮（B05–B08）

方案：`docs/superpowers/plans/2026-10-14-bio-pdf-post-m1-remediation.md`。
第一轮报告：`reports/post_m1/round1_report.md`。

**测试**：第一轮结束 384 → 本轮结束 **402 passed / 4 skipped**；前端 `npx vitest --run` 4 passed；`npm run build` 通过。

## 提交清单

| 提交 | 任务 | 处置的核查缺陷 |
|---|---|---|
| `277fc1e` | B05 | 整句否决（Figure/SD）误删真实结果；less than 变 claim；undetermined 造 unknown Measurement；mention 为裸 dict |
| `ec4303b` | B06 | MIC/MBC 串列（表级 metric 覆盖全表）；证据 ID 断链（`:cell:` 与 `ev-` 不一致）；层级/合并表头；ParseIssue 缺失 |
| `0b41a5a` | B07 | 显式片段永远无法确认（SEQ_COMPLETENESS 永久 fail）；raw 与结构化字段矛盾的修正可被接受；前端自动提版本；实体无接线 |
| `7b1e3c0` | B08 | 导出 revision 竞态（后台重读 current）；bytes=-100 语义错误；GET 校验失败删除源文件；上传双缓冲 |

## 关键验收证据

**B05（活性）**：方案反例表 10 例全绿：`Figure 1 shows an IC50 of 5 nM`→IC50=5；`(mean ± SD)` 不删测量；一句拆出 `20 µM` condition + `viability 5%` result；`less than 10 µM` 在结果句中→operator `<`（词→符号变换记入 conditions）；`Claim 1:` 才有 claim_threshold。真论文 31 条 typed mentions（undetermined 15/condition 8/statistic 4/result 4），旧版 20 条 unknown Measurement 全部转为带角色与原因的提及。

**B06（表格）**：`Peptide|MIC|MBC` 夹具 MIC=8/MBC=16（修复前 MBC=8/MBC=16）；合并表头 4 条测量 target 来自父头、metric/unit 来自子头（证据指向对应单元格）；S/R/I 矩阵 4 条分类结果 entity=药物列头、target=菌株行头、unit 不适用；inventory/scoring 零测量。证据链：cell 先建真实 span→映射交给提取器，字段证据键统一 value/metric/unit_raw/entity_raw/target_raw/assay_id。真论文产出 6 条 issues：TABLE_INVENTORY(p7)、TABLE_SCORING(p9)、4 条 RESULT_WITHOUT_METRIC（带证据 id），经发布事务落库、API `GET /jobs/{id}/issues` 可读。

**B07（审核闭环）**：显式显示的片段（RRRQRRKKRG）现可确认——修复前 SEQ_COMPLETENESS 永久 fail 使审核循环卡死；接受后 `sequence_scope=fragment` 保持不变。修正 `raw=">128"` 配 `number="12.8"` 被拒。记录带服务端 `_revision/blockers/allowed_actions`；ReviewPanel 删除 Math.max 自动提版本，409 后必须显式重新核对。实体：显式命名建 Entity（document 命名空间稳定 id），测量的 entity_raw 精确匹配才链接，不匹配留 None+审核原因；跨行融合误配（`peptide All`）用句界停用词修复。

**B08（导出/源）**：快照在 POST 内同步捕获（单读事务读 revision+基线+heads+不可变版本），后台只消费快照——barrier 式确定性测试：捕获→审核 r2→构建，导出仍 r1 且 records 报 r2。revision 与内容经 SnapshotService 同事务一致。Range：`bytes=-100`→末 100 字节（修复前返回开头 100）、`N-` 开放式、越界 416、seek 有界读。上传临时文件流式哈希+原子替换；GET 校验失败不再删已登记源文件。

## 未完成（第三轮）

- **B09**：ST.25 纯续页/长核酸行（算法与夹具可做）、真实专利页发现——需本地专利样本核验（样本存在，清单页未定位）。
- **B10**：评测框架（CorpusScope/一对一匹配/not_evaluated CLI）——无 gold 也可实现的确定性部分待做。
- **B11**：有界区域复解析——依赖第二通道环境；接口/预算/映射可先行。
- **B12**：工作台补全（表格/原文/issue 面板）、执行矩阵与统一验收报告。

第三轮全部任务均无算法阻塞，等待执行轮次；真实引擎运行（扫描件 OCR、第二通道）受环境限制时按方案标注"未验证"，不阻塞其余交付。
