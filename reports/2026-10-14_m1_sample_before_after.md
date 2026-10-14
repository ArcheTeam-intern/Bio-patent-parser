# M1 验收：同论文复跑对比（T06–T08）

日期：2026-10-14。基线 HEAD：`c9b5fd7`。数据目录：`data/optimization-demo`（旧 `data/demo` 保留可审查）。

**样本**：TAT-RasGAP317-326 论文（SHA-256 `d927d1c3…da35`，12 页），`docling_sdk` 引擎。
**旧结果**（优化前，`data/demo` 任务 `job-8415a1c6daa0`）：序列候选 12、活性候选 83、待审核 95。
**新结果**（`job-95b61e00997d`）：序列候选 5、活性候选 20、待审核 25。

**没有人工 gold：以下全部是逐项抽查与回归，不是准确率。**

## 1. 序列候选变化

| 项 | 旧 | 新 |
|---|---|---|
| 作者名 `Christian Widmann` | 候选（误提） | 不再出现；`NON_SEQUENCE_ROLE` 诊断 |
| 药物/菌名（Benzylpenicillin 等） | 候选（误提） | 不再出现 |
| 10 残基真实片段 `RRRQRRKKRG` | 漏提 | **找到**（2 处来源：摘要与正文，分别成记录） |
| 10 残基真实片段 `DTRLNTVWMW` | 漏提 | **找到**（同上） |
| `RASGAP`（名称残留） | — | 仍出现 1 条，进入审核（残留误提，见"真实缺失"） |
| 无上下文长字母串（如 `ORIGINALRESEARCH`） | 候选 | 仅 `NO_SEQUENCE_CONTEXT` 诊断 + 原始提及，不进候选 |

被过滤原因（诊断中可见）：`NON_SEQUENCE_ROLE`（角色排除）、`NO_SEQUENCE_CONTEXT`（无上下文）、`NO_NUCLEIC_CONTEXT`（小写串无核酸提示）。

## 2. 活性候选变化

| 文本 / 位置 | 旧行为 | 新行为 |
|---|---|---|
| `Accepted: 17 May 2017` | 提成 `17 M` | 0 条；`metadata` 提及 |
| `cells were treated with 20 µM peptide A for 2 h` | 当实测 | `condition` 提及，不成 Measurement |
| `95% confidence interval` | 当实测 | `statistic` 提及 |
| `MIC was 1–10 nM` | 曾截成 10 nM | range 完整（1/10 独立保留） |
| `IC50 = 1.2 ± 0.3 nM` | 曾截成 0.3 nM | plus_minus 完整 |
| `Claim 1: … MIC less than 10 µM` | 实测 | `claim_threshold` |
| 83 条（含 Used/Notes/Animal/Score 表格噪声） | — | 20 条；表格非指标列不再产生 Measurement |

提及角色分布：condition 12、statistic 8、metadata 2、unclassified_letters 9。

## 3. 表格状态

| 页 | 表 | 状态 |
|---|---|---|
| 页 7 | 菌株用途表（Strain/Used in/Notes） | 检出（25×4）；**归类为 TABLE_UNCLASSIFIED**，不产生伪活性；header_role 已按 Docling 标记 |
| 页 9 | 动物评分表（Parameter/Animal ID/Score） | 检出（17×3）；同样不产生伪活性 |
| 页 8 | TABLE 2 药敏表（S/R/I） | **上游 Docling 未检出**（该页无 table 结构，文本散为普通块）：按方案归为"上游未检出"，需 T16 区域复解析（第二通道）后由 T09 分类提取；不伪造 |

## 4. 证据质量

- 70 条证据 span **全部**为字符/单元格精度（`location_precision=character`），旧版为"有 bbox 但只跳页"。
- 证据 ID 为内容哈希（不解析 ID 取页码）；多页序列按来源页注册多个 span。
- 未覆盖：真实多页序列（本论文无 ST.25 跨页清单）、表格 cell 级证据（本论文无合格指标表）。

## 5. 真实缺失与未覆盖项

1. **`RASGAP` 残留误提 1 条**：出现在强提示句（含 "sequence" 字样）中；需上下文距离阈值进一步收紧（T16 或后续）。
2. **页 8 药敏表**：上游未检出，待 T16 第二通道。
3. **20 条 `unknown` 指标活性**：多为图注语境百分比，角色判别未细分为 statistic/result 的子情况；保留审核。
4. **无人工 gold**：本报告仅为抽查；未运行评测（T15 未实施）。
5. 表格未生成 Measurement（无合格表）——页 7/9 的表级语义审核项已建（TABLE_UNCLASSIFIED 诊断），T09 的完整表分类器未实施。

## 6. 工程验证

- `pytest -m "not live"`：351 passed, 4 skipped。
- 新回归：`tests/unit/test_activity_prose.py`（T08 固定表 9 例）、`test_sequence_candidates.py`（T07 三入口）、`test_parser_fusion.py`、`test_stage_manifest.py`、`test_concurrent_transactions.py`、`test_parse_plan.py`、`test_contract_compat.py`、`test_evidence*`。
- 恢复与并发：fencing/租约/409/审核幂等均已有测试（T02/T03）。
