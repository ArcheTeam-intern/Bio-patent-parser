# 生物论文与专利 PDF 增强解析：具体修改与验收方案

> **For agentic workers:** 使用 executing-plans 按任务执行；任务以 checkbox 跟踪。只有用户授权并行代理时才采用 subagent-driven-development。本文是交给其他模型的执行方案，本轮仅查代码、查制品和写文档。

**Goal:** 提高序列和活性数据的正确提取率，使每个字段可回到原文，并保证重试、审核和导出不会损坏已完成结果。

**Architecture:** 保留 Python 确定性阶段工作流、SQLite、不可变制品、FastAPI 和 React。原生文本保留原始字符与位置，Docling/MinerU提供结构/OCR候选，领域规则负责筛选、组合、校验；冲突保留双方证据并进入审核。先修真实误提、漏提和证据链，再增加受控的区域复解析及模型辅助。

**Tech Stack:** 当前 Python 3.12、Pydantic 2、pypdfium2、Docling SDK/服务适配器、MinerU HTTP 适配器、FastAPI、SQLite、React 18、TypeScript、PDF.js；不需要引入 LangChain/LangGraph 完成这些改动。

---

## 0. 执行约束与当前基线

- 项目根目录：`E:/GoLandCode/Bio-patent-parser`。
- 先读 `docs/BIO_PDF_HANDOFF.md`、`docs/BIO_PDF_DEMO_UI_HANDOFF.md` 和 `reports/2026-10-14_run_review.md`。
- 方案所依据的源码基线：审查时 HEAD `5789129`。执行者先检查当前差异，已被其他改动修复的问题不重复修复。
- 当前测试基线：298 passed、4 skipped；前端 `npm run build` 通过。这是工程基线，不是提取准确率。
- 原任务：`job-8415a1c6daa0`，源 SHA-256 `d927d1c3d8f80fe396d1090190bac25e944ade61b74683fffcab382cc596da35`，12页论文 *The Anticancer Peptide TAT-RasGAP317-326 Exerts Broad Antimicrobial Activity*。
- 新优化任务使用独立 `data/optimization-demo`；旧 `data/demo` 保持可审查，不能通过改写现有制品让旧结果看似正确。
- 所有阈值先作为配置和待验证策略；不能把解析引擎置信度、字母表检查或双引擎一致说成真实准确率。
- 只改本项目，不改 `docling/` 上游仓库。每个阶段完成后提交一个可审查的变更；提交范围限定于该任务。
- 本文涉及新增字段，须同时更新 Pydantic、API、TS、JSON Schema/导出和旧制品兼容读取器。

### 实际问题比上轮报告更具体

| 环节 | 源码/制品事实 | 影响 |
|---|---|---|
| 页面路由 | InspectStage 写路由，LayoutParseStage 只读全局 engine 并固定 layout_native | 扫描页的 OCR 策略没有进入正式工作流 |
| 文本融合 | NormalizeStage 以 docling 文件存在就选择 docling，否则 native | 原生通道跑了，但没有拿来校验/补充最终文本 |
| 序列 | inline.py 仅限制任意字母、空白和长度>=15；ALLOWED_ALPHABETS未使用 | 作者/药物/菌名误提；第9页两段10残基真实序列漏提 |
| 短序列 | 原文有 RRRQRRKKRG、DTRLNTVWMW | 应提取明确展示的片段，不能自行拼成完整合成肽 |
| 活性 | prose.py 的 M 无单位词边界；任何数值+单位均生成 Measurement | 17 May 2017 被提成 17 M；条件值混成结果 |
| 表格 | Docling raw 识别第7页菌株用途表、第9页动物评分表，没有第8页药敏表 | 55条所谓活性来自非目标表格，metric 是 Used/Notes/value/Animal/Score |
| 实体关系 | handler 调用 link_explicit_relations([], sequences, measurements, []) | 样例 relations=0，83条中 assay_id非空=0 |
| 证据 | 序列证据后补页码并截到200字符；前端始终传bbox=null | 多页与长序列证据丢失，无法定位字符/单元格 |
| 校验 | validate_evidence 对dict使用getattr | 有位置的证据也可能被错误判为unknown |
| 评测 | match_sequences跳过seq_id_no=None的所有候选 | 当前论文的作者/药物误提不进入序列FP统计 |

原生字节流解析是读取PDF文本层，不等于有正确阅读顺序、正确ToUnicode映射和完整序列。采用它的目的应是尽量保留原始字符与可回查位置；是否正确仍需核验。

## 1. 方案选择、依赖与交付顺序

可选方向：A. 在现有架构内修规则、证据和路由（推荐）；B. 只替换解析器（无法解决通用表格误作活性、实体错配、评测遗漏）；C. 将整篇交给多模态模型生成JSON（可提供辅助，但字符级序列无法由它独立证明）。本方案采用A，并把B/C作为有界复解析能力。

分成五个可独立验收批次，每批完成后保留一份同文档的对比报告：

| 批次 | 任务 | 必须达到的效果 |
|---|---|---|
| M0 可靠性 | T01–T03 | 任务配置生效、制品可恢复、并发写被隔离 |
| M1 当前论文修复 | T04–T08 | 不再输出作者/日期/普通表头；提取两段短序列；点击真实证据可定位 |
| M2 领域与专利 | T09–T11 | 多层表头、药敏分类、跨页序列、实体与实验方法可追溯 |
| M3 审核与交付 | T12–T14 | 可正确修订、冲突不覆盖、导出快照一致、页面可调试 |
| M4 评测与增强 | T15–T17 | 评测覆盖无SEQ ID序列；区域复解析受控；附带序列文件可接入 |

依赖：T02依赖T01；T03依赖T02；T04依赖T01/T02；T05依赖T01/T04；T06依赖T01/T05；T07/T08依赖T06；T09依赖T05/T06；T10依赖T06/T07；T11依赖T08/T09/T10；T12依赖T03/T06/T11；T13依赖T03/T12；T14依赖T06/T12/T13；T15的负例先随T07/T08加入，完整评测依赖T11；T16依赖T04/T05和T09语义提取，固定反例不等人工gold齐备就可执行；T17独立增强但需T01/T06/T10。

## 2. T01：统一配置、版本与契约，先冻结修改边界

**修改：** `src/bio_pdf/contracts/common.py`、`document.py`、`records.py`、`job.py`、`contracts/__init__.py`、`api/contracts.py`、`application/job_service.py`、`configs/demo.toml`、`web/src/api/client.ts`。

**新增：** `src/bio_pdf/config.py`、`src/bio_pdf/contracts/compat.py`、`src/bio_pdf/contracts/extraction.py`、`tests/contracts/test_contract_compat.py`。

- [ ] 统一domain `schema_version="1.1.0"`；HTTP envelope继续 `api_version="v1"`。接受旧domain `1.0.0`/`v1`，由compat显式补默认字段；读未知主版本返回SCHEMA_UNSUPPORTED，禁止宽松忽略未知字段。
- [ ] 新增WorkflowConfig：parser、ocr_policy、pages、targets、document_type_hint、rule_versions、parser_options、reparse预算；从真实TOML加载并在job创建时持久化完整effective_config及其hash。现有options_ref字符串不代表实际配置加载。
- [ ] 保留raw字段；normalization变换要记录来源和规则，禁止覆盖raw_text。数值始终是Decimal可验证的字符串。
- [ ] 增加明确的块语义角色：保留kind，新增semantic_role（body/title/caption/author/header/footer/reference/claims/methods/sequence_listing/unknown）。角色不确定时unknown，不依靠人名黑名单判断。
- [ ] TextBlock增加ordinal、source_refs；TableCell增加header_role（column/row/both/data/unknown）、source_refs，允许text_raw为空字符串，区分空格与缺失cell。Table增加footnote_refs、caption_refs和结构检查摘要。
- [ ] EvidenceSpan扩展如下；不从ID反解页码。原有bbox仍用于单个主要框，更多框通过fragments保存，旧证据由compat映射成一段。

```python
class EvidenceFragment(_StrictModel):
    page_number: PageNumber
    bbox: BoundingBox | None = None
    block_id: NonEmptyStr | None = None
    table_id: NonEmptyStr | None = None
    cell_id: NonEmptyStr | None = None
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)  # Python/Unicode字符半开区间
    raw_text: str | None = None
    source_artifact_id: NonEmptyStr | None = None

# EvidenceSpan增加以下字段，原字段保留兼容：
source_parser: str | None = None
source_locator: str | None = None
fragments: list[EvidenceFragment] = Field(default_factory=list)
location_precision: Literal["character", "cell", "block", "page", "none"] = "none"
```

- [ ] Measurement增加entity_raw、target_id、metric_raw、assay_id；entity_id只能是实体对象ID。旧表格把row label存entity_id的记录由compat保留到entity_raw，不能宣称已经成功链接。
- [ ] SequenceRecord增加entity_id、source_kind（listing/inline_fragment/table/reference_attachment）、sequence_scope（fragment/full_listing/undetermined）、residue_annotations和source_refs；D/L、retro-inverso、修饰、连接子作为注释保留。
- [ ] 增加AssayRecord（assay_id/document_id/method_raw/target_raw/conditions/condition_evidence/evidence_ids）、MetricDefinition（name/value_kind/unit_requirement/target_requirement/entity_requirement）、RawMention（mention_id/text/role/evidence_ids/reason_codes）。RawMention单独产物，用于条件或未能确认的数值，不计为Measurement。
- [ ] `contracts/extraction.py`统一批量返回结构；如定义泛型records，请同步所有调用方，而不是序列提取器返回list、活性提取器返回batch混用。

```python
from bio_pdf.contracts.common import EvidenceSpan, _StrictModel
from bio_pdf.contracts.records import Measurement, SequenceRecord
from pydantic import Field

class RawMention(_StrictModel):
    mention_id: str
    text: str
    role: str
    evidence_ids: list[str]
    reason_codes: list[str] = Field(default_factory=list)

class ExtractionBatch(_StrictModel):
    records: list[SequenceRecord | Measurement] = Field(default_factory=list)
    evidence_spans: list[EvidenceSpan] = Field(default_factory=list)
    raw_mentions: list[RawMention] = Field(default_factory=list)
    diagnostics: list[dict[str, str]] = Field(default_factory=list)
```

RawMention以此类为单一定义，`records.py`/API需要时re-export，禁止重复定义。序列/活性batch分别保证records内容类型；handler聚合时做显式类型检查。
- [ ] 更新export schema和TS枚举；导出manifest含schema_version、pipeline_version、各规则version、parser版本、effective_config_hash、source_sha256、coverage、warnings。

**验收夹具：** 旧fixture_basic_records.json可以加载；带旧字符串entity_id的表格结果读出entity_raw但不造实体；新EvidenceSpan多页往返不丢片段；未知字段仍报错；空表格格子可表示。

**命令：** `.venv\Scripts\python.exe -m pytest tests/contracts -q`。

## 3. T02：缓存与制品链，修复“恢复后仍是旧结果”

**修改：** `workflow/runner.py`、`workflow/stages.py`、`workflow/handlers.py`、`storage/repository.py`、`storage/artifacts.py`、`contracts/job.py`。
**新增：** `storage/result_reader.py`、`tests/unit/test_stage_manifest.py`。

现状：cache key基本只有源路径/mtime/大小和options，未包含上游hash；恢复仅核验first artifact；阶段写固定路径且不可变写入器拒绝不同内容；adapter又直接write_text覆盖。因此规则升级、上游重跑和恢复不是稳定闭环。

- [ ] StageContext增加stage_run_id、input_artifact_refs和lease_token。每次尝试写 `jobs/<job_id>/stage_runs/<stage_run_id>/...`，不覆盖旧成功产物。
- [ ] handler用输入ArtifactRef读取所需文件，停止推断上一阶段固定路径。引入ResultReader统一读取“job当前发布的制品”；API/ReviewService/ExportService使用它，兼容旧jobs路径。
- [ ] 每个阶段写stage manifest：stage_run_id、所有inputs/outputs的ID/hash/relative_path、rule_versions、config_hash、page_coverage、warnings、review_required。stage.artifact_manifest_ref指向manifest本身，不再指first output。
- [ ] cache key采用以下结构，去掉路径/mtime作为业务身份：

```python
key = make_cache_key({
    "stage": stage_name,
    "document_sha256": document_sha256,
    "pages": sorted(requested_pages),
    "input_artifact_hashes": sorted((name, ref.sha256) for name, ref in inputs.items()),
    "effective_options": effective_stage_options,
    "implementation_version": stage_version,
    "provider_version": provider_version,
})
```

- [ ] 恢复时核验manifest与全部输出；任一缺失/变更重跑该阶段，并按实际新输出hash判断下游缓存能否复用。禁止只验证normalized而忽略raw_response/manifest。
- [ ] adapter输出也统一原子写入独立stage-run目录；生产者stage_run_id正确写入ArtifactRef。
- [ ] 修复`artifacts`以内容hash为唯一ID却在register时覆盖relative_path的问题：同内容在两个job目录出现，后注册会让旧job引用指向新job。保留blob内容hash，artifact occurrence另用stage_run_id+logical_name生成唯一artifact_id；旧hash ID兼容可读，新同内容不同路径不能互相覆盖。给双job相同inspection.json增加回归。
- [ ] 成功阶段全部完成后原子发布result manifest（指向normalized/extract/validation）；发布前现有结果仍可读。用户修订引用的是发布基线，不随工作目录变化。
- [ ] 缓存跳过验证阶段后，最终status由已发布结果和当前open reviews计算；不能因为review_total初始为0就把有待审核任务标为succeeded。
- [ ] 缺选页/部分页输出保存coverage问题；明确partial policy，当前默认coverage未满足则任务failed，不能对空页/漏页伪报完成。

**验收：** 改extract规则版本只重跑extract及受影响下游；篡改非first raw文件使其缓存无效；第二attempt不发生ARTIFACT_CONFLICT；恢复95条待审核任务仍succeeded_with_review；旧任务仍可读；provider版本入manifest。

**命令：** `.venv\Scripts\python.exe -m pytest tests/unit/test_runner_safety.py tests/integration/test_workflow_resume.py tests/unit/test_stage_manifest.py -q`。

## 4. T03：队列恢复、租约与审核项幂等

**修改：** `workflow/queue_worker.py`、`storage/repository.py`、`application/job_service.py`、`api/routes/jobs.py`、`review/plan.py`、`workflow/runner.py`。
**同时修改：** `api/dependencies.py`、`api/app.py`的连接生命周期；新增 `tests/api/test_concurrent_transactions.py`。
**测试：** `tests/integration/test_queue_worker.py`、`tests/integration/test_repository.py`、`tests/unit/test_review_revision.py`。

- [ ] claim返回ClaimedJob(job,worker_id,generation,lease_until)，不只返回Job。generation必须进入续租、阶段提交、结果发布、结束状态和release条件。
- [ ] 每次请求、导出后台任务、worker执行与heartbeat分别使用独立SQLite连接。当前API复用app.state.job_service的单连接，`check_same_thread=False`仅允许跨线程访问，不会隔离并发BEGIN/COMMIT；不同请求可能加入/提交对方事务。启动只做migration与保存service factory，dependency创建/关闭请求连接，后台任务自己开连接，禁止捕获已结束请求service。
- [ ] generation单调递增并持久化在job或不删除的lease历史中，release只标记释放或按条件删除后保留generation；不能每次删除lease导致下一次重用generation=1。
- [ ] 队列resume意图持久化为execution_mode=resume；worker调用run_job(...,resume=True)。首次run保持False，崩溃后reclaim采用resume。
- [ ] running且有效租约的任务收到resume返回409 JOB_RUNNING；不能直接重置为queued让第二worker领取。
- [ ] 每30秒续900秒租约，后台续租使用独立Repository连接；不要跨线程复用SQLite连接。heartbeat返回rowcount/boolean；失去租约则取消解析子进程并停止发布。
- [ ] 单纯续租仍不足：每次关键写入/提交使用worker_id+generation fencing；旧代worker完成后不得覆盖新代status、review或published result。未发布stage-run文件可留作审计。
- [ ] ReviewItem ID从排队顺序idx改为(job_id,baseline_id,record_id,record_revision)的稳定hash。重建同基线审核计划不会重开resolved item；语义改变要求创建新job，避免给旧审核项换记录。
- [ ] 同一job resume只允许同配置/规则基线；“重新解析/换规则”创建新job并关联parent_job_id。审核后的旧job恢复不覆盖head；部分失败但无发布结果可以继续发布首个基线。

**事务核心：**

```sql
UPDATE worker_leases
SET heartbeat_at = ?, lease_until = ?
WHERE job_id = ? AND worker_id = ? AND generation = ?;
-- rowcount != 1 视为 lease lost
```

关键提交必须在检查lease有效性后同一事务写DB。文件写到隔离目录，最终manifest指针更新放同事务；不能先检查、等待几分钟、再无条件更新。

**验收：** 两worker同时claim只有一个成功；超时回收后旧worker写status/发布被拒；运行中resume返回409；失败任务resume复用有效阶段；审核后恢复不新增/重开原resolved item。使用可注入时钟，不靠真的等待15分钟测试。

**并发事务验收：** 两请求对不同record同时审核，一请求故意rollback，另一请求仍成功且历史完整；导出与审核并发不出现cannot start a transaction within a transaction；API重启不遗留共享事务。

## 5. T04：让逐页分类真正驱动解析/OCR

**修改：** `pdf/quality.py`、`workflow/handlers.py`的Inspect/NativeParse/LayoutParse、`application/job_service.py`、`api/routes/jobs.py`、`api/app.py`、`parsers/base.py`、`docling_adapter.py`、`docling_child.py`。
**新增：** `workflow/parse_plan.py`、`parsers/registry.py`、`tests/unit/test_parse_plan.py`。

- [ ] create_job校验选页1..page_count，重复页拒绝422，空/None语义为全页；persist ocr_policy；不可用parser在入队前返回ENGINE_UNAVAILABLE。
- [ ] 修复image coverage：将图片bbox裁到可见page，计算矩形union面积；当前直接求和再clamp不是union。检测无法提供隐藏文字信息时记unknown，不默认声称无隐藏层。
- [ ] 将quality.json消费为明确ParsePlan，记录requested pages、primary/secondary任务、OCR语言、原因与预算。
- [ ] 默认规则如下，之后用开发集调参数：

| 页面信号 | primary | supplemental | 必须记录 |
|---|---|---|---|
| 有正常原生文本、普通段落 | native | 布局解析按配置 | native/OCR字符来源 |
| 原生文本正常且表格/序列候选区 | layout_native | native保留作字符校验 | 未识别区域/表格证据 |
| 扫描/原生乱码/隐藏层可疑 | layout_ocr | native保留作对照 | OCR实际执行、语言、覆盖 |
| 图文混合页 | layout_ocr或指定区域OCR | native | OCR覆盖区域，避免重复覆盖正常正文 |
| 空白页 | blank outcome | 无 | 已检查而非漏页 |

- [ ] ocr_policy=force要求所有非blank请求页实际OCR；disabled禁止OCR但扫描页保留SCAN_WITH_OCR_DISABLED问题；auto由signals选择。native parser本身不支持force，入队返回422 OCR_UNSUPPORTED，不能静默无效。
- [ ] Docling profile不再固定layout_native；透传配置。来自[官方pipeline options](https://docling-project.github.io/docling/reference/pipeline_options/)的TableFormer模式、OCR选项仅作为可配置选项，不能标为准确率保证。执行时按已安装SDK实际支持字段适配并pin版本。
- [ ] 修复docling_child的page_range(min,max)覆盖非连续选页：将{2,4}拆成连续范围任务，不把第3页混入结果；空页集合先解析为有效full list；合并时校验原始页号。
- [ ] registry实际接入native/docling_sdk/docling_serve/mineru_http；没有服务地址/能力探测失败的引擎不出现在可执行选项。MinerU adapter存在不等于正式工作流已支持。
- [ ] 启用Docling子进程超时、取消和stderr/log文件；超时返回ENGINE_TIMEOUT，不无限阻塞worker。Windows进程组与子进程终止要确保不留下推理进程。
- [ ] manifest区分capabilities（能做什么）与observations（此次实际OCR页、表格数量、覆盖）。tables=True不能当作本次识别到了表格。

**验收：** mixed三页夹具分别命中native/layout_ocr/blank；force与disabled有不同effective plan；重叠图片的coverage准确；{2,4}只输出2/4；无Tesseract/无MinerU返回明确能力问题。真实扫描样本单独人工确认，数字PDF渲染后只能用于合成OCR回归。

## 6. T05：归一化正确性与双通道融合

**修改：** `parsers/normalize_docling.py`、`parsers/mineru_adapter.py`、`pdf/native.py`、`workflow/handlers.py`的NormalizeStage。
**新增：** `parsers/fusion.py`、`parsers/table_grid.py`、`tests/unit/test_parser_fusion.py`、`tests/fixtures/providers/docling/two_tables_same_page.json`。

- [ ] 所有block/table/cell ID至少包含parser run、页、table ID和provider locator；Docling cell不能再只用p:t:c_idx；native:block_id不能跨页重用native:rN。
- [ ] 验证同normalized document中ID唯一；同ID不同内容/位置返回NORMALIZATION_ID_COLLISION，不能被dict覆盖。
- [ ] Docling读取其真实row_header/column_header标记，解析caption/footnotes引用与body树阅读顺序；不把caption、作者、参考文献label一律折成text。
- [ ] 修复缺page/size时默认第1页和612×792：从DocumentInspection补确切geometry；定位缺失保留none+warning，不能编造页号。cell无prov但有bbox时根据upstream坐标系声明变换；无法明确则保留缺失。
- [ ] MinerU HTML采用occupancy grid，rowspan/colspan占位覆盖后续行。每行从第一个未占列放cell；检测重叠，grid指回原始cell ID，不复制伪造新证据。
- [ ] 空cell、行列数量、header链、caption和footnote保留；span越界/重叠作为结构问题，不能让错表仍checks_passed。
- [ ] NormalizeStage按ParsePlan选择primary结构，同时保存native/layout/OCR的source projections。不要直接串接两路全文；同区重合形成对应关系，否则会重复提取。
- [ ] 文本融合策略：结构与阅读顺序由layout提供；只有空间重合且文本对齐明确、native quality正常的块，才将native内容作为preferred lexical source。字符冲突保留alternatives并阻止自动接受。
- [ ] 原生rect不假定必为一行且互不重叠；去除同范围重复文本，保存被合并block引用。双栏阅读顺序按列/版面区域处理，跨栏序列不能仅按全页y排序拼接。
- [ ] Greek µ/μ→u仅用于unit_normalized；序列中不可全局I/l/1、O/0等替换。新增source alignment解释为何选择哪个字符，模糊时保留原字符并进入审核。

**网格放置参考算法：**

```python
occupied: dict[tuple[int, int], str] = {}
for row_index, row_cells in enumerate(rows):
    col_index = 0
    for cell in row_cells:
        while (row_index, col_index) in occupied:
            col_index += 1
        for r in range(row_index, row_index + cell.rowspan):
            for c in range(col_index, col_index + cell.colspan):
                if (r, c) in occupied:
                    raise ValueError("TABLE_GRID_OVERLAP")
                occupied[r, c] = cell.cell_id
        cell.row_index, cell.column_index = row_index, col_index
        col_index += cell.colspan
```

**验收：** 同页两表第0个cell ID不同且证据不串；span合并表头覆盖正确；原生与layout相同数值只产生一次候选，来源保留双方；1/I冲突不静默选一个；丢尺寸不会伪造geometry。

**命令：** `.venv\Scripts\python.exe -m pytest tests/contracts/test_docling_normalizer.py tests/unit/test_mineru_adapter.py tests/unit/test_parser_fusion.py -q`。

## 7. T06：把证据在提取时生成，停止事后猜页码

**修改：** `workflow/handlers.py`的ExtractStage、`extract/sequences/inline.py`、`st25.py`、`extract/activities/prose.py`、`tables.py`、`validation/evidence.py`、`api/routes/results.py`。
**新增：** `extract/evidence.py`、`tests/unit/test_evidence_index.py`。

- [ ] 提取器返回ExtractionBatch(records,evidence_spans,raw_mentions,diagnostics)，证据由实际匹配offset/source cell生成，不能让handler通过ID字符串后补第1页。
- [ ] 创建evidence ID采用document_id+parser_run_id+source_locator+span offset的hash；它是opaque ID。raw_text保存完整命中表达，不截200字符。上下文excerpt只用于UI，与证据正文分开。
- [ ] char_start/end相对不可变block.text_raw；先对raw匹配再做normalization，确保text_raw[start:end]==span.raw_text。多行序列用多个fragment保持顺序。
- [ ] 在glyph映射未经过验证前，bbox只能标block级；不要用字符串长度等分bbox伪造字符定位。原生精确搜索可返回文本范围rect时，需要含Unicode/合字/旋转的映射测试。
- [ ] field_evidence覆盖value、metric、unit_raw、entity_raw、entity_id、target_raw、target_id、assay_id及conditions。所有来源引用集合必须包含在record.evidence_ids里或作为明确依赖可加载。
- [ ] 按EvidenceSpan模型构建evidence index；相同ID和完全相同payload合并，相同ID不同payload报错。修复validate_evidence的dict/getattr混用。
- [ ] 区分EVIDENCE_EXISTS、EVIDENCE_DOCUMENT_MATCH、EVIDENCE_PAGE_VALID、EVIDENCE_BBOX_VALID、EVIDENCE_TEXT_MATCH和EVIDENCE_FIELD_COVERAGE。geometry校验调用validate_bbox；page-only不是精确geometry pass。
- [ ] 批量 `/jobs/{job_id}/evidence?ids=...` 或POST lookup支持多个ID，响应含baseline_id；旧单ID路径保留。ID必须归属当前published baseline/允许的审核版本。
- [ ] M1同时完成最小前端证据接入：将client已有getEvidence的Record<string,unknown>改typed EvidenceData，Workbench点击记录按API返回page/bbox定位。T14再补字段导航、多页切换、编辑与布局；这样M1已有真实证据高亮可验收。

**最小前端修改：** `web/src/api/client.ts`、`web/src/features/workbench/WorkbenchPage.tsx`；初次typed response至少定义page_number、bbox、fragments、location_precision，不再读ID页码。

**参考校验逻辑：**

```python
span = EvidenceSpan.model_validate(evidence_index[eid])
if span.document_id != record.document_id:
    outcome = "fail"
elif span.page_number is None:
    outcome = "unknown"
elif span.bbox is None:
    outcome = "unknown"  # page anchor有效，但不能标为有精确坐标
else:
    page = page_geometry[span.page_number]
    validate_bbox(span.bbox, width_pt=page.width_pt, height_pt=page.height_pt)
    outcome = "pass"
```

**验收：** 第9页短序列与所在block/bbox关联；>200字符长序列仍可核验尾部；续页都有fragment；表格field点击能加载行/列头与数值；没有page不默认为1；超边界bbox和跨document evidence被拒。

## 8. T07：序列候选规则，同时降低误提与补短片段

**修改：** `extract/sequences/inline.py`、`amino_acids.py`、`variants.py`、`validation/sequences.py`、`workflow/handlers.py`。
**新增：** `extract/sequences/context.py`、`tests/fixtures/domain/inline_sequence_cases.json`。
**测试：** `tests/unit/test_sequence_candidates.py`、`test_validation.py`。

- [ ] 替换“整block都是字母+长度>=15”的检测，分三条入口：listing/table明确序列字段；prose中明确序列上下文+独立残基串；无上下文的长字母串只入diagnostics而不进入正式序列候选。
- [ ] 检查蛋白/DNA/RNA对应alphabet，但alphabet只作为必要条件；很多普通单词也完全落在蛋白alphabet中，不能因此确认为序列。
- [ ] 优先排除已明确的author/reference/header/footer角色；普通表格药名/菌种不扫描为序列。表格的sequence/amino-acid/CDR列仍可扫描，不能用“table”一词将整页禁掉。
- [ ] 正文以明确提示词和局部窗口判定：amino-acid sequence、residues、nucleotide sequence、CDR、对应SEQ ID、DNA/RNA/protein及括号残基串。句子边界和引文括号不能跨越到下一条候选。
- [ ] 短肽不使用全局15字符门槛。明确上下文允许短序列（测试至少覆盖2/5/10残基，是否成为可接受数据仍需上下文/审核）；无上下文长串只作待分类原始线索。
- [ ] 明确DNA/RNA/protein优先，ACGT串无分子类型提示时unknown，不凭alphabet与长度猜DNA；含小写的核酸只在明确核酸/listing context下转大写。
- [ ] 去掉全block `_VETO_WORDS`。例如同段“Table 1 lists peptide sequences … (RRRQRRKKRG)”仍应提取，不用某个单词否决整段。
- [ ] 保留D/L氨基酸、retro-inverso、N/C端修饰、连接子与位点定义的证据。RRRQRRKKRG、DTRLNTVWMW在当前原文是natural moiety片段；不能直接输出两段+GG作为完整合成肽，也不能自行reverse后宣称原文就是该串。
- [ ] observed_length按残基位置计，未知token占一位而非丢弃；声明长度/闭合边界/完整原文范围共同决定is_complete，molecule_type未知留审查原因。
- [ ] 精确同一source span的双路候选合并来源；相同序列用于不同实体/位置时保留不同记录，不按sequence字符串跨全文合并。

**固定输入/输出回归表：**

| 输入 | 输出 |
|---|---|
| author角色：Christian Widmann | 0条序列，诊断NON_SEQUENCE_ROLE |
| 普通表格：Benzylpenicillin、Cefuroxime axetil、Staphylococcus capitis | 0条序列 |
| 正文：HIV TAT protein (RRRQRRKKRG) | 1条protein片段，sequence=RRRQRRKKRG，证据为这10字符与上下文 |
| 正文：human RasGAP protein (DTRLNTVWMW) | 1条protein片段，sequence=DTRLNTVWMW |
| 正文：DNA sequence acgtacgtac | DNA片段ACGTACGTAC，raw保持原拼写 |
| 无上下文：AAAAACCCCCGGGGGTTTTT | 0条正式候选，保留raw mention |
| 提示词+序列RRRQRRK1RG | 保留1为未解析位置，不变成I/删除 |
| “at least 90% sequence identity to SEQ ID NO:7” | SequenceReference，不生成残基串 |

**最小负例测试先加：**

```python
def test_author_and_drug_are_not_sequences():
    blocks = [
        {"text": "Christian Widmann", "kind": "text", "semantic_role": "author", "page_number": 1},
        {"text": "Benzylpenicillin", "kind": "table", "semantic_role": "body", "page_number": 8},
    ]
    assert extract_inline_sequences(blocks, "doc").records == []
```

接口随T06变成ExtractionBatch；旧测试按records迁移，不能为保留错误旧行为而弱化新规则。

**命令：** `.venv\Scripts\python.exe -m pytest tests/unit/test_sequence_candidates.py tests/unit/test_validation.py -q`。

## 9. T08：正文活性：词边界、结果/条件判别、局部上下文

**修改：** `extract/activities/prose.py`、`values.py`、`assays.py`、`validation/activities.py`。
**M1最小表格护栏同时修改：** `extract/activities/tables.py`、`workflow/handlers.py`，先以MetricDefinition筛选header，Used/Notes/Animal/Score不进入默认bioactivity；停止默认measured，未分类整表入diagnostics。T09再补完整类型/多层表头算法，避免M1回归仍产生原来55条噪声。
**新增：** `extract/activities/metrics.py`、`extract/activities/mentions.py`、`tests/unit/test_activity_prose.py`。

- [ ] 先修单位边界，至少不再将May/Mouse/Model的M匹配为浓度单位；保留合法`5nM`、`5 nM,`、`5µM`。单位大小写敏感（mM与mm不同），前后边界不可统一用\b处理%/µ。
- [ ] 第一补丁可给unit组后加`(?![A-Za-zµμ])`；最终匹配用数值表达tokenizer，识别完整比较符/区间/误差/科学记数表达式。`1 × 10^-9 M`不支持时保留整表达unparsed，不能退化提取尾部`9 M`。
- [ ] parse_activity_value增加结构不变量：所有数字Decimal可解析且finite；range lower<=upper；uncertainty>=0；禁止混合scalar/range字段；categorical不能带number。扩展科学记数时保存raw和规范值，禁止float格式化。
- [ ] 建立MetricDefinition registry：MIC/MBC/IC50/EC50/Kd/Ki/kcat/Km及有来源支持的inhibition/viability/fold_activity/antimicrobial_susceptibility等；规范名与metric_raw分开。未知指标不强塞value，进入未分类mention审核组。
- [ ] 单位registry按metric扩展s^-1、min^-1、U/mg、M^-1·s^-1等酶指标单位，并保留其表达式来源。浓度、质量浓度、比活力、比例不同维度不能混用；ug/mL↔nM没有同一分子量来源时不换算。
- [ ] 按句子、caption、table footnote范围构造上下文；指标/实体/靶标/结果动词可在同句里关联，而不是只找数字前40字符。跨句链接必须有明确指代且保留依赖证据；有多个实体时不选最近的一个。
- [ ] RoleClassifier区分result、condition、statistic、metadata、claim、cited、undetermined。20µM若表示treatment dose存Assay condition；95%若为CI存统计说明；若是cell viability结果，需metric、entity、assay语境后才能成为Measurement。
- [ ] 实测判定需要结果语境与实验来源，方法说明是强证据；没有方法链接但有明确实验结果可保留undetermined待审核。Claims section/“至少、低于”阈值判claim_threshold，引用其他论文的数字标cited_result。
- [ ] 不默认将所有prose标general_statement；按分类传真实evidence_kind和原因。OCR只看到数字、不知含义时保存mention，不伪造bioactivity。
- [ ] 去重仅针对相同源span/两路对齐出来的同一次观测；同值、同单位但条件/实验不同不合并。正文与表格复述同一次结果可建duplicate_group，未确认相同观测前仍保留两条。

**必须覆盖的夹具：**

| 文本 | 预期 |
|---|---|
| Accepted: 17 May 2017 | 0 Measurement，也不生成17 M mention |
| compound A had an IC50 of >128 µg/mL against target B | IC50，operator=>，number=128，单位ug/mL；实体/靶标证据可查 |
| MIC was 1–10 nM | range完整，不是10 nM |
| IC50 = 1.2 ± 0.3 nM | plus_minus完整，不是0.3 nM |
| cells were treated with 20 µM peptide A for 2 h | condition concentration/time，不默认活性结果 |
| peptide A reduced viability to 95% after 2 h | viability结果+时间条件；不是IC50 |
| 95% confidence interval | statistic，不是活性 |
| 1 × 10^-9 M | 完整科学记数或unparsed完整raw，不是9 M |
| Claim 1: peptide A has MIC less than 10 µM | claim_threshold，不能变实测 |

**测试命令：** `.venv\Scripts\python.exe -m pytest tests/unit/test_activity_prose.py tests/unit/test_activity_values.py tests/unit/test_evidence_kind.py -q`。

## 10. T09：生物表格：先判类型，再解析矩阵和层级表头

**修改：** `extract/activities/tables.py`、`workflow/handlers.py`、`contracts/document.py`、`validation/activities.py`。
**新增：** `extract/activities/table_classifier.py`、`header_graph.py`、`tests/fixtures/domain/table_susceptibility.json`、`table_multilevel_mic.json`、`table_strain_inventory.json`、`table_animal_score.json`。

- [ ] 禁止任意table→Measurement。TableClassifier使用caption、section、header、单位和数据形态，分activity_numeric/susceptibility/sequence/assay_conditions/inventory/scoring/unclassified。
- [ ] 当前默认bioactivity profile中Used in/Notes菌株目录不生成活性；Animal ID/Score评分表保留结构但不列为分子活性。未来临床/动物表型profile可独立支持，不能把所有生物表格都称无效。
- [ ] 通用 `_HEADER_RE` 解析成功不等于metric识别成功。只有MetricDefinition或有明确语义的指标映射才输出Measurement；unclassified表创建一项表级语义审核，不要每格生成噪声记录。
- [ ] 构建header graph，支持column_header/row_header层级、span和脚注单位继承。每个data cell产生完整header_path与所有字段来源；重复单位或靶标冲突留unknown，不取最后一个。
- [ ] 支持两种方向：实体在行、靶标/指标在列；靶标/菌种在行、药物在列。不能永久把col0当entity。
- [ ] 表9页评分0/1/2不能作药物活性；Table 2中的S/R/I则可以是`antimicrobial_susceptibility` categorical结果，entity=drug、target=bacterial strain、unit不适用。
- [ ] S/R/I仅在susceptibility表分类及legend成立时解析为分类；保留legend对I的原文说明，不假设所有年代/实验标准中的I语义完全相同，不从S/R反推MIC数值。
- [ ] value/unit/entity/target/metric的field_evidence分别指向data cell/表头链/行头/脚注。表格级主证据不能替代全部字段来源。
- [ ] 删除handler强制`measured`。TableContext含section/caption/method/citations，传classify_evidence_kind；专利阈值表claim_threshold，来源不明undetermined。
- [ ] 跨页表通过continuation_group、列宽/header fingerprint/caption号识别；只有明确续表且列映射一致才继承header。两张相似表不同实验条件不自动拼接。

**可人工阅读的合并头夹具：**

```text
row0: Peptide [rowspan=2] | E. coli [colspan=2] | S. aureus [colspan=2]
row1:                     MIC (µM) | MBC (µM)   | MIC (µM) | MBC (µM)
row2: A                   >128     | ND         | 8        | 16
```

必须得到4个观测：A/E.coli/MIC/>128、A/E.coli/MBC/ND、A/S.aureus/MIC/8、A/S.aureus/MBC/16；每条target来自父表头，metric/unit来自子表头，entity来自row2 col0。

**药敏夹具：**

```text
Species/strain | Benzylpenicillin | Cefuroxime axetil
E. coli A      | R                | S
S. capitis B   | S                | I
```

必须得4条分类结果，target为菌种/strain、entity为药物；不产4条MIC，也不产药物/菌种序列候选。

**真实页8漏表恢复：** Docling raw就没有Table 2，先归类为“上游未检出”，不得只改normalizer假称解决。T16在该页/区域使用第二通道。M2验收可先用确定性人工结构夹具验证领域层；完整页8端到端验收要等待T16拿到真实结构。

**命令：** `.venv\Scripts\python.exe -m pytest tests/unit/test_activity_tables.py tests/unit/test_mineru_adapter.py -q`。

## 11. T10：专利序列清单跨页、页眉干扰与编号引用

**修改：** `extract/sequences/st25.py`、`references.py`、`variants.py`、`validation/sequences.py`、`workflow/handlers.py`。
**新增：** `tests/fixtures/domain/st25_cross_page.json`、`st25_nucleic_long_line.json`。

- [ ] ST25状态机按已确认reading order/块角色feed，携带每行block ID、page、bbox/offset，而不是只有text+page。
- [ ] 当前 `_looks_like_residue_line` 对token长度>8直接停止，会把60字符连续DNA行判成prose；改成molecule-aware：DNA/RNA检查对应字母与位置数，protein三字母检查tokens，one-letter按明确类型处理。
- [ ] 页眉、页脚、专利号、打印页码过滤基于role+位置，过滤不关闭当前residue state；正文非法行不自动丢弃后继续拼接，应生成AMBIGUOUS_SEQUENCE_CONTINUATION。
- [ ] `<400>`记录SEQ ID，残基续页逐段积累evidence fragments；不能只保存第一次页。相邻记录切换由`<210>`/结构规则决定，不把下一个序列吞入当前序列。
- [ ] `<211>`长度、位置编号、残基总数做交叉检查；未知残基占位保留。闭合却无残基生成unresolved reference/diagnostic，避免构造空NonEmptyStr导致全任务失败。
- [ ] 数字只有符合listing行号布局与递增位置规则时才视为position label；序列区域里的0/1不能统一当编号删除。OCR将O/I识成0/1时保留未解析位置及裁图，不通过删除数字凑齐长度。
- [ ] `<220>/<221>/<222>/<223>`按feature key/location/text保留，避免将化学修饰降成普通单字母，明示坐标和文字来源。
- [ ] 不按包含<210>的整页跳过inline抽取；按已消费的listing block/region排除，页内其他真实片段仍可提。
- [ ] references支持`SEQ ID NO: 7`、`SEQ ID NO. 7`、`SEQ ID NOs 7–9`及中文格式，但范围展开需有界（默认max100）；每个ref带真实span，而不是无页全局ID。
- [ ] 同document同seq_id_no多份listing发生字符差异时保留冲突与source，不任取第一份。编号只在所属专利/附件关系里解析，不跨专利合并。
- [ ] variants记录W317A等显式变体文本；没有参考序列与坐标定义时保持未解析，不默认按肽局部编号317实施替换。

**验收：** 60字符DNA行与2页续段完整保留；页眉US号不终止；declared length不匹配阻断接受；结束无残基不导致整个job失败；跨页fragment高亮可逐段切换；10残基短肽与ST25并存不互相排除。

**命令：** `.venv\Scripts\python.exe -m pytest tests/unit/test_st25.py tests/unit/test_sequence_references.py -q`。

## 12. T11：实体、实验方法和关系进入正式工作流

**修改：** `extract/relations.py`、`extract/activities/assays.py`、`workflow/handlers.py`、`contracts/records.py`、`export/json_export.py`、`application/export_service.py`。
**新增：** `extract/entities.py`、`extract/assay_linker.py`、`tests/unit/test_entities.py`、`tests/unit/test_assay_linker.py`。

- [ ] 从表格行/列头、明确“peptide A / antibody X / compound 3”提entity mention，再按同文档明示别名建立实体对象；entity_id采用文档namespace+稳定来源hash，原文名称存entity_raw/name_raw。
- [ ] `TAT-RasGAP317-326`和W317A变体分开，正文明确别名可链接；相似字符串不能自动把不同抗体/重轻链合并。
- [ ] AssayExtractor从methods、caption、example提方法名、target/strain/cell line、dose/time/temp/pH等；每个条件字段必须指回原文。不要将全篇method合成一个assay覆盖所有实验。
- [ ] AssayLinker优先explicit figure/table/example ID、明确实体+方法语境；多个可能实验时assay_id保持null并列候选，不取最近方法段。
- [ ] 主流程真实传entities/assays/references到关系函数，删除空列表占位；导出entities、assays、relations、unresolved_references及对应evidence。
- [ ] 抗体配对仍要求显式链映射证据；不能把所有VH×VL作笛卡尔积。未见序列清单时保留引用缺失，不让模型补序列。
- [ ] 关系ID和relation evidence来自真实mention/chain assignment；不得自造“rel:chain”但证据index不存在。
- [ ] 生物measurement需要的entity/target/assay是否必填由metric定义决定，不能一刀切。例如percent与药敏分类单位可不适用，但必须有指标和对象；酶kcat需要s^-1及酶/实验语境。

**验收：** 两个肽两种条件结果不串；同药不同strain不合并；entity_id全部可在entities解析；assay_id非空全部可在assays解析；每条关系的两端及证据存在；未知对象不造链接。

**命令：** `.venv\Scripts\python.exe -m pytest tests/unit/test_relations.py tests/unit/test_entities.py tests/unit/test_assay_linker.py -q`。

## 13. T12：校验、人工修正、审核门槛形成闭环

**修改：** `validation/sequences.py`、`activities.py`、`evidence.py`、`acceptance.py`、`review/plan.py`、`application/review_service.py`、`api/routes/reviews.py`、`api/contracts.py`。
**测试：** `tests/unit/test_validation.py`、`tests/unit/test_acceptance.py`、`tests/application/test_review_service.py`。

- [ ] 校验输入包含entities、assays、metric registry、geometry和typed evidence index。`entity_id`非空不等于linked pass，必须解析到实体且同document；单位非空不等于合法单位。
- [ ] 依据metric设置单位/靶标/实体是否required。S/R/I分类和比例单位无需物理浓度，记录not_applicable；MIC数值单位缺失不能标完整通过。
- [ ] 增加ACT_METRIC_SUPPORTED、ACT_VALUE_UNIT_COMPATIBLE、ACT_ASSAY_LINK、ACT_FIELD_EVIDENCE、SEQ_CONTEXT_EXPLICIT、SEQ_SOURCE_COMPLETENESS、SEQ_POSITION_ORDER和SOURCE_CONFLICT checks。
- [ ] evidence字段全部校验，不能只检查record.evidence_ids。missing/跨文档/超边界fail；page-only坐标精度unknown；有些字段unit/assay证据来自caption/方法，应检查关联范围。
- [ ] auto_accept继续默认false。verified_human/reference也不能越过新产生的fail；decide_acceptance先判断fail/conflict，再看verified状态，防止旧确认状态掩盖新冲突。
- [ ] 审核reason_codes采用固定代码+可读details，不能把自然语言原因当优先级代码。队列按字符冲突、值/单位冲突、表格结构/语义、对象/实验缺失排序；相同问题可表级分组，记录仍各有决策历史。
- [ ] typed correction扩展：measurement可改metric/entity/target/assay引用、unit_raw、value及字段证据选择；unit_normalized由服务端重新算。sequence可改normalized_sequence及残基注释，通过显式position edits记录每个变更，raw_sequence和原解析evidence不覆盖。
- [ ] confirm/correct验证selected evidence IDs确属当前baseline、同文档、有可加载来源；修订值必须由所选字段证据支持。审查记录保存selected field evidence、actor、reason、base_revision与diff。
- [ ] 修正因OCR错误时，原始raw不能假装已经变正确：新增human_transcription（文本、image/page/bbox证据、actor、reason），新record引用该修订证据，原始机器证据仍保留。若无文本证据，不能走当前“texts为空就return”路径直接接受任意raw。
- [ ] 序列未解析位置必须由人工位置修正/外部已核验listing解决；不能以点击确认绕过。缺declared length但明确短片段可以接受“片段”身份，不能升为完整蛋白。
- [ ] API response返回record_revision、result_revision、current_record、blockers、allowed_actions；records接口为每条record提供_revision，禁止前端猜头版本。
- [ ] correction schema中的ActivityValue对operator/range/plus_minus/categorical各分支执行结构校验，structured字段须与raw的可解析表达一致；不能raw=">128"却number="12.8"、operator="="而通过。
- [ ] 审核修正仅对发布基线写新不可变record version；失败事务不写decision/record/head，拒绝保留历史。

**验收：** 任意raw/外文档evidence拒绝；非法finite/混合value字段拒绝；缺metric的“value”不因手动confirm变实测；正确受证据支持的修正可接受；两窗口r1只一个成功，第二个409且版本未被覆盖；同job恢复不重开已审核项。

## 14. T13：快照、导出与源文件I/O

**修改：** `application/export_service.py`、`api/routes/exports.py`、`api/routes/results.py`、`api/routes/documents.py`、`storage/repository.py`、`export/json_export.py`、`csv_export.py`。
**新增：** `application/result_snapshot.py`、`tests/api/test_document_ranges.py`。

- [ ] 把records与export读取统一到ResultSnapshotService.capture(job_id)。同一SQLite read transaction内读取job result_revision、published baseline refs、所有heads及被引用的不可变versions；baseline文件由hash验证读取。避免只锁heads后事务外再读取“当前”内容。
- [ ] 导出请求capture一次并写不可变snapshot.json。export row登记这份快照的revision/hash/ref；后台build_from_snapshot(snapshot_ref)只消费已固定快照，不再次读current head。records接口返回与内容对应的revision。
- [ ] export目标路径含export_id，防止同revision不同accepted_only/format覆盖。例如exports/<export_id>/result.json和manifest.json。content metadata与manifest revision必须一致。
- [ ] export manifest counts在accepted_only筛选后计算，同时给source_counts及excluded_counts；默认accepted_only=true，诊断下载显式include_candidates并醒目标记状态。
- [ ] JSON输出entities/assays/relations/evidence与所有条件；CSV每观测一行，复杂conditions/field_evidence用JSON列或关联表导出，保持operator/range/uncertainty。
- [ ] FASTA仅输出可表示为标准残基串、已接受且无未解析位置的序列；修饰/D型/retro-inverso信息保存在manifest，明确FASTA不能完整表达化学结构。片段允许输出但header标fragment，不将其命名完整链。
- [ ] 上传改成临时文件分块写+流式hash，超限删除本次临时文件；校验成功后content hash文件入库。相同hash已存在复用；source store和外部sample不因GET检查失败而删除。
- [ ] Range读取使用文件seek+有限长度stream/FileResponse支持；suffix range bytes=-500取末500字节，open-ended合法，非法/多范围按明确policy处理416。现有suffix end计算为500是错误，不能只修内存而保留协议bug。
- [ ] 非Range直接FileResponse；get_document只读取已登记inspection或重新只读inspect，禁止 `_inspect_or_409` 删除已有源文件。

**竞态回归：** POST export固定r1 → barrier阻塞后台 →审核生成r2 →释放后台。导出row、snapshot、文件manifest均为r1，records当前r2。使用同步barrier确保竞态发生，不靠sleep偶然命中。

**Range验收：** 已知1000字节文件，bytes=100-199返回100字节、bytes=-100返回900..999、bytes=950-返回50字节、bytes=1000-返回416。读取内存不随全文件size增长。

**命令：** `.venv\Scripts\python.exe -m pytest tests/application/test_export_service.py tests/api/test_document_ranges.py tests/api/test_api_flow.py -q`。

## 15. T14：研发工作台从列表变成可核验的界面

**修改：** `web/src/features/workbench/WorkbenchPage.tsx`、`pdf/PdfViewer.tsx`、`reviews/ReviewPanel.tsx`、`api/client.ts`、`features/compare/ComparePage.tsx`。
**导出UI同步修改：** `features/workbench/ExportDialog.tsx`默认acceptedOnly=true，并基于同一records snapshot revision显示筛选后的预计数量，不能用job独立请求的revision搭配另一次records结果。
**新增：** `features/evidence/useEvidence.ts`、`EvidenceNavigator.tsx`、`features/tables/TableView.tsx`、`features/text/TextView.tsx`、`features/reviews/SequencePatchFields.tsx`、`MeasurementPatchFields.tsx`。

- [ ] 点击记录/字段先调用getEvidence(jobId,eid)，根据返回的page_number/bbox/fragments设置PDF target；删除pageOf(eid)。多个证据显示Evidence 1/N和来源引擎，逐段切换。
- [ ] bbox转换依据page geometry和PDF.js可见viewport；规范坐标已含旋转，不再次旋转。Canvas CSS尺寸与像素尺寸分离，DPR=2和zoom不偏移。
- [ ] 清理上一页renderTask并cancel，处理PDF.js rendering cancelled exception；切页/zoom时overlay只在目标页对应geometry渲染完成后显示，避免旧页位置暂时显示在新页。加载新PDF后释放旧loadingTask/pdfDoc。
- [ ] page-only显示“有页码，无区域坐标”，缺位置显示“缺少定位证据”，接口错误展示错误状态，不把null当第1页。
- [ ] URL读写page/tab/record/evidence/zoom，刷新恢复同字段证据。加载page_count前显示加载态，不显示“0页”作结果。
- [ ] 表格tab实际消费pages API tables并显示rowspan/colspan、caption、header chains；点击cell定位PDF，显示分类与结构问题。原文tab展示primary/native/layout/OCR来源切换及差异，不能继续是空占位文案。
- [ ] 分别显示RawMention/conditions与Measurement，unknown单位或metric可筛查；字段空缺显示缺失原因。不要把所有数字都称活性。
- [ ] 补sequence correction editor：残基原/新字符diff、位置、来源图像/文本、注释和reason；生成typed patch，不能复用目前仅value的表单。
- [ ] Measurement editor含metric/entity/target/assay/unit/operator以及scalar/range/plus_minus/categorical；比较全部结构字段，不能当前raw不变就把改number视为unchanged。切换record时重置draft或按record_id key remount。
- [ ] conflict发生后立即禁用提交，刷新records与review/head并显示新旧差异；人工再次确认后使用新record_revision。不使用Math.max(conflict.live,oldRevision)让旧draft无核对地重试。
- [ ] 已resolved review被另一窗口完成时，显示该决定并结束当前review上下文；需要修订已确认记录则创建绑定当前revision的新审核项，不复用resolved review_id。
- [ ] 从服务端blockers禁用confirm并显示原因；有未解析残基时指向修正/复解析动作。成功后刷新record、review count、job及export预览。
- [ ] parser compare实现同source SHA、page set、domain规则版本比较：only A/only B/field conflict/agreement，点击任一项定位双方证据。无gold只显示差异；有gold引用对应评测版本，不显示虚构准确率。
- [ ] 宽屏三栏可拖动/折叠；窄屏按PDF/结果/审核tab切换，避免375px挤三栏。以研发审核功能可用为准，不重做整套视觉设计。

**定位实现参考（client需先提供EvidenceData定义）：**

```typescript
async function locate(evidenceId: string) {
  const span = await getEvidence(jobId, evidenceId);
  const fragment = span.fragments[0];
  const page = fragment?.page_number ?? span.page_number;
  const bbox = fragment?.bbox ?? span.bbox;
  setTarget({ page, bbox });
  setSelectedEvidence(span);
}
```

竞态处理：使用AbortController或请求generation防止先点A后点B、A慢响应覆盖B位置。该片段是定位核心，挂入useEvidence时需加取消和错误状态。

**可核验的前端检查：**

1. 点第9页两段真实序列，各自高亮正确段落；120%/200%缩放及DPR=2仍贴合。
2. 合并表头任一活性字段能切换value/entity/unit/target证据。
3. r1→r2冲突后提交按钮禁用；重新加载并确认后才可提交。
4. 修改scalar number或operator、修改plus_minus uncertainty都会产生patch；切到另条记录不会带入旧值。
5. 从`?page=9&record=...&evidence=...`刷新仍显示9页；375px宽度能完成一次审核。

当前web没有测试runner。只为高风险审核/定位逻辑增加Vitest+Testing Library并设置`test` script：新增 `features/reviews/MeasurementPatchFields.test.tsx`、`ReviewConflict.test.tsx`、`features/evidence/useEvidence.test.tsx`。PDF实际绘制用上述浏览器检查，不能以mock PDF测试替代视觉定位。

**命令：** `npm --prefix web run test -- --run`（添加runner后）；`npm --prefix web run build`。

## 16. T15：评测修复与真实样例回归

**修改：** `evaluation/annotation.py`、`matching.py`、`metrics.py`、`report.py`、`annotations/v1/manifest.json`、`cli.py`。
**新增：** `evaluation/corpus.py`、`tests/unit/test_inline_matching.py`、`annotations/v2/manifest.json`、`annotations/v2/annotation_guidelines.md`。

- [ ] 保留v1标注与旧报告，建立v2规范。Annotation增加source_locator/evidence fragments、sequence scope/annotations、assay/conditions、stable entity名称引用；只有human_verified且status=verified进入正式分母。
- [ ] CorpusScope明确哪些doc/pages/regions已被人工穷尽标注，并标scope_verified。正式FP/FN只在这些范围计算；范围外候选报告unannotated coverage，不能当FP也不能当TP。空gold若范围已被人确认“没有序列”才是负例基准；manifest完全未标注则not_evaluated。
- [ ] 修复序列匹配忽略seq_id_no=None：listing按文档+listing namespace+SEQ ID匹配；inline按人工标注源位置/实体/片段范围建立候选边，同文档内一对一匹配，然后比较完整序列和注释。
- [ ] 全部系统输出的序列候选都进入FP计数，不能跳过author/drug。已拒绝候选也分别报告candidate指标；只看accepted指标会掩盖审核噪声。
- [ ] 同编号不同内容计exact FN+FP，同时记录detected=true和字符错误；匹配优先选择完全匹配的候选，重复额外候选仍FP。
- [ ] measurement按entity_raw/canonical entity、target/strain、metric、assay/condition组、完整value及unit比较。T11将entity_id改opaque ID后matching必须lookup实体名称，不与gold.entity_raw直接比较ID。
- [ ] 当前measurement wrong_value被从missed/FP排除，修成：exact_tp=full_tuple_correct；exact_fn=total_gold-exact_tp；exact_fp=total_candidates-exact_tp；wrong_value仅是诊断子集，不能减少错误分母。
- [ ] 输出precision/recall/F1、序列exact-match rate/CER、数值与单位/对象/靶标/实验条件字段正确率、表格结构coverage、证据定位coverage、待审数量/人工修改量、accepted错误率。
- [ ] 数值gold优先人工记录operator/number/lower/upper/uncertainty，不只用被评测的同一parse_activity_value函数解析gold，否则双方共享bug可看似一致。原始文字仍保留审计。
- [ ] 双方单位都无法normalize时不得因None==None判单位相同；unknown unit仅原始表达精确一致时可单独报告raw agreement，不能计normalized unit正确。酶/浓度/百分比单位按维度比较。
- [ ] 图中没有明确数字的曲线不由像素估算进入精确测量gold；可列图形信息未覆盖。论文补充材料缺失、专利附件缺失、不可读序列单独统计coverage gaps，不能只评有易读文本的页。
- [ ] 重新挑选标注页：论文至少2/7/8/9页和页1负例；专利选实际sequence listing、example activity、claims和复杂表页，而非manifest当前统一1/2/3。由内容发现候选页后人工核验。
- [ ] dev/holdout按专利family和论文/补充文件组划分，不能同家族不同语言版本分别落训练和留出；保留真实扫描/混合页分层。现有HTML伪PDF排除并记录。
- [ ] 新增CLI：`bio_pdf.cli evaluate --gold <manifest> --records <snapshot.json> --out <reportdir>`，输出machine-readable metrics.json和report.md；无verified gold输出not_evaluated，不是0%/100%。

**指标定义参考：**

```python
precision = safe_ratio(exact_tp, total_candidates)
recall = safe_ratio(exact_tp, total_gold)
exact_fp = total_candidates - exact_tp
exact_fn = total_gold - exact_tp
```

**强制回归：** 已穷尽人工标注的负例范围0 gold+12作者/药物candidate=>FP=12、precision=0、recall=None；未标注manifest=>not_evaluated；2段短序列gold+0candidate=>FN=2；同编号错误串=>exact FP/FN各1；一条value/unit正确但entity错=>FP/FN各1；一gold被重复两次=>TP=1、FP=1。

**验收产物：** 新任务sample_before_after.md至少含候选变化、被过滤原因、两段真实短序列是否找到、页8表是否检出/识别、字段定位截图、真实缺失；无人工gold只能称抽查/回归，不能报正式准确率。自动接受继续关闭，是否开启需另定最低precision及足够样本/置信区间。

**命令：** `.venv\Scripts\python.exe -m pytest tests/unit/test_matching.py tests/unit/test_inline_matching.py tests/unit/test_metrics.py -q`。

## 17. T16：有界区域复解析，解决页8上游漏表和扫描歧义

**修改：** `pdf/render.py`、`workflow/handlers.py`、`parsers/registry.py`、`parsers/mineru_adapter.py`、`docling_adapter.py`、`review/compare.py`。
**新增：** `review/reparse_plan.py`、`parsers/region_adapter.py`、`contracts/reparse.py`、`tests/unit/test_reparse_plan.py`。

- [ ] ReparsePlanner只基于具体问题发任务：table caption/行列排列明显但primary tables=0；sequence长度/字符冲突；坏unit字形；表格grid冲突。不能因为结果少就无限重跑整篇。
- [ ] 默认每region最多2个追加attempt、每job最多10个region，单attempt 120秒；预算计入config与manifest。CPU环境可配置更大timeout，保持有限。
- [ ] 有界RegionRequest包含source document/hash、original page、bbox、rotation、dpi、engine/profile、expected_kind和trigger codes。区域裁图保存EvidenceImage及transform_to_page；从局部结果映射回原页，不给裁图虚假的page1。
- [ ] 引擎只能消费PDF时，允许请求原页并按region匹配输出；若必须生成裁剪PDF/图像输入，显式保存新source映射、保持原page ID与变换。旋转表格尝试方向必须计预算。
- [ ] 页8第一策略：真实MinerU HTTP原页解析，与Docling主结果并列；未运行服务则返回SECONDARY_UNAVAILABLE。第二策略：人工选择Table 2区域+提高渲染清晰度后支持的region引擎。不得将手写表结构作为真实引擎结果。
- [ ] 启发式表格区域检测只能提供ROI候选：caption邻近文字网格、线段/列对齐；结果不直接转成已确认Table。适配器实际产出或人工修订必须有来源记录。
- [ ] 复解析结果均保留原/新raw、结构、字段差异。结构更完整可作为新候选primary，但存在字符冲突就需审核；两引擎一致只记cross_source_agreement，不设verified_reference。
- [ ] `review/compare.py`当前SequenceDiff.conflicts对长度不等时直接索引会越界，改成safe char access，插入/删除采用编辑距离对齐，以免把一次丢字符显示成全串替换。
- [ ] 可选LLM/VLM仅生成`entity span/metric span/role/table header mapping`建议，输入包含block/cell IDs，输出字段必须指已有span；不得输出无来源残基/数值。温度配置、prompt/model版本、请求/响应和预算进制品。
- [ ] sequence视觉模型若建议I/1更改，只进入suggested edits，必须与裁图/人工或正式listing核验，不能“更符合蛋白alphabet”就替换。
- [ ] 预算耗尽/引擎失败标REPARSE_EXHAUSTED/ENGINE_UNAVAILABLE并回审核；禁止另启不受控agent循环。

**验收：** 页8真实secondary若检出Table 2，进入T09药敏分类提取并定位值/药物/菌种；检不出报告仍缺表而非伪造通过。rotated ROI映射回原页正确；小预算不会无限调用；不等长序列diff不崩；模型无来源建议不能接受。

## 18. T17：接入序列清单附件，减少PDF字符识别依赖

这是后续增强，当前PDF-only修复完成后实施。

**新增：** `ingest/attachments.py`、`extract/sequences/st26.py`、`contracts/source_bundle.py`、`tests/fixtures/domain/st26_minimal.xml`、`tests/unit/test_st26.py`。
**修改：** `api/routes/documents.py`、`api/contracts.py`、`workflow/handlers.py`、`extract/sequences/references.py`。

[WIPO官方资料](https://www.wipo.int/en/web/standards/sequence/resources)说明ST.26的序列清单采用XML。本文不以公开年份推断所有专利格式，也不把Docling的USPTO XML支持等同ST.26生物序列清单解析。

- [ ] 支持用户同时上传main PDF + ST25 text/ST26 XML附件；main document_id仍代表主PDF，source_bundle_hash涵盖全部来源和关联。新附件创建新job，不改变旧job的source set。
- [ ] 来源关联包括publication/application标识、source URI/用户声明、附件hash和取得日期；XML中SEQ ID与PDF引用同号不够，必须确定是同专利listing。自动联网取附件作为独立后续任务，当前只支持显式上传。
- [ ] ST26按已pin的官方DTD/schema和sample实现adapter，禁用外部实体与网络DTD自动解析，保留SequenceData标识、长度、分子类型、residues及features/qualifiers。解析并不等于通过官方validator。
- [ ] XML证据用source artifact+XPath/element locator，page/bbox可空，location_precision=none但具有精确源locator；不能因为不是PDF页就认为无来源。证据校验对XML locator设置not_applicable geometry。
- [ ] 与PDF清单字符比较；关联与内容完全匹配可以标reference match候选，有官方附件来源/版本核验才可verified_reference；发生差异保留双方并审核。
- [ ] 修饰/非标准残基/unknown位置保留；XML缺长度或feature矛盾不补齐。分号/定位语义按schema处理，不自行转换化学结构。

**验收：** 正确XML输出seq、长度、feature与locator；另一专利相同SEQ ID不合并；错误XML/外部实体不触发网络；PDF/XML一残基冲突可见；没有PDF坐标但有XML来源不是EVIDENCE_MISSING。

## 19. 各任务通用执行方式与验收门槛

每个T任务按下列步骤推进，确保交付可审查：

- [ ] 读取列出的当前文件与现有测试，确认该问题仍存在，并记录基线SHA。
- [ ] 加入本任务列出的真实反例/输入输出夹具；先运行目标测试，确认能暴露原问题。
- [ ] 修改最小相关代码并更新类型/Schema/兼容器，产物写到独立新job/stage-run。
- [ ] 跑任务对应测试；变更跨流程后运行 `.venv\Scripts\python.exe -m pytest -o addopts='' -m 'not live' -q`；前端变更运行 `npm --prefix web run build`。
- [ ] 需要真实引擎的任务，先运行doctor/capabilities确认环境，随后真实样本执行；环境不可用说明缺口，不用mock通过替代真实解析结果。
- [ ] 写该任务报告：改动文件、行为前后、夹具结果、真实样例证据、未覆盖项。完成后提交相关文件，避免将用户数据/上传原件一起提交。

### 必须做的端到端验证

1. 同一论文同一页集合，在新job上复跑；作者/药物/菌种不进入序列候选，May不进入活性。
2. 第9页两段10残基片段进入候选并带来源；不自行产生完整retro-inverso合成肽序列。
3. 第7/9页普通表格有结构可看，但默认bioactivity视图不再产生Used/Notes/Animal/Score伪指标。
4. 第8页药敏表要么真实提取并正确关联药物/菌种/S/R/I，要么显示具体漏表问题及尝试记录。
5. 字段点击高亮真实区域；多页序列/合并表头/缩放旋转有实际核验。
6. 人工修改、双窗口冲突、重试恢复和导出revision竞态均通过回归；同一export内容可追溯至固定snapshot。
7. 报告candidate FP/FN、accepted数据、覆盖缺口和审核数量；没有verified gold就不声明准确率。

### 交付给执行模型的现成指令

```text
请在 E:/GoLandCode/Bio-patent-parser 按
docs/superpowers/plans/2026-10-14-bio-pdf-accuracy-optimization.md 执行。
先核对当前代码与报告，基于当前改动调整文件路径，避免覆盖已有工作。
执行M0，再执行M1；每个任务完成后报告改动文件、目标回归测试和真实样例结果。
数据使用 data/optimization-demo，保留旧data/demo与原上传PDF供对比。
先完成可复现的规则/证据/恢复修复，随后M2/M3，再做M4增强。
不要通过伪造表格、把未知字段写成默认值、删掉困难候选或mock引擎输出来宣称准确。
严格保留原始证据；没有人工gold时只报告抽查和差异。
每个任务独立提交；当前任务完成后继续下一任务，遇到真实环境缺口写入报告并完成不依赖它的任务。
```

以上指令授权的是本项目修改。云端传输文档、部署、批量下载附件、自动接受规则启用均不由此文档推断授权。
