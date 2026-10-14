# 补充修复方案执行报告：第一轮（B01–B04）+ B05

方案：`docs/superpowers/plans/2026-10-14-bio-pdf-post-m1-remediation.md`。
核查依据：`reports/2026-10-14_post_m1_review.md`。
数据目录：`data/post-m1-demo`（旧 `data/demo`、`data/optimization-demo` 保留）。

**测试**：修改前 351 passed / 4 skipped → 本轮结束 **384 passed / 4 skipped**。
每条命令实际输出见各提交；前端 `npm run build` 通过，新增 `npx vitest --run`（4 导航竞态测试）。

## 提交清单

| 提交 | 任务 | 记录的核查问题 |
|---|---|---|
| `1407e11` | B01 | 前端从 ID 猜页码、bbox 永为 null、假字符精度 |
| `6035161` | B02 | 229 个跨页重复 block ID；证据 hash 缺页号；段落 bbox 冒充字符精度；一页一段落只配对一行 native 造成重复提取；伪造第 1 页证据 |
| `a34fa32` | B03 | lease_guard 只看延迟标志；阶段完成/审核写入/status/发布不在带 token 的事务；manifest 自身无校验；损坏发布清单可静默回退；worker_id 不唯一 |
| `4ffca0a` | B04 | 混合页不实际 OCR；targets 未进配置；parser_options 未进 converter；docling_serve/mineru 都跑 SDK；不可用引擎静默替换 |
| `277fc1e` | B05 | 整句否决（Figure/SD）误删真实结果；less than 变 claim；undetermined 造 unknown Measurement；mention 为任意 dict |

## 每项核查缺陷的当前状态

| 核查发现 | 状态 | 证据 |
|---|---|---|
| 证据定位回归（前端猜 ID） | **已修复** | 浏览器实测：点击 ev-hash → PDF 跳第 9 页；面板显示"段落区域，文本偏移 216–226（无字符级坐标）" |
| 重复提取（同位置两路） | **已修复** | 融合改为有序集合消费：真论文从 5 候选（4 个重复）→ 2 个候选，每个带 layout+native 双来源 fragment |
| 精度声明过强（bbox 覆盖整段） | **已修复** | `text_precision/geometry_precision` 拆分；段落 bbox 报表为 `block`；前端标签如实 |
| 证据 47 vs 报告 70 | **已核实** | 47 为当时 extract.json 实况；本轮数据已重跑，见下方真实数字 |
| 表格串列（MIC/MBC） | **未修复** | **B06**（反例已复现，下一步） |
| 真实结果漏提（Figure/SD） | **已修复** | 10 个反例测试 + 真论文 31 条提及带角色去向 |
| 混合页未实际 OCR | **已修复** | spy 测试证明页 2 走 OCR profile、页 1 走 layout；真引擎混合样本待有扫描件时运行 |
| 租约保护不完整 | **已修复** | 8 个 fencing 反例：gen1 在阶段完成/发布两个提交门被拒，成功与异常路径均不能写 |
| 导出固定快照不完整 | **未修复** | **B08**（B03 已铺好 DB 基线指针） |

## 真实论文数字（`data/post-m1-demo`，docling_sdk）

- 复跑任务 `job-0b23d755faf9`：序列候选 2（RRRQRRKKRG、DTRLNTVWMW，各含双通道来源，geom=block）、正式活性 Measurement 0、原始提及 31（undetermined 15 / condition 8 / statistic 4 / result 4）、审核项 2。
- **0 条正式活性不是"过滤完数字"**：该论文正文活性全在图注与含混上下文中（survival rate 20%→80% 等），无可解析注册指标；31 条提及全部保留 text/role/reasons/证据，供 B06 的 issue 与 B12 的 UI 展示。旧版把这些硬造为 20 条 `unknown` Measurement。
- 证据全部字符偏移精度或诚实降级；无伪造第 1 页证据。

## 未完成与后续

- **B06**（表格指标串列/证据 ID 断链/层级表头）：反例已复现（`Peptide|MIC|MBC` → MBC=8/MBC=16），实现待做。
- **B07**（实体/assay 接线、审核修正闭环）、**B08**（快照导出、Range 后缀、只读源校验）、**B09–B12**（专利/评测/区域复解析/工作台）：未开始。
- 没有服务/gold 的延期项：混合文档真实 OCR 端到端（需扫描件样本）、第二通道真实二次解析、正式准确率分数——接口、算法与回归已就位。
