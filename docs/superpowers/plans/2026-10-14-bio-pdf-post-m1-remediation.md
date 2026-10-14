# M1 执行后的补充修复与验收方案

> **For agentic workers:** 使用 executing-plans 按B01–B12执行并跟踪checkbox；仅在用户授权并行时使用subagent-driven-development。本文是新增差量方案，上一份17任务方案保留为设计背景。

**Goal:** 修复M0/M1接线和正确性缺口，完成真实字段证据与领域结果审核，再推进复杂表格、专利、评测和区域复解析。

**Architecture:** 保留现有确定性Python工作流、SQLite、stage-run制品、FastAPI和React。增加数据库条件提交、固定发布基线、统一证据工厂与真实逐页执行；领域规则按具体mention/cell而非整句/整表作判断。

**Tech Stack:** 当前Python3.12、Pydantic2、pypdfium2、Docling、MinerU HTTP适配器、React/TypeScript/PDF.js。无需新增工作流框架。

---

## 0. 接手说明与完成标准

- 根目录 `E:/GoLandCode/Bio-patent-parser`；起点HEAD `cab949a`，先读 `reports/2026-10-14_post_m1_review.md`。
- 原两份执行报告保留；新增correction说明，不悄悄改旧报告让其看似正确。
- 数据写到 `data/post-m1-demo`；保留旧 `data/demo` 与 `data/optimization-demo`，新规则用新job。
- 基线测试351 passed、4 skipped；不能把测试数量或候选减少作为准确率指标。
- 每个任务必须按“反例失败→实现→针对性回归→真实制品/API或页面核验→报告/提交”完成。
- 接手时记录实际HEAD；已有修复则核验后跳过，不能重复覆盖用户改动。
- B01/B02/B03/B04/B05/B06/B08是阻断可靠验收的优先修复；B07/B09/B10完善领域闭环；B11真实增强；B12统一交付。
- 没有服务/gold只能延期对应的真实引擎运行/正式分数，不能延期可离线完成的契约、算法、UI和回归。

## 1. B01：先接通前端证据导航，修复opaque ID回归

**修改：** `web/src/api/client.ts`、`features/workbench/WorkbenchPage.tsx`、`features/pdf/PdfViewer.tsx`。
**新增：** `features/evidence/useEvidence.ts`、`EvidenceNavigator.tsx`及相应高风险导航测试。
**后端配合：** `src/bio_pdf/api/contracts.py`、`api/routes/results.py`。

- [ ] 为getEvidence定义EvidenceData：evidence_id、baseline_id、page_number、bbox、fragments、raw_text、source_locator、精度字段。删除Record<string,unknown>类型与pageOf(eid)。
- [ ] 点击record先选定记录，再加载其evidence；从API返回定位page/bbox，不猜ID、不默认为第一页。
- [ ] 支持value/unit/metric/entity/target等字段的多证据切换；missing evidence要显示真实错误。
- [ ] 加请求取消/generation，快点A再点B时A慢响应不能覆盖B；取消旧PDF renderTask，页渲染完成前不显示旧bbox。
- [ ] page/evidence/record/tab/zoom由URL恢复；加载源PDF前显示加载态。
- [ ] precision为block时显示“段落区域+文本偏移”；没有bbox但有page显示“页级证据”，不标精确字符高亮。

**定位核心：**

```typescript
const span = await getEvidence(jobId, evidenceId);
const fragment = (span.fragments ?? [])[fragmentIndex];
setTarget({
  page: fragment?.page_number ?? span.page_number,
  bbox: fragment?.bbox ?? span.bbox,
});
```

**验收：** 当前job的ev-hash点开能到第9页；两段短序列允许先显示段落区域，精度标签诚实；快速切换、缩放、DPR=2、URL刷新不误定位。新增Vitest/Testing Library只覆盖导航/编辑关键行为，PDF绘制用真实浏览器检查。

## 2. B02：源ID、证据精度与融合去重

**修改：** `src/bio_pdf/pdf/native.py`、`parsers/fusion.py`、`extract/evidence.py`、`contracts/common.py`、`document.py`、`workflow/handlers.py`。
**新增：** `tests/unit/test_evidence_identity.py`、扩展 `test_parser_fusion.py`、真实片段精简夹具。

- [ ] native block ID包含parser namespace、page和rect locator，不能跨页重复native:rN；归一化/融合结果均校验文档内ID唯一。
- [ ] evidence ID包含page、source artifact/hash、source locator、offset；source_parser从adapter元数据取得，不用block ID字符串猜native/layout。fragment.source_artifact_id必须可加载。
- [ ] 将文本精度与几何精度拆开：text_precision=offset/block/none，geometry_precision=character/cell/line/block/page/none。旧location_precision保留兼容，但无字符bbox不能报character。
- [ ] `span_from_block_match`保存确切raw子串/offset，bbox若来自父块仅标block/line；精确range rect只在字符映射通过Unicode/合字/旋转验证后启用。
- [ ] 不创建没有raw文本/来源的假证据来让EVIDENCE_EXISTS通过；新格式缺证据应fail或unknown。legacy迁移不能默认为page1。
- [ ] 融合从“一段配一个行”改为一段匹配有序native span集合。按空间和文本覆盖计算consumed ranges，不是只记used block ID；已被layout段落覆盖的native行仅作为来源，不再追加到主抽取流。
- [ ] 同一源位置的双通道候选合并evidence；不同页/实验/实体的相同序列不得按字符串全局合并。
- [ ] source conflict保存完整双方raw与源refs，生成关联block/record的SOURCE_CONFLICT check；当前只记录前120字和normalized.errors不能证明已阻断。
- [ ] semantic_role不能因补入native text重新变unknown从而绕过caption/author判别；继承已对齐region角色并保留角色来源。

**具体夹具：** 一个layout段落含两短序列，对应native多行；只输出两个序列candidate，each有两路来源。两页都含同一残基串、相同rect index/offset，应得到不同span ID。I/1冲突在第200字符仍保留完整差异并阻断接受。

**当前论文验收：** 去掉名称片段RASGAP后，两段明确展示的序列按同源位置合并，不能再把第9页同一位置称“摘要+正文”。命中范围不能伸入`RASGAP317-326`实体名；这不是添加RASGAP单词黑名单，明确标注的合法残基串RASGAP仍可以提取。

## 3. B03：真正的fencing、manifest校验与发布事务

**修改：** `workflow/queue_worker.py`、`runner.py`、`stages.py`、`handlers.py`、`storage/repository.py`、`result_reader.py`、`application/job_service.py`。
**测试：** `tests/api/test_concurrent_transactions.py`、`tests/unit/test_stage_manifest.py`、`tests/integration/test_queue_worker.py`。

- [ ] 用LeaseToken(job_id,worker_id,generation)取代只看keeper.lost的Callable；StageContext携带token，所有共享DB写入由repository条件事务执行。
- [ ] heartbeat检查generation和lease_until>=now；lease_is_valid检查到期时间；失效不能续活。heartbeat异常设置lost，stop后join线程再关连接。
- [ ] 失去租约取消正在运行的解析子进程；阶段返回成功/失败、review写入、status更新、发布均必须重新在提交事务验证token，不能只在阶段开始检查。
- [ ] 验证阶段先生成review plan artifact，最终发布事务才登记review items与head baseline；旧worker不可通过handler另开Repository写入共享review队列。
- [ ] claim记录execution_mode，expired reclaim自动resume；首次run保持fresh。worker_id使用UUID或PID+随机值，不只hostname+秒。
- [ ] `_cache_is_intact`先核验stage manifest字节与登记hash，然后核验每个输出；校验manifest的job/stage/cache_key/inputs语义，不能只检查输出hash。
- [ ] ResultReader只在明确旧格式且没有published baseline时回退legacy；manifest存在但损坏、少entry、未知schema一律RESULT_CORRUPT，不回退工作文件。
- [ ] 新发布manifest是不可变文件，DB保存published_manifest_ref/baseline_id；在验证token的同一事务更新发布指针与状态。FS当前指针只作展示镜像，读取以DB指向的不可变manifest为准。
- [ ] 发布失败不能先把job标succeeded；崩溃发生在“写不可变文件但未提交DB”时，读者仍读旧基线。关键paths严格限制在当前artifact root与job作用域。
- [ ] 跨任务相同内容的occurrence隔离继续保留；旧review/head引用固定baseline，规则升级建立新job。

**关键事务形态：**

```sql
BEGIN IMMEDIATE;
SELECT generation FROM worker_leases
WHERE job_id=? AND worker_id=? AND generation=? AND lease_until>=?;
-- 查不到立即ROLLBACK，返回LEASE_LOST
-- 持有同一写事务期间提交stage/review/published ref/status
COMMIT;
```

**必须新增的反例：** generation1进入ValidateStage后暂停，generation2回收并发布，generation1返回成功/抛异常均不能改status/review/published ref；在最后一次guard与发布之间夺租；篡改manifest及其中输出hash仍无法命中缓存；损坏published manifest不读legacy。使用barrier和注入时钟，不能靠偶然sleep。

## 4. B04：逐页执行计划与effective配置真正生效

**修改：** `workflow/parse_plan.py`、`handlers.py`、`parsers/base.py`、`docling_adapter.py`、`docling_child.py`、`application/job_service.py`、`api/routes/jobs.py`、`api/app.py`、`config.py`。
**新增：** `parsers/registry.py`、`tests/integration/test_mixed_parse_execution.py`。

- [ ] ParsePlan按(engine,profile,language,options)分组，并进一步拆连续页区间；分别执行layout_native/layout_ocr，再按原始页号合并。出现部分OCR页时不能用layout_native覆盖全篇。
- [ ] auto/native无法提供扫描OCR时明确unsupported/coverage issue；force与disabled必须符合真实调用，而不仅ParsePlan输出正确。
- [ ] ParseRequest增加typed parser_options、timeout、cancel token和具体provider identity；Docling converter使用配置的tableformer_mode、do_table_structure、ocr_lang，不继续固定eng/默认表格选项。
- [ ] timeout从持久化effective config进入执行；环境变量若覆盖需在job创建时解析进effective config，不能在复跑时偷偷换值。
- [ ] docling_sdk、docling_serve、mineru_http分派到对应adapter。未实现/未配置的parser入队前拒绝，不同名字不能都调用SDK。
- [ ] API/CLI目标参数正确进入cfg：当前create_job的targets未覆盖WorkflowConfig；override区分未传与显式auto/default，model_copy更新后重新validate。
- [ ] provider版本用importlib.metadata或服务返回版本，记录真正执行版本，不能Docling __version__缺失就一直unknown。
- [ ] Docling用可管理的Popen/进程组保存PID，timeout或失租时清理进程树；TimeoutExpired通常不携带PID，不能以getattr(exc,'pid')声称子孙进程已清理。
- [ ] 每页manifest同时记录planned与actual engine/profile/OCR状态、produced结构/文本来源、blank和unsupported原因。配置变化需证明实际request/converter参数变化。

**验收：** 3页fixture：数字页1、扫描页2、空白页3；auto必须真正对页2启用OCR，disabled无OCR且暴露覆盖缺口，force所有非blank页OCR。{2,4}不输出3。sequences-only不产活性。配置中文OCR不能仍跑eng；engine不可用不静默替换。

真实扫描fixture必须标来源；数字PDF渲染只用于合成回归。轻量adapter spy测试证明分派与参数，真实引擎运行证明实际效果，报告中区分两者。

## 5. B05：按每个mention判断活性，消除误删与unknown堆积

**修改：** `extract/activities/mentions.py`、`prose.py`、`values.py`、`metrics.py`、`contracts/extraction.py`、`workflow/handlers.py`。
**新增：** `tests/unit/test_mention_role_scope.py`；扩展 `test_activity_prose.py`。

- [ ] classifier输入包含mention span、局部clause、section/semantic_role、metric候选和来源，不再按全句固定优先级一刀切。
- [ ] Figure/Table编号是来源定位线索，不是整句metadata否决词；mean±SD/SEM修饰结果误差，不能排除整个测量。只有该mention本身是CI/n/统计限定词才归statistic。
- [ ] 一句中dose与response分别标记：treated with20µM是condition，reducing viability to5%是result。methods剂量/结果百分比分开取证。
- [ ] `less than/at least`先是值比较表达；仅有claims section、权利要求编号或明确主张语境才标claim_threshold。论文实验上界保持结果身份和operator，不因比较词变专利claim。
- [ ] metric解析采用registered指标+局部关系，支持`MIC of peptide A was <10µM`以及规范percent viability；entity/assay不明确留需审核字段，不把指标变unknown。
- [ ] role=undetermined或metric未支持时只生成typed RawMention+表/句级问题；不生成正式unknown Measurement。原始数字与证据仍可浏览/复解析，不以删除困难数据改善指标。
- [ ] 每个RawMention实际满足mention_id/text/role/evidence_ids/reason_codes，不能当前任意dict(text,page,reasons)冒充契约。创建工厂生成其span，serializing时严格校验。
- [ ] ActivityValue执行finite、branch字段互斥、range顺序、非负误差校验；科学记数不支持时保留完整表达，不能截尾。原始比较语句可规范operator，记录词→符号变换来源。
- [ ] 两段真实短序列保留retro-inverso/D-amino-acids上下文与原文引用；不直接推导完整化学肽串，名称候选用结构边界修复而非逐词黑名单。

**已复现的反例必须改为：**

| 输入 | 预期 |
|---|---|
| Figure 1 shows an IC50 of 5 nM. | IC50=5nM，来源Figure1，不因figure丢失 |
| Compound A had an IC50 of 1.2 ± 0.3 nM (mean ± SD). | 一条plus_minus，误差说明保留 |
| Cells were treated with 20 µM peptide A, reducing viability to 5%. | 20µM条件mention+viability=5%结果；两个不同span |
| The MIC of peptide A was less than 10 µM.（results） | MIC，operator=<，number=10，非claim |
| Claim 1: peptide A has MIC less than 10 µM. | claim_threshold，不称实测 |
| 95% confidence interval / unknown的20µM | statistic/undetermined mention，不造bioactivity |

**真实样例：** 原20条unknown逐项给出去向：识别为具体metric、归condition/statistic、保留undetermined。新报告提供对应source span，不能只报告“20→0”。

## 6. B06：修复表格指标串列和证据ID断链，再补层级表头

**修改：** `extract/activities/tables.py`、`metrics.py`、`extract/evidence.py`、`workflow/handlers.py`、`validation/evidence.py`。
**新增：** `extract/activities/table_classifier.py`、`header_graph.py`、`tests/integration/test_table_evidence_flow.py`及MIC/MBC、药敏、inventory/scoring夹具。

- [ ] 先创建每个cell的EvidenceSpan，取其真实span.evidence_id组成`cell_id→evidence_id`映射，再调用extract_table_measurements；删除`:cell:`手工构造和缺证据时重新hash不同ID的兜底。
- [ ] 一个表分类为activity_numeric只表明类型；每个data cell从自己header_path取metric。禁止classify_table_metric取sorted第一个指标后传metric覆盖全表。
- [ ] 所有field_evidence ID必须存在且来自对应header/value/row/footnote；record.evidence_ids包含它们的union。统一字段键为value/metric/unit_raw/entity_raw/target_raw/assay_id，避免unit/unknown entity_id混用。
- [ ] 支持header_role与rowspan/colspan的层级路径、父级靶标/子级指标、脚注单位继承；结构重叠/冲突生成checks，不默默忽略。
- [ ] registry保留指标差异，MIC50不能无条件折为MIC；未知别名保留metric_raw待核验。词组别名要真正匹配，当前逐token循环匹配不到s/r/i等词组。
- [ ] 支持数值矩阵/药敏分类/序列列/条件表/inventory/scoring；第7/9页可明确为非目标类别，页面仍展示结构。未分类表不每格造Measurement。
- [ ] S/R/I仅药敏上下文解释，entity来自drug列头、target来自strain行；不猜I的标准含义，不反推MIC。numeric/分类metric分别校验unit required/not_applicable。
- [ ] 建ParseIssue模型与issues表，scope为table/region/mention，含issue_id/baseline_id/source IDs/reason/action/state。TABLE_UNCLASSIFIED/MISSING_TABLE不是普通字符串诊断；可在UI打开、请求复解析或标为非目标，决定可审计。
- [ ] ValidateStage发布review/issue plan，B03事务统一登记；API和review enum增加issue类型或独立issues端点，不能把非sequence/measurement对象交给只会读两种记录的旧ReviewService。

**第一验收（无需真实引擎）：**

```text
Peptide | MIC (uM) | MBC (uM)
A       | 8        | 16
```

必须为MIC=8与MBC=16；两个value及header证据全部可通过API加载；当前内存复现MBC=8/MBC=16必须先出现失败测试。

**第二验收：** 合并头E.coli/S.aureus各MIC/MBC共4条，target来自父头；S/R/I矩阵4条分类且对象不转置；inventory/scoring不产伪活性。完整ExtractStage→ValidateStage→API链路没有missing field evidence。

**延期规则：** T09不需要等真实页8检出才实现，先用明确结构夹具；页8端到端与B11并行推进，不形成分类器↔复解析循环依赖。

## 7. B07：实体/assay接线与审核修正闭环

**修改：** `extract/relations.py`、`extract/activities/assays.py`、`workflow/handlers.py`、`contracts/records.py`、`validation/activities.py`、`sequences.py`、`acceptance.py`、`application/review_service.py`、`api/contracts.py`、`web/src/features/reviews/ReviewPanel.tsx`。
**新增：** `extract/entities.py`、`extract/assay_linker.py`、两个typed PatchFields组件及关键编辑测试。

- [ ] 从明确实体名/表头提取entities；entity_id引用已存在同文档Entity，entity_raw保留名称。正式链接必须有证据，不能只凭非空字符串检查通过。
- [ ] 提取methods/caption/example的AssayRecord和condition证据；用explicit figure/table/example关联测量。多个assay可能时保留候选，不将全篇共用一条assay。
- [ ] 删除主流程link_explicit_relations([],...)空接线；relations两端和evidence可解析；抗体链配对仍需要明确来源，不按近邻组合。
- [ ] registry检查metric/value/unit/entity/target/assay及字段证据。unknown metric不允许按现有confirm直接成为accepted活性。
- [ ] 区分“完整显示的片段”与“完整蛋白”：显式短片段可以审核接受，sequence_scope仍fragment；不能所有inline都is_explicit_sequence=false/is_complete=false导致永远无法确认。未知残基或来源冲突仍阻断。
- [ ] 支持sequence位置修正和注释；raw_sequence不改，新version存规范串、残基diff、actor/reason与selected evidence。OCR修正无文本支持时新增human_transcription+源图证据，不能texts为空就接受任意raw。
- [ ] measurement表单含metric/entity/target/assay/unit/operator/scalar/range/uncertainty/qualifier；所有字段参与dirty检测。raw=">128"却number="12.8"或operator="="必须拒绝。
- [ ] selected evidence验证baseline/文档/字段范围，并写入新record；unit_normalized由服务端计算。failed check先于verified_human/reference接受逻辑。
- [ ] records API返回明确record_revision/allowed_actions/blockers；前端冲突后禁用提交、重新加载并显示差异，人工再次确认后才使用新revision，删除Math.max自动提高版本号逻辑。
- [ ] 修复已解决review_id无法继续修改的流程：需要再修订时创建绑定当前record revision的新审核项，不能复用resolved项。

**验收：** 两窗口一成功一409，无旧draft覆盖；更改number/operator实际生成patch；换记录重置编辑值；原源不被覆盖；sequence片段可按其真实身份接受；未解析/跨文档证据不能接受。

## 8. B08：固定快照导出、Range与只读源校验

**修改：** `application/export_service.py`、`api/routes/exports.py`、`results.py`、`documents.py`、`storage/repository.py`、`web/src/features/workbench/ExportDialog.tsx`。
**新增：** `application/result_snapshot.py`、`tests/api/test_document_ranges.py`、`tests/application/test_export_snapshot_race.py`。

- [ ] 在export POST捕获完整固定快照：同一read transaction读取DB published baseline ref、result_revision、heads和不可变versions。写不可变snapshot并登记snapshot_ref/hash；后台只build_from_snapshot，不能重新snapshot(current)。
- [ ] records API复用SnapshotService，revision与内容一致；不可变versions虽可稍后按固定key加载，但必须pin baseline与head集合，不读另一个current manifest。
- [ ] 导出路径含export_id（及format/filter），不能同revision不同accepted_only复用同一文件；counts按筛选后计算，另外给source/excluded counts。
- [ ] 默认accepted_only=true；诊断导出显式标候选。JSON包含entities/assays/relations/evidence/conditions；FASTA注明fragment与不能表达的修饰/D/L/retro-inverso信息。
- [ ] upload分块写临时文件并hash，避免chunks+join保留全量两份；只清理本次temp，不删除已有source/sample。
- [ ] Range用seek+有限stream，修suffix range。读取元数据/样例时校验失败仅返回错误，不能因GET删除源文件。
- [ ] cli直接导出的基线产物与人工审核后的快照区别明确；下载路径显示baseline/revision/filter，避免拿baseline result.json当已审核结果。

**强制竞态测试：** POST固定r1→barrier暂停后台→审核r2→执行后台，metadata、snapshot、文件manifest全r1，当前records全r2。再测试同revision两个不同filter导出不冲突。

**Range夹具：** 1000字节文件，bytes=-100应返回900..999，bytes=100-199返回100字节，bytes=950-返回50字节，bytes=1000-为416。

## 9. B09：ST.25纯续页、长核酸行和真实专利页发现

**修改：** `extract/sequences/st25.py`、`references.py`、`variants.py`、`workflow/handlers.py`。
**新增：** `evaluation/page_discovery.py`、ST25跨页/长DNA/页眉/编号/未知残基夹具、`tests/integration/test_patent_listing_flow.py`。

- [ ] 解析器在每条残基行进入状态机时存page/block/raw offset，不是事后仅在含<210>/<400>的block记seen_pages。
- [ ] 每个片段证据只存对应页/行真实raw；取消给每页都复制record.raw_sequence[:400]。第2页没有tag也必须有证据。
- [ ] molecule-aware continuation支持60字符连续DNA/RNA，避免现有token>8规则判prose。protein按三字母或已声明one-letter表示读取。
- [ ] 页眉/页脚不结束序列，未知字符占位保留；数字只在确认position label范围删除，不能删除OCR的0/1凑长度。
- [ ] 零残基listing产生unresolved issue而非NonEmptyStr异常导致全job失败；seq编号/declared length/position order不一致留冲突。
- [ ] handler只排除已消费listing区域，不按整页含tag就关闭inline提取。SEQ ID引用span与其实际页绑定，禁止现在unresolved ref强写page1且生成不同ID。
- [ ] 相同SEQ ID的不同listing source有冲突时不任取一个；features/variant坐标定义保留。
- [ ] page_discovery读取PDF签名与inspection后扫描可提取文本，生成listing/examples/claims/table候选页；文本低覆盖页列为需渲染/OCR，不推断“没有序列”。
- [ ] 可用本地样本：WO2017060493A1（167页）、US20240026005A1（142页）、WO2021207200A2（99页）。本轮仅查首3/末12页未发现ST25文本标签，执行者继续发现并渲染核验；不能编造其清单页号。
- [ ] HTML伪PDF与probe排除，signature通过后仍需检查页可读性。人选真实清单/实验/权利要求页进入开发与留出集。

**最小确定性fixture：** page1:`<210>1/<211>4/<212>PRT/<400>1/Ala Gly`；page2:`Ser Val`。输出AGSV，证据p1=Ala Gly、p2=Ser Val；无tag续页必须出现。不依赖外部OCR就可实现并验证该算法。

## 10. B10：先完成评测框架，人工gold只阻塞正式分数

**修改：** `evaluation/annotation.py`、`matching.py`、`metrics.py`、`report.py`、`cli.py`。
**新增：** `evaluation/corpus.py`、`annotations/v2/manifest.json`、`tests/unit/test_inline_matching.py`。

- [ ] 添加CorpusScope：document/pages/regions、穷尽标注状态、human verification、来源hash。候选只在verified且完整标注scope内计FP/FN；范围外是unannotated coverage。
- [ ] 无SEQ ID的inline按来源位置/实体/片段建立匹配边，完成一对一配对，再比较残基；所有正式candidate包含误提名字都参与统计，不能当前seq_id_no=None直接skip。
- [ ] listing同号错误内容记exact FP+FN各1，detected独立统计；重复candidate只一个TP，其他FP。
- [ ] measurement按完整entity/target/metric/value/operator/unit/assay/conditions tuple，entity opaque ID先解析名称/身份；wrong_value为诊断子集，不从FN/FP中扣掉。
- [ ] gold尽量独立结构化人工录入，不完全使用被评测parser解析raw作为标准；未知单位双方normalize=None不能相等即正确。
- [ ] 输出candidate precision/recall、序列exact/CER、field错误、table/evidence coverage、unknown与审核负担、accepted错误率；只有人工verified annotation计正式分数。
- [ ] CLI `evaluate --gold ... --records ... --out ...` 可在无gold运行并输出not_evaluated+coverage/diagnostics；合成回归gold仅用于测试，不写成真实人工标注。
- [ ] draft标注可由工具建议，但生产manifest的human_verified/status不能由执行模型批量设true。
- [ ] dev/holdout按专利family/论文补充来源分组；真实扫描与数字页分别报告；图中没有明确数字的曲线不能像素估计后计精确值gold。

**回归：** 已确认负例scope的0gold+5false candidates=>FP5；未标注scope=>not_evaluated；2gold+0candidate=>FN2；同源重复=>额外FP；正确数字错对象=>FP/FN；错误序列同编号=>exact FP/FN。不需要人工数据才能写这些测试。

## 11. B11：有界区域复解析与页8真实药敏表

**前置：** B02定位/source映射、B04真实adapter分派、B06领域表格提取已可用。B10正式gold不是前置。
**新增：** `review/reparse_plan.py`、`parsers/region_adapter.py`、`contracts/reparse.py`。
**修改：** `pdf/render.py`、`review/compare.py`、`parsers/mineru_adapter.py`、`workflow/handlers.py`、issues API/UI。

- [ ] 以MISSING_TABLE/GRID_CONFLICT/SOURCE_CONFLICT/SEQ_LENGTH_CONFLICT生成RegionRequest，含原始page、bbox、transform、engine/profile/trigger及预算。
- [ ] budget沿用每job10 region、每region2 attempt、单次120秒可配置；失败、耗尽、不可用均有结果，不无限重试。
- [ ] 原页生成局部裁图或引擎支持时原页指定region，保存EvidenceImage与transform_to_page；旋转也进入变换链，不将局部page1当原文page1。
- [ ] 页8按真实结构引擎执行：先验证可用本地第二通道；Docling准确结构模式/不同cell matching参数的区域尝试可作替代实验，但同系列两次不是独立验证。没有环境时先完成接口/预算/映射，明确真实parse尚未跑。
- [ ] 实际识别到Table2才进入B06药敏矩阵；记录drug×strain×S/R/I与各字段证据。不得手写表结构作为引擎产物，也不能caption“TABLE2”出现就称表已提取。
- [ ] 原/新结果并列，冲突不能投票自动修字符。两路一致记录agreement；不等于verified_reference。
- [ ] 修compare.py长度不同索引越界与插入删除错位，使用序列对齐显示真实edit位置；保留完整串。
- [ ] 可选VLM/LLM只提供已有span/header mapping/role建议；没有来源的序列或数值不允许接受。字符更改仍由源图/正式listing/人工核验。

**验收：** 页8真实检出则能经API打开cell证据并导出分类结果；未检出保留MISSING_TABLE issue与attempt trace。ROI逆映射含旋转/缩放正确，预算可证明有限，差1字符或长度不等的diff可审核。

## 12. B12：工作台补全、全链路报告与验收状态

**修改：** `web/src/features/workbench/WorkbenchPage.tsx`、`features/compare/ComparePage.tsx`、`features/reviews/ReviewPanel.tsx`。
**新增：** `features/tables/TableView.tsx`、`features/text/TextView.tsx`、`features/issues/IssuePanel.tsx`。
**交付：** `reports/post_m1/<commit>/sample_before_after.md`、`execution_matrix.md`、`artifact_inventory.json`及定位截图。

- [ ] 表格tab显示真实tables与合并头/分类/issue；原文tab可切primary/native/OCR并展示source冲突。不能继续显示占位句却称功能完成。
- [ ] 区分正式Measurement、condition、statistic、undetermined mention；显示对象/方法/字段缺失原因与复解析入口。
- [ ] issues可选择重解析/非目标/保留未解决等受控动作；audit与baseline绑定。没有issues项不得把diagnostic称人工审核项。
- [ ] compare页面同source SHA/page set/domain rules版本才比较；无gold展示差异，有gold展示对应评测版本。373–375px下用PDF/结果/审核切换，宽屏保留三栏。
- [ ] 实际运行新论文job以及至少一个确定性跨页专利fixture，再选择真实专利页；真实OCR/第二通道环境不可用只标未验证，其他任务继续完成。
- [ ] 从不可变snapshot自动生成candidate/mention/issue/evidence计数，附artifact hash和commit。不得手抄数量、将同源重复说成不同页、把offset说成字符bbox。
- [ ] execution_matrix每任务记录code_done、unit_passed、integration_passed、real_sample_checked、UI_checked、environment_blocked、commit；只有任务所需层全部满足才complete。覆盖不足写partial，未运行写not_run。
- [ ] 旧报告修正附录列47/70、同页重复、precision、快照和表级review项等差异，注明取证artifact hash，避免历史结论继续误导执行者。

**当前论文的整链路验收清单：**

1. 作者/药物/日期不误提；RASGAP名称不当残基；两真实片段合并同源重复且仍标fragment。
2. 20条unknown都有可审查去向，condition/result/statistic不互相混淆；真实结果不会因Figure/SD字样被丢弃。
3. 前端点击新ev-hash能定位，字段精度如实显示，刷新和快速切换正确。
4. MIC/MBC结构夹具每列指标正确、所有field evidence可在API打开；页8真实检出与否如实记录。
5. 人工修正和冲突保护实际操作可用；新export内容固定revision，state/manifest/counts一致。
6. 混合文档按页实际OCR；失租worker在成功/失败路径均不能写共享结果；损坏manifest不能回退绕过校验。
7. 评测无gold显示not_evaluated，候选噪声、source coverage和未解决issues照常报告。

## 13. 执行顺序与明确依赖

第一轮B01→B02→B03→B04，修前端回归/来源/任务可靠性；第二轮B05→B06→B07→B08，修领域语义与交付；第三轮B09→B10→B11→B12，做专利/评测/真实增强及完整验收。

- B01可先接当前endpoint；B02改精度契约后同步更新client。
- B03先固定发布指针，B08才能捕获确定baseline。
- B06算法无需等B11；B11真实执行依赖B06分类与证据链。
- B09/B10的确定性用例不依赖真实OCR/gold；正式分数单独待人工标注。
- ST.26附件保持上一方案T17后续增强，不作为本轮核心修复前置。

**每任务检查命令：** 根据文件范围先运行针对性测试；跨模块任务结束后运行：

```powershell
.venv\Scripts\python.exe -m pytest -o addopts='' -m 'not live' -q
npm --prefix web run build
```

前端增加高风险交互测试runner后运行 `npm --prefix web run test -- --run`。真实PDF视觉定位必须在浏览器核验，mock通过不能替代。每轮报告需记录实际命令和输出，测试skip说明原因。

## 14. 可直接交给执行模型的指令

```text
请按 docs/superpowers/plans/2026-10-14-bio-pdf-post-m1-remediation.md 执行B01–B12。
先读 reports/2026-10-14_post_m1_review.md，对当前HEAD核验缺陷，保留已有改动。
现有351测试通过不代表M0/M1完成，优先补跨模块反例，再实现修复。
数据写到data/post-m1-demo；旧报告/上传文件/旧job保留。
按三轮顺序执行，每任务独立提交与验收；不要只修模块而不接API/前端/导出。
没有人工gold只延期正式分数；没有第二通道只延期真实二次解析，继续完成接口、算法与回归。
最终交付执行矩阵、可追溯artifact inventory、真实before/after及定位截图。
不能以过滤完数字、伪造表、猜字段或默默回退其他引擎宣称准确；原始证据始终可查。
```
