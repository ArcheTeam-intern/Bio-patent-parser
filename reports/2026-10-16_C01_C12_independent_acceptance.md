# C01–C12 独立验收与下一轮修复要求

日期：2026-10-16。核查范围：`git diff 2068150...6805846`，并检查当前发布制品。

原方案：`reports/2026-10-16_domain_review_display_comparison_plan.md`。

## 结论

**部分符合预期，尚未达到原方案的完整验收要求。**

确实交付了序列声明清点、部分 OCR 恢复、单位规则修正、图表逐页查询、审核分组及 MinerU 批量协议基础。执行报告也如实写明了 C08、C11 未实现及真实云实测缺失。

但 C03、C04、C06、C09 的“完成”标记过宽：存在可复现的数据丢失、领域语义错误、批量决定部分提交，以及问题页定位不完整。C10 的模拟协议测试通过，尚不能说明请求参数、真实工作流重试及几何映射已经正确。

本轮没有修改业务实现，没有向 MinerU 上传文件，没有写入实际任务的审核决定。测试使用既有测试与临时目录中的隔离复现；本文是验收结果及后续修复交接，不是已修复声明。

## 一、重新执行的验证

| 检查 | 本轮实际结果 |
| --- | --- |
| `.venv/Scripts/python.exe -m pytest -o addopts='' -m 'not live' -q` | **514 passed，4 skipped**；43.99 s；1 条 Starlette/httpx 弃用警告 |
| `npm --prefix web run test -- --run` | **28 passed，6 个测试文件** |
| `npm --prefix web run build` | **通过**，TypeScript 与 Vite 返回 0 |
| 批量审核隔离复现 | 后项失败时，前项已提交，不能整体回滚 |
| OCR 合并隔离复现 | 不同区域、相似但不相同的 OCR 残基被直接删除 |
| 页眉过滤隔离复现 | 三个不同编号的合法重复残基行均被删除 |
| MinerU MockTransport 隔离复现 | attempt 换目录后重复提交；同目录换源文件误复用；参数不贯通；固定页尺寸 |
| 真实发布制品 | 复核 EP 全文、EP 30–33 页、论文新任务的 inventory、relations、measurements |

**测试通过与功能验收通过不同。** 上述隔离复现覆盖的失败模式没有被当前测试套件锁定。

## 二、真实效果：找齐声明，不等于找齐残基或关系

### 2.1 EP 全文任务 `job-819fab3181d3`

读取已发布 `extract` 制品，而不是只读取执行报告：

| 清点状态 | 数量 |
| --- | ---: |
| 文档声明总数 | 82 |
| 观察到的声明编号 | 82 |
| 系统标为 complete | **8** |
| partial | **19** |
| conflict | **1** |
| missing | **54** |

这 8 条通过了当前长度/字母表等规则检查，但没有独立逐字符 gold，不能解释为“已验证正确 8 条”。

关键记录：SEQ 7 为 DNA，**90/364**；SEQ 8 为 protein，**97/121**；SEQ 9 为 DNA，**88/337**。这说明 OCR 有恢复进展，但尚未达到完整提取目标。

该任务在后续 C04 实现前发布，关系数为 0，不能要求它自动包含新算法结果。后续需要固定全链路版本重跑，避免用不同版本任务分别证明整条链路完成。

### 2.2 EP 关系任务 `job-f4e73151ce60`

该任务只覆盖 30–33 页：2 个实体、4 条关系。实际存在：

- SEQ 7：DNA，90/364，`heavy_chain`，`link_status=linked`；
- SEQ 8：protein，100/121，`heavy_chain`，`linked`；
- SEQ 9：DNA，111/337，`light_chain`，`linked`。

DNA 编码序列、蛋白可变域、完整链应区分角色；部分恢复也应明确表达。`linked` 不等于人工确认，当前记录的 validation 仍为 unchecked；但 UI 将 DNA 显示为重链/轻链的领域含义仍不正确。

### 2.3 论文新任务 `job-8205561af0cc`

- 2 条序列片段、255 条活性记录、3 个实体、**0 条关系**。
- 255 条活性的 `entity_id`、`sequence_ids`、`assay_id` **仍全部为空**。
- “缺少单位”原因已降为 0，分类药敏规则这一点有实际改进。
- 但“实体未关联”仍有 255 条，原方案的被测对象及序列—活性链路没有完成。

不能把这些抗生素药敏结果强配给两个肽片段；应建立非序列实体并区分背景药敏与目标生物分子的实验结果。

## 三、实现可靠性审查（Standards）

标准来源：仓库及父目录未发现 AGENTS、CONTRIBUTING 或 CODING_STANDARDS；以下采用原方案和现有服务明确的事务、证据约定，不把个人风格偏好作为违规。

### S01 [P1] 批量审核不是整体原子提交

**代码：** `src/bio_pdf/application/review_service.py:225–235`；单条 `apply_decision` 内自行 BEGIN/COMMIT。

`apply_batch` 先检查版本和 open 状态，然后逐项调用各自提交事务的 `apply_decision`。它没有在一个事务内覆盖全部验证和写入。

**本轮独立复现：** 用临时 PDF、临时 SQLite、真实 JobService/worker 建立两个 open 项；第一项 reject 带原因，第二项 reject 不带原因。

```text
OPEN_ITEMS 2
BATCH_ERROR WorkflowError: reject requires a reason
AFTER_STATUSES ['resolved', 'open']
DECISIONS 1
RESULT_REVISION 1
```

客户端得到失败，但第一项已经生效。即便预先补齐参数校验，预检查后发生的并发版本变动仍会产生同类问题。

**违反：** 方案 §3.3“任一冲突整体失败”、审核服务的原子事务约定。

**修复：** 抽出不自带事务的内部单条决定逻辑；批量入口在单个 `BEGIN IMMEDIATE` 内检查全部动作、reason、patch、证据、版本和 blockers，并写入全部版本、决定日志及状态。任一失败统一 rollback。重复 review_id/同记录重复操作应在写入前拒绝。

### S02 [P2] 全部任务视图将不同任务的审核组合并

**代码：** `web/src/features/reviews/ReviewQueuePage.tsx:147–152`。

前端使用 `g.label`（如“第 8 页·药敏矩阵”）作为 Map key，没有任务、基线身份。不同任务同页矩阵被合为一组，“选择分组”将跨任务选择；后端按任务统计的组数与前端实际呈现也会不一致。

**违反：** 方案 §3.3“任务及基线 → 表格/区域 → 子记录”。

**修复：** 后端返回稳定 group_id 和具体成员身份，至少绑定 job、baseline、来源表格/区域；前端使用该 key，不重新按显示文字分组。序列摘要映射也应按任务/基线/记录版本区分。

### S03 [P2] 发布制品损坏被图表接口伪装成尚未生成

**代码：** `src/bio_pdf/api/routes/results.py:240` 附近的 `except WorkflowError`。

隔离复现中，损坏发布指针后 ResultReader 抛 `RESULT_CORRUPT`，图表端点却返回空列表、total=0 和 `no normalized result yet`。

**违反：** 方案 §4.1 要求未生成、损坏、无图表分别展示。

**修复：** 只处理明确的“尚无结果”，让损坏保留 `RESULT_CORRUPT` 和适当错误状态；不得降级成空内容。

### S04 [P2] 问题页码仍从字符串猜，正常表格问题无法定位

**代码：** `src/bio_pdf/api/routes/results.py:530–546` 的 `_issue_page`。

当前从 detail 或 `:pN:` 等 ID 形状提取数字，没有查询真实来源对象。真实 Docling 表 ID 如 `7:t0`，结构重叠详情如 `TABLE_GRID_OVERLAP at r0c0…`，因此返回 page_number=None，第 7 页筛选会将其排除。

**违反：** 方案 §4.3 从真实表格/证据对象解析位置，不从不透明 ID 猜页号。

**修复：** 生成 issue 时携带真实 page/region/source 引用，兼容历史项时查表格、块、证据索引；无法解析才显示位置未知。多页来源应保留页列表。

**启发式提示：** 前后端各自实现审核分组造成规则分叉，属于可能的重复逻辑；已在 S02 报告其实际影响，不另计硬缺陷。

## 四、方案覆盖及领域正确性审查（Spec）

### P01 [P1] OCR 相似去重会删除不同内容和不同位置的残基

**代码：** `src/bio_pdf/workflow/handlers.py:851–859`。

对同一页全部 native 文本做子串判断或相似度 >=0.6 去重，不检查是否同一物理位置。该规则既会删除合法重复/相似残基，也会隐藏原生与 OCR 的字符冲突。

**本轮独立复现：** native 第 y=100 处为 `ACGT`，OCR 第 y=300 处为 `AGGT`：

```text
MERGED_TEXTS ['<210> 1…<400> 1', 'ACGT']
INVENTORY [('1', 'complete')]
SEQUENCES [('1', 'ACGT', True)]
```

不同位置的 OCR 行直接消失；同位置的两来源差异也会被如此消去。

**违反：** 方案 §2.3 保留来源、冲突双方和位置，不能自动择一冒充确认。

**修复：** OCR 保留完整 bbox、来源和置信信息；仅同一物理提及且规范化内容确实一致时合并；同区域不同内容生成 source conflict，不提高 complete；不同区域的同内容也保留各自位置。不要只调整 0.6 阈值。

### P02 [P1] 页眉过滤器会删除真实重复残基行

**代码：** `src/bio_pdf/extract/sequences/st25.py:511–523`、`:547`。

跨至少 3 页出现，且带数字或长度 <=12 的文本就当 running material 删除，没有位置/版面角色约束。残基行本来就常带位置计数，且不同 SEQ ID 可以具有相同片段。

**本轮独立复现：** 三页分别声明 SEQ 1/2/3，均为长度 2，合法残基行 `Ala Cys 2`：

```text
THREE_VALID_SEQUENCE_RECORD_COUNT 0
THREE_VALID_SEQUENCE_INVENTORY [('1', 'missing'), ('2', 'missing'), ('3', 'missing')]
```

**违反：** 方案 §2.2 同内容不同 SEQ ID 独立保留；§2.3 按几何、角色、重复识别页眉。

**修复：** running material 必须有页边区域或明确 header/footer 角色依据，重复只是辅助信号。序列状态内的合法残基不能单靠重复判成页眉。覆盖同内容不同编号、同序列不同位置的回归。

### P03 [P1] 关系角色、恢复状态及多实体关系处理错误

**代码：** `src/bio_pdf/extract/entities.py:250–261`、`:309–324`；`src/bio_pdf/extract/relations.py:117–130`。

- 看到 VH/VL 就选 `heavy_chain/light_chain`，未区分 DNA 编码序列、蛋白可变域和完整链。
- 只要 SequenceRecord 存在就标 linked，没有结合部分恢复、冲突及证据定位状态表达可信度。
- 原文 `#134 and 136` 虽建立两个实体，但 `links.append` 只取 `names[0]`，丢失 #136 的明确关系。

真实 EP 30–33 页任务中的 DNA 90/364、111/337 已产生上述链关系。本轮复查了实际发布制品。

**违反：** 方案 §2.1、§2.4、§2.5 的角色区分、状态及明确证据关联要求。

**修复：** 分离“引用已解析”“残基完整性”“来源可信度/人工确认”；DNA 表示编码序列，蛋白域与整链使用准确角色；每个明确命名主体都建立证据关系。补齐论文 TAT/RasGAP、GG、retro-inverso、W317A 和非序列抗生素实体，不能只用 EP 小窗口关系代替全部 C04 验收。

### P04 [P1] MinerU 远端身份不能跨真实 attempt 恢复，且未绑定源和配置

**代码：** `src/bio_pdf/parsers/mineru_adapter.py:781–811`；`src/bio_pdf/workflow/runner.py:427` 附近的 attempt 目录构造。

identity 保存到当前 attempt 的 output_dir，但工作流重试创建新 attempt 目录。现有单测只在同 output_dir 再调用，覆盖不到真实重试。

反方向，复用 output_dir 时仅检查 base_url/batch_id，不检查源 SHA 和配置；更换源文件及对应 document_id 后仍可把旧远端结果标为 `source_identity_verified=true`。

**本轮 MockTransport 复现，无外部网络请求：**

```text
DIFFERENT_ATTEMPT_POST_COUNT 2
CHANGED_SOURCE_POST_COUNT 2 OLD_IDENTITY_REUSED True VERIFIED True
```

**违反：** 方案 §5.3 恢复不重复创建、源身份一致；§6.3 实际执行和制品身份一致。

**修复：** 远端任务登记放在跨 attempt 的稳定存储，绑定完整源 SHA、页集合、引擎/model、有效参数及其指纹；记录申请、上传、轮询等阶段，恢复时核对一致性。使用真实 worker retry/resume 路径验收，不只直接调用 adapter 两次。

### P05 [P1] MinerU 参数与页几何仍不贯通

**代码：** `src/bio_pdf/parsers/mineru_adapter.py:797–800`、`:1105–1116`。

有效 parser_options、profile 与请求页没有传入 `request_upload_slots`，实际使用默认 language=en、table=true、OCR=true，没有 page_ranges。

**本轮复现：** 请求 `ocr_lang=ch`、`do_table_structure=false`、`layout_native`，上传 body 仍为 en、enable_table=true、is_ocr=true。

内容列表投影仍把页面固定为 612×792；传入 bbox `[0,0,1000,1000]` 原样落在这个页面上，未结合源 PDF 尺寸或坐标协议处理。

**违反：** 方案 §5.3 真正传参和 §5.4 真实页尺寸、旋转、映射。

**修复：** 按已核对的云协议映射有效参数和页范围；归一化接入真实 PDF 几何和明确的坐标单位/基准，并覆盖 A4、旋转、非连续页及未知几何。协议没有给出足够信息时明确降级，不猜坐标。

## 五、C01–C12 建议验收状态

| 项目 | 建议状态 | 原因 |
| --- | --- | --- |
| C01 基线冻结 | 已交付基础制品 | 已有样本/版本/配置与发布制品清单；本轮未重新生成冻结文件 |
| C02 声明清点 | 基础达标，非残基完整性达标 | 已真实清点 82，局部范围有说明；不能据此称序列找齐 |
| C03 序列识别 | **部分完成，存在 P1** | 仍有 54 missing；相似去重和页眉过滤会丢真实残基 |
| C04 领域关系 | **部分完成，存在 P1** | 角色/多主体错误；论文 255 条仍全部未关联 |
| C05 单位规则 | 针对本次目标基本达标 | 分类药敏缺单位原因已为 0，数值指标规则保留 |
| C06 审核 | **部分完成，存在 P1** | 批量非原子，跨任务混组；baseline 身份/替代迁移未实现 |
| C07 图表逐页 | 主要功能已交付，仍需完善 | 支持按页与计数；损坏仍被吞成空结果，缓存基线身份需补齐 |
| C08 原文阅读 | **未完成** | 阅读顺序、三视图仍未按方案实现 |
| C09 问题详情 | **部分完成** | 有卡片/筛选，但来源页码不可靠；处理动作和执行 loop 未接线 |
| C10 MinerU | **部分完成，存在 P1** | 有批量协议/分派基础；参数、重试身份、几何有问题，无真实云实测 |
| C11 对比功能 | **未完成** | 对比任务、API、页面均未实现 |
| C12 评测 | **部分完成** | 如实报告 not_evaluated 是正确口径；独立 gold 和效果指标仍没有 |

缺 Token 只能阻止真实云实测，不能成为 C11 任务编排、API、页面和合约验证都不实施的理由。人工 gold 需要人工确认，但标注工具、待标注清单、数据结构及接入也可以先完成。

## 六、交给执行模型的下一轮修复清单

只做以下明确任务，沿用原方案，避免另建无关框架。

| 编号 | 任务 | 必须通过的新增验收 |
| --- | --- | --- |
| D01 | 批量审核统一事务 | 后项缺原因/非法 patch/blocker/版本冲突/重复项时全部零写入；成功批次日志和版本完整 |
| D02 | OCR 按位置融合与冲突保留 | 同位置不同字符保留冲突；不同位置相似/相同残基都保留；真实 DNA 不再被阈值删除 |
| D03 | 页眉按几何/角色过滤 | 三个 SEQ 各自 `Ala Cys 2` 均恢复 AC，同时 EP 页眉仍被排除 |
| D04 | 关系角色、主体与论文链路 | DNA 为编码角色，partial/conflict 状态明确；#134/#136 双主体不丢；论文构建体/变体/非序列实体可追踪 |
| D05 | 审核稳定身份和分组 | 两任务同页不合组；跨 baseline 旧项 superseded、历史人工决定不丢；后端 group_id 统一使用 |
| D06 | MinerU 稳定身份及真实参数 | worker 跨 attempt 只创建一次远端任务；源/配置变化不复用旧任务；语言/表格/OCR/页范围匹配真实请求 |
| D07 | MinerU 页几何、C11 对比骨架 | A4/旋转/非连续页映射正确；对比 API+队列+UI 可运行，实验不污染正式审核队列 |
| D08 | 完成 C08 原文三视图 | 多栏不串行；原始块完整；阅读段落与来源对照可回到真实块 |
| D09 | 问题来源、错误分类和动作 | 表格 `7:t0` 的问题定位第7页；RESULT_CORRUPT不变空列表；处理动作有后端和日志 |
| D10 | 固定全链路版本重跑与 gold | EP 82项逐条报告；披露 complete/partial/missing/conflict；独立 gold 验证重点区域与关联；云端实测有真实制品 |

说明：8 complete/82 声明是当前任务的状态统计，不是必须固定追求的验收数字。修复删除误报后计数可变化；最终应基于真实原文逐项解释，不能靠调高预算或把 unknown 改 complete 来提升结果。

### 执行顺序与报告

先 D01–D04 修正事务和数据语义，再 D05–D09 完善身份、接入与界面，最后 D10 在统一版本上验收。对比功能可先用明确标记的 fixture/MockTransport 验证逻辑，真实效果报告必须使用真实云制品。

建议交付：

- 逐项修复状态、提交、验证命令和输出；
- 覆盖上述隔离复现的自动化回归；
- 新的 EP 全文与论文任务、有效参数及基线；
- C08/C09/C11 的真实浏览器证据；
- MinerU worker retry/resume 证据和真实云实测状态；
- gold 范围、未标注范围及仍未解决的问题。

## 七、审查摘要

- **Standards：4 类硬问题；最严重的是批量审核部分提交。**
- **Spec：5 类实质实现问题；序列内容保留、领域关系与 MinerU 身份/参数均需修正。**
- C08、C11 和独立 gold/真实双引擎效果仍未交付。当前可作为继续完善的基础，不能按原方案全部完成验收。
