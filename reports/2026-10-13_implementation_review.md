# 生物论文与专利 PDF 工作流实现审查

日期：2026-10-13。审查基于业务仓库 HEAD `2207fe2` 和当前工作目录中的文件。

## 结论

**架构方向符合原方案，当前实现尚未满足首期 Demo 的验收要求。**

已具备结果合同、解析适配器、序列与活性规则、SQLite 存储、状态机和评测模块，也有真实解析器运行制品。主要缺口集中在完整流程接线、解析输出与证据保真、领域规则对复杂输入的处理，以及评测正确性。

| 里程碑 | 当前判断 | 原因 |
|---|---|---|
| M1A：输入检查、原生解析、Docling、统一制品 | 部分实现 | 核心源码未被 Git 收录；部分磁盘输出不符合统一合同；真实 Docling 坐标丢失 |
| M1B：序列/活性、校验、审核、导出 | 模块已实现，完整流程未完成 | CLI 未连接领域提取、校验、审核和状态机；校验结果无法直接写回记录合同 |
| M1C：人工标注与准确率基线 | 评测框架已实现，验收未完成 | 人工标注清单为空；活性匹配未比较值、比较符和单位，存在重复匹配 |

第二期的官方序列附件、局部第二路 OCR、LLM，以及第三期界面，不作为本次首期缺陷。MinerU 虽已提前接入，其当前输出不能用于同样本准确率比较。

## 审查范围与证据

- 对照 `docs/BIO_PDF_HANDOFF.md`、设计文档和 Tasks 0–11 执行计划。
- 阅读应用源码、现有测试、报告和已经保存的真实解析器响应。
- 运行现有离线测试：`python -m pytest -o addopts='' -m 'not live' -q`。
- 结果：**261 passed, 4 skipped**。没有新增或修改应用测试，没有调用远程模型或解析服务。
- 用当前 HEAD 的 Git 归档模拟干净检出，检查源码能否导入。
- 使用合成 PDF 和离线输入复现 CLI、状态机、领域规则与评测问题。
- 对真实 Docling/MinerU 原始制品重新执行归一化检查。

复现脚本和结果保存在忽略目录 `data/review_2026-10-13/`，核心领域与解析复现脚本为 `reproduce_review.py`，结果为 `domain_and_parser_reproductions.json`。这些是本次审查临时制品，不是项目正式验收测试。

本次没有修改应用源码，也没有提交代码。测试通过只能证明现有测试覆盖的输入通过，不能证明真实 PDF 提取准确率达标。

## 发现的问题

### 1. [P1] 核心 PDF 源码被忽略，干净检出不能导入

位置：`.gitignore:52`，规则 `pdf/`。

该规则会匹配任意层级的同名目录，`src/bio_pdf/pdf/` 中的 `native.py`、`render.py`、`geometry.py`、`quality.py` 等文件没有进入 Git。

证据：

- `git ls-files src/bio_pdf/pdf` 返回空。
- `git check-ignore -v src/bio_pdf/pdf/native.py` 指向这条规则。
- 将 HEAD 通过 `git archive` 展开后，归档内没有该包；导入报 `ModuleNotFoundError: No module named 'bio_pdf.pdf'`。

影响：本地源码和提交的代码不一致，换机器或交给另一个模型从提交启动会失败。

修复：将样本目录规则锚定为 `/pdf/`，检查类似的 `/docling/` 规则，并收录业务源码。验收时应从实际提交的干净检出运行。

### 2. [P1] 用户入口没有执行完整的领域工作流

位置：`src/bio_pdf/cli.py:162`；`src/bio_pdf/workflow/runner.py:94`。

CLI `parse` 调用解析适配器后直接打印制品路径，没有继续调用序列提取、活性提取、关系构建、校验、审核队列及领域导出。源码中没有把这些业务阶段注册到 `WorkflowRunner` 的完整生产接线。

证据：包含 ST.25 蛋白序列和 `IC50 = 1.2 nM` 的合成 PDF 可以成功完成 native parse，但输出只有 `manifest.json`、`normalized.json` 和 `raw_response.json`，没有序列、测量或审核记录。当前 CLI 只有 `doctor`、`inventory`、`parse`。

影响：模块单测通过后，用户仍不能通过正常入口得到方案要求的生物领域结果；批处理、恢复、审核、导出与 benchmark 入口也未完整交付。

修复：实现明确的生产阶段注册及配置加载，用一个入口执行源文件检查到领域导出的全过程；解析制品缓存和失败恢复应走同一流程。

### 3. [P1] 磁盘 normalized.json 与统一结果合同不一致

位置：`src/bio_pdf/parsers/docling_child.py:46`、`src/bio_pdf/parsers/docling_adapter.py:192`、`src/bio_pdf/parsers/mineru_adapter.py:288`。

Docling 子进程把上游 Docling JSON 写到名为 `normalized.json` 的路径；父进程归一化后只在内存中返回，没有将 `NormalizedDocument` 写回。MinerU 也先保存投影后的 Docling 风格结构，再在内存归一化。

证据：使用真实响应模拟 SDK 路径，磁盘文件按 `NormalizedDocument` 验证出现 16 个错误；现有 MinerU 的磁盘 `normalized.json` 出现 107 个合同验证错误。部分报告脚本另外写出的归一化文件不能证明适配器自身保存正确。

影响：内存运行、CLI 文件输出和断点恢复读到的对象可能不同；跨语言消费者不能依赖同一合同。

修复：将上游响应保存为独立 raw 制品，所有适配器统一序列化并验证 `NormalizedDocument` 后再返回 `normalized_path`。

### 4. [P1] MinerU 允许把另一个 PDF 的结果关联到本地文件身份和截图

位置：`scripts/run_mineru_live.py:112`、`src/bio_pdf/parsers/mineru_adapter.py:251`。

运行脚本允许同时传入本地文件和远程 URL。本地文件用于 document_id、哈希与截图，适配器只在没有 URL 时上传本地文件，因此解析内容可能来自完全不同的 PDF。

证据：`reports/2026-10-13_mineru_paper1.md` 中 document_id 使用本地生物论文哈希，但 source_url 是 MinerU 官方 demo PDF。保存的响应是题为 “The response of flow duration curves to afforestation” 的水文论文，共 13 页，与本地生物论文不是同一文档。

影响：文本、表格、文档哈希和证据截图会被错误关联。这份运行报告不能作为本地论文的解析结果，也不能参与 Docling/MinerU 同样本比较。

修复：只提交实际被标识的源文件；若支持 URL，先取得并校验实际源文件字节，再从同一文件计算身份和截图。对混合来源明确拒绝或完成哈希一致性验证。

### 5. [P1] MinerU 的真实 HTML 表格被静默丢弃

位置：`src/bio_pdf/parsers/mineru_adapter.py:649`。

`content_list` 投影只读取 `text` 或 `content`，实际表格主要在 `table_body` HTML 中。没有这两个文本字段时直接跳过，表格、标题和脚注没有进入统一结果。

证据：现有原始响应含 5 个 HTML 表格，归一化后表格及 table block 均为 0，未报告这一信息损失，能力清单仍表示支持表格。

影响：依赖表格的活性测量和实验条件无法提取，结果却呈现为正常成功。

修复：解析 HTML 表格的单元格、行列、合并关系、标题、脚注与页位置；暂不支持的结构必须留下原始证据并报告损失。

### 6. [P1] 真实 Docling 文本框坐标在归一化时丢失

位置：`src/bio_pdf/parsers/normalize_docling.py:397`。

真实 Docling 坐标位于 `prov[].bbox`，当前文本归一化读取顶层 `bbox`。表格 provenance 同样需要完整转换，不能只保留页码。

证据：现有 SDK 响应产生 50 个块、serve 响应产生 638 个块，归一化后所有文本块 bbox 都为空，errors 也为空；原始响应包含 provenance 坐标。

影响：无法稳定定位序列和测量原文、生成对应局部截图或执行局部 OCR；“保留原文证据”的核心要求没有满足。

修复：读取所有 provenance span，保留多页/多框信息；将 Docling 的坐标原点转换到统一约定，并验证页面尺寸、旋转与坐标范围。

### 7. [P1] 活性评测会将错误值计为正确，并重复使用同一候选

位置：`src/bio_pdf/evaluation/matching.py:136`。

匹配只使用 document/entity/target/metric，命中后直接增加 correct，没有比较数值、比较符、单位和 assay。候选匹配后也未从可匹配集合中移除。

证据：

- Gold 为 `MIC >128 ug/mL`，候选为 `MIC 0.001 nM`，统计仍为 correct=1、missed=0、false_positives=0。
- 两条具有相同键的 gold 可以共用一条候选，得到 correct=2。

影响：最重要的活性提取错误会被评测隐藏，基线和回归结果不可信。

修复：定义完整活性元组匹配，包括数值类型、数值或区间、比较符、单位、实体、靶标和实验条件；明确允许的等价转换，并采用一对一匹配。分别报告字段级与整条记录指标。

### 8. [P1] 校验模块输出与领域记录合同类型冲突

位置：`src/bio_pdf/validation/sequences.py:23`、`src/bio_pdf/contracts/records.py:100`；活性和证据校验存在相同模式。

校验返回的 checks 中 `details` 是字符串，领域记录的 `CheckResult.details` 要求 `dict[str, str]`。

证据：将 `validate_sequence()` 结果写回 `SequenceRecord` 并按合同重新验证，出现 4 个 details 类型错误。绕过验证的赋值不能解决导出和重载问题。

影响：领域阶段接入后，正常记录无法完成严格的校验写回与存储。

修复：各模块复用同一 `CheckResult` 合同，先构造合法的结构化检查，再更新记录；导出和数据库重载都应能通过验证。

### 9. [P1] 序列归一化静默删除未知位置

位置：`src/bio_pdf/extract/sequences/st25.py:224`，蛋白 fallback 也有类似字符过滤。

只保留 `isalpha()` 字符会删除未知符号或无法识别的位置，随后长度可能碰巧与声明长度相同。

证据：ST.25 输入声明长度 4、原始序列 `AC?GT`，输出归一化 `ACGT`，observed_length=4、is_complete=true，校验结果为 checks_passed。

影响：错误残基位置丢失，而且被报告为完整序列，违反“不静默改残基”的设计约束。规则通过仍不是人工接受，但错误状态会污染后续审核和统计。

修复：将无法解析的位置明确表示为未知或 unresolved；保留原始字符与位置，触发冲突或待审核，禁止以过滤后的长度证明完整。

### 10. [P1] 正文活性规则将区间或均值误差截成另一标量

位置：`src/bio_pdf/extract/activities/prose.py:19`。

当前正则匹配单个数字加单位，能够在复杂表达式末尾找到看似合法的后缀。

证据：

- `The IC50 was 1-10 nM.` 被提为 scalar 10 nM。
- `IC50 was 1.2 ± 0.3 nM.` 被提为 scalar 0.3 nM。

影响：直接改变测量意义和大小；后续单位/范围校验通常无法发现这种错误。另有微符号和百分比标点的识别边界需要补齐。

修复：完整解析比较符、区间、均值与误差表达式，保留 raw_value 和类型。无法支持时应输出 unresolved 并要求审核，不能从后缀生成确定标量。

### 11. [P2] 活性表格只读取单行表头，合并表头语义错位

位置：`src/bio_pdf/extract/activities/tables.py:99`。

当前没有根据 rowspan/colspan 构建完整表头链。

证据：两层表头 `Peptide | MIC (ug/mL)`，第二层为 `E. coli | S. aureus`，数据行为 `A | 8 | 16` 时，第一条缺靶标，第二条变成 metric=value、unit=None、target=None。

影响：同一活性表格内的第二个实验结果丢失指标与单位，靶标无法正确关联。

修复：展开合并表头，将指标、单位、靶标和条件作为各数据列的完整上下文；不能确认关联时保留歧义和证据。

### 12. [P2] 请求页范围未兑现，原始页码可能被重新编号

位置：`src/bio_pdf/parsers/docling_serve_adapter.py:178`、`src/bio_pdf/parsers/mineru_adapter.py:649`、`src/bio_pdf/parsers/normalize_docling.py:307`。

serve 请求没有发送页范围，也未过滤最终结果；MinerU 的 content_list 分支忽略请求页。部分归一化逻辑按数组序号重新编号，丢失原始页码。

证据：serve 请求 [1,2] 返回 12 页；MinerU 请求第 1 页返回 13 页。稀疏输入的原始 page_idx=6 在相关路径被归一化为第 1 页。

影响：小页解析的成本、结果数量和证据页码不符合请求；评测切片也可能错位。

修复：适配器明确声明是否支持服务端页范围；保留源页码，必要时在本地严格过滤，并在 manifest 记录实际提交、处理和输出的页集合。

### 13. [P2] 状态机过早报告成功，恢复不核验缓存制品

位置：`src/bio_pdf/workflow/runner.py:94`、`:102`、`:122`、`:152`。

未注册的阶段直接跳过，最后仍返回 succeeded；阶段的 review_required 没有传到总结果；恢复只检查数据库中的成功状态，没有核验文件存在性与哈希。

证据：

- 没有任何 handler 时，运行结果仍为 succeeded，stages_run=[]。
- handler 返回 review_required=3，运行结果 review_count=0、status=succeeded。
- 首次产出 evidence.json 后修改其内容，resume 仍跳过该阶段，保留损坏文件并返回成功。

影响：缺失阶段、需要人工审核和损坏证据均可能表现为成功，恢复结果不可依赖。

修复：检查必要阶段注册情况，聚合审核与警告状态；恢复前核验制品合同、存在性和哈希，失效时重新执行依赖阶段，并返回真实结果引用。

## 首期验收仍缺少的内容

`annotations/v1/manifest.json` 当前 `annotations: []`，状态为 `pending_human_annotation`。因此目前没有人工 gold、真实准确率基线，尤其不能确认扫描件效果。

首期应至少补齐：

1. 用户可运行的完整命令：输入 PDF → 原生/Docling → 序列/活性 → 校验 → 待审核记录 → 导出。
2. 同一 PDF 的源文件、document_id、解析内容、截图和证据坐标一致。
3. 统一输出合同可从磁盘和 SQLite 重载，且关键原文/表格信息不被静默丢弃。
4. 人工标注包含序列逐字符真值、完整活性元组和证据位置，覆盖原生论文、专利及真实扫描件。
5. 评测采用一对一匹配，能区分错误值、错误单位、范围、比较符、未知残基和错误实体关系。
6. 报告区分规则通过、待人工审核与已接受，不把解析成功或单测通过表述为准确率达标。

## 建议修复顺序

1. **先让交付可运行**：修复 Git 源码收录，接通生产工作流，统一校验合同与磁盘序列化。
2. **再保证来源与证据可靠**：修复 MinerU 源文件身份、Docling 坐标、MinerU 表格和原始页码；失去信息必须报告。
3. **处理领域高风险错误**：未知残基、区间和均值误差、合并表头与上下文关联。
4. **修正状态与评测**：必要阶段、待审核状态、恢复哈希验证、一对一完整活性匹配。
5. **最后建立真实 baseline**：先完成少量有人工 gold 的真实文件，再扩大样本和比较解析器。现有错误来源的 MinerU 报告应注明失效并重新运行同一文件。

不建议在这些问题修复前扩展 UI、替换工作流框架或重构为 Go/TS。当前主要困难是输出保真和验收闭环，而框架选择无法解决这些具体错误。
