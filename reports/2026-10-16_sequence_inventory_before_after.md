# 序列清点 before / after（2026-10-16）

> 范围、清点、漏提/误提、真实残基恢复与未解决项。所有数字来自真实运行产物（`data/post-m1-demo/jobs/*/stage_runs/*/extract.json`），不是方案核查数字的复述。

## 1. 样本与口径

- 样本：`pdf/patent_EP3027649B1_TNFa_IL17_bispecific_antibodies.pdf`（sha256 `ffb2e5bb7a63…`，195 页，B1 授权版）。
- 口径：声明数 = 文本层可见 `<210>` 数；`<160> 82` 为文档自述总数；恢复 = 残基实际进入 `SequenceRecord`；**EPB1 类**= 归一化序列包含页眉碎片（`EP 3 027 649 B1` 被当残基）。
- 任务：BEFORE=`job-10fbff9f5374`（C02 阶段：无页眉过滤、无区域 OCR）；AFTER=`job-819fab3181d3`（C03 终版：页眉过滤 + 标签分词 + DNA 行 + 区域 OCR 兜底，预算 10 页）。

## 2. 清点（82 个声明逐条追踪）

| | BEFORE | AFTER |
| --- | --- | --- |
| 声明清点 | 82 / 82（= `<160>`） | 82 / 82 |
| complete | 0 | **8** |
| partial | 39 | 19 |
| missing | 43 | 54 |
| conflict | 0 | 1 |

missing 上升是**诚实性的提升而非退化**：BEFORE 的 39 条"partial"全部是页眉污染（见下节），过滤后如实归入"区域未恢复"。

## 3. 误提（最关键的发现）

- BEFORE：**39 条有残基的声明 39/39 全部是 EPB1 型**——典型如 `SEQ 6: 4/9 EPB1`、`SEQ 47: 16/699 EPB1EPB1EPB1EPB1`。也就是说，修复前"已恢复 39 条"的叙事完全由页眉碎片构成，**没有一条是真实残基**。
- 根因链：页眉行 `EP 3 027 649 B1` 无标点、词长 ≤8 → 通过残基行启发式 → 进入残基状态；同时区号列（5/10/…/55）与页眉跨页重复。
- AFTER：**EPB1 型 = 0**。过滤为"跨页重复（≥3 页）且含数字或极短"的行，**先排除 ST.25 标签行**（`<212> DNA`/`<211> 364` 在真实专利中跨页重复，误过滤会吞掉全部声明元数据——修复过程中被回归测试捕获并修正）。

## 4. 真实残基恢复（AFTER）

| SEQ | 长度/声明 | 类型 | 内容（前 40 字符） | 说明 |
| --- | --- | --- | --- | --- |
| 1 | 5/5 ✅complete | protein | `SYGVH` | |
| 2 | 16/16 | protein | `????????????????` | OCR 区域字符不可辨，如实计 unresolved |
| 3 | 15/13 | protein | `ARRRRRRRRRRRRRYR` | 超长冲突（1 条 conflict 来源） |
| 5 | 7/7 | protein | `KVSNRFS` | VL CDR 2 |
| 6 | 9/9 | protein | `SQTTHAPFT` | |
| **7** | **90/364** | **DNA** | `CCCCAGGCAAGGGCCTGGAATGGGTGTCCGTGATCTGGTC…` | **修复前 0 字符；区域渲染 OCR 恢复 90** |
| 8 | 97/121 | protein | `EVQLVESGGGLVQPGG…` | VH 域；修复前是 `EPB1` |
| 9 | 88/337 | DNA | `AGCCAGCAGCCGCAGGCAGACGGAGCGGCAAACGGAAACT…` | DNA 行启发式解锁 |
| 12 | 100/121 | protein | `EVQLVESGGGLVQPGG…` | 与 SEQ 8 为不同构建体，独立成行 |
| 23 | 30/30 | protein | `EVQLVESGGGLVQPGGSLRLSCAASGFSLD` | 无未解析 token |

- 有残基的声明：AFTER 28 条，其中 17 条无未解析 token（BEFORE：39 条全部为污染）。
- 与方案核查对照：方案记载"SEQ ID 7 声明 364 nt、文本层无残基、渲染可见"——AFTER 中 SEQ 7 以 90/364 partial 落地：**声明可见、区域已 OCR、剩余字符未全部辨识**，逐字符正确性仍待人工核对（不宣称）。

## 5. 范围诚实（40 页局部任务）

- 任务：`job-0b05fe714374`（pages 1–40，native）。
- 结果：observed 32 / 全文 82 / `declared_total_unseen` 50 / `covers_full_document=false`；页域 `observed_pages=[30..40]`。
- 与方案核查的"前 40 页可见 32 个清单起始编号"一致；**清单页明确标注覆盖范围，不冒充全文**。

## 6. 误提负例（修复后仍在库中的专项检查）

- `EPB1`、`EPB1EPB1`：0（全文 195 页）。
- `MMP`/`CDR` 类短字符串按氨基酸字母表误判：AFTER 未产生独立序列记录（短 token 需残基状态上下文才可能被收入；当前未发现独立误提）。
- 页眉/页脚/行号：不进入残基（跨页重复过滤）。

## 7. 未解决项（如实）

1. **54 条声明 missing**：区域 OCR 预算（`reparse_max_regions`=10 页）之外；提高预算或 OCR 升档（PP-OCRv6 medium，权重可下）可扩。
2. **OCR 质量**：小型等宽字体的部分行 OCR 不可辨（如 SEQ 2 全 `?`）——升档/放大渲染可改善；当前如实计 unresolved。
3. **逐字符正确性**：恢复出的 90/97/100 字符序列尚未经人工逐字核对；无 gold，不宣称准确率。
4. **1 条冲突**（SEQ 3 15>13）：按 observed>declared 记 conflict，待人工核对原文。
5. **C04 关系**：EP 侧 #134/#136 → 序列的 note 关系已建立（真实证据 span）；本报告的 82 条声明与构建体的**全量**配对（82×关系矩阵）未做——仅覆盖带 `<223>` 注释的声明。
