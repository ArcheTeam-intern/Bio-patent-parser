# 2026-10-14 精度优化方案执行报告（M0/M1）

执行对象：`docs/superpowers/plans/2026-10-14-bio-pdf-accuracy-optimization.md`。
执行范围：**M0（T01–T03）与 M1（T04–T08）全部完成**；M2–M4（T09–T17）未执行，原因见第 3 节。
测试基线：351 passed, 4 skipped（执行前 298）。前端 `npm run build` 通过（未新增前端改动）。

## 1. 已完成的提交

| 提交 | 任务 | 内容 |
|---|---|---|
| `8a2740b` | T01 | WorkflowConfig（严格 TOML、effective_config 持久化+哈希）；domain schema 1.1.0；compat 读取器（旧 entity_id→entity_raw，未知主版本拒绝）；EvidenceFragment/语义角色/header_role 等契约扩展 |
| `c9f17e3` | T02 | stage-run 隔离目录；内容化 cache key（含实现/引擎版本）；阶段 manifest（全量输出哈希）；恢复核验全部输出；发布基线 result-manifest；ResultReader；artifact occurrence ID；页覆盖门禁；表格指标护栏（MetricDefinition） |
| `366c760` | T03 | ClaimedJob（worker_id/generation/lease）；lease_counters 单调代际；心跳 rowcount fencing；worker 独立连接心跳线程；lease 丢失停止发布；resume 意图持久化+409 JOB_RUNNING；API 每请求独立连接；审核项稳定 ID、不重开 resolved |
| `1375e44`+`bc1db…` | T04 | ParsePlan（force/disabled/auto 语义、OCR_UNSUPPORTED 422、SCAN_WITH_OCR_DISABLED）；InspectStage 产出计划、LayoutParseStage 消费；docling 子进程按连续段转换（{2,4} 不再拉页 3）；引擎超时+进程树清理 |
| `6532adb` | T05 | 表格 cell ID 含表序号（同页多表不碰撞）；ID 冲突→NORMALIZATION_ID_COLLISION；缺尺寸不再伪造 612×792（跳过+记录，覆盖门禁报错）；Docling header 标记→header_role；structure_issues 重叠检测；MinerU occupancy grid；双通道融合（重叠+文本对齐，冲突保留双方并阻断自动接受） |
| `f996142` | T06–T08 | 提取时生成证据（不透明 ID、字符 offset 校验、多页 fragments、EvidenceIndex 冲突检测）；validate_evidence dict 访问修复；序列三入口规则（方案固定回归表）；prose 单位词边界+角色判别（metadata/statistic/condition→RawMention；claim→claim_threshold） |
| `c9b5fd7` | 实跑修正 | Docling label→SemanticRole（caption 此前从未被排除）；强/弱提示词分级（白蛋白等名词仅在括号紧邻时生效）；记法串与散文串过滤；layout 引擎名 `docling_sdk` 别名（此前结构解析被静默跳过） |

## 2. 真实验证（无人工 gold，仅抽查/回归）

详见 `reports/2026-10-14_m1_sample_before_after.md`。要点：

- 同论文（TAT-RasGAP，12 页，Docling）：待审核 95 → 25；序列 12 → 5；活性 83 → 20。
- **两段 10 残基真实片段（RRRQRRKKRG、DTRLNTVWMW）从漏提变为找到**，且各自保留两处来源位置。
- 作者名/药物/菌名不再进入序列候选（诊断可见过滤原因）；`17 May 2017` 不再产生 `17 M`。
- 70 条证据全部字符/单元格精度（旧版只跳页）。
- 页 8 药敏表：上游 Docling 未检出（方案 T16 预期内），未伪造。
- 残留：`RASGAP` 1 条误提、20 条 unknown 指标活性（多为图注）——均进入审核。

## 3. 未执行任务与原因

| 任务 | 状态 | 原因 / 前置 |
|---|---|---|
| T09 表格分类与多层表头 | 未执行 | 依赖真实表结构（本论文仅页 7/9 两表且均为 inventory/scoring）；**M1 护栏已先行**：非指标表不产生 Measurement。页 8 真实药敏表需 T16 先拿到结构 |
| T10 ST.25 跨页/页眉/编号引用 | 未执行 | 需要专利样本（当前语料为论文，无 ST.25 多页清单）。现有 st25 已支持多页 fragments 证据注册 |
| T11 实体/实验方法/关系接线 | 未执行 | M2 范围；需要实体提取设计落地（entity_id 现为 None，entity_raw 保留原文） |
| T12 校验/审核闭环增强 | 部分 | 现有原子审核+409+未知残基阻断已可用；ACT_METRIC_SUPPORTED 等新检查待 T09/T11 的 metric/实体数据 |
| T13 快照/导出/源文件 I/O | 部分 | 导出快照（固定 revision）已实现；Range 后缀 range 计算 bug（`bytes=-500`）未修 |
| T14 工作台界面增强 | 未执行 | 需要前端开发轮；当前工作台可用（PDF+记录+审核） |
| T15 评测与 gold | 未执行 | 依赖人工标注；方案明确"无 verified gold 输出 not_evaluated" |
| T16 有界区域复解析 | 未执行 | 依赖 T09 语义分类与引擎预算；页 8 缺口已明确记录 |
| T17 ST.26 附件 | 未执行 | 方案列为后续增强 |

## 4. 遵守的方案约束

- 旧 `data/demo` 未改写；新结果在 `data/optimization-demo`。
- 未伪造表格、未把未知字段写成默认值、未删除困难候选、未 mock 引擎输出。
- 未知残基占位保留；区间/误差/比较符不截断；claim 阈值与实测分离。
- 每个任务独立提交；提交不含用户上传原件（`data/` 不入库）。
