# Biological PDF Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现可复核、可恢复的 Python 生物论文/专利 PDF Demo，提取序列与活性证据，建立真实评测，并保持后续 Go/TS 迁移边界。

**Architecture:** 普通 Python 状态机编排有界步骤，SQLite 保存运行/审核元信息，文件保存不可变证据与结果。原生 PDF、Docling 和 MinerU 通过适配器转换为统一 JSON，领域规则处理序列、活性和关系。业务代码不直接依赖上游解析器对象。

**Tech Stack:** Python 3.12、uv、Pydantic v2、Typer、sqlite3、pypdfium2、Docling、可选 MinerU V1、pytest；后期可选 FastAPI/HTTP LLM 适配器。

---

## 0. 执行模型先读这些文件

1. 设计基线：`E:\GoLandCode\Bio-patent-parser\docs\superpowers\specs\2026-10-13-bio-pdf-workflow-design.md`。
2. 本实施计划：当前文件。
3. 当前来源与样本清单：`research/corpus_inventory_2026-10-13.json`、`research/pdf_parser_comparison_2026-10-13.md`。
4. 当前工作记录：`task_plan.md`、`findings.md`、`progress.md`。

本轮交付只有方案。以下文件和命令都是执行模型需要实现的目标，不能把这些代码块当作已经存在/已运行的代码。测试示例是最小合同与真实回归要求；生产逻辑按设计算法实现，不能为过示例硬编码答案。任务中合法 Python Protocol 的省略体只用于接口声明，其他实现不得留下未实现分支。

用户已确定先做 Python Demo，再迭代效果。先执行第一期并报告结果；后续阶段按新任务继续。不要在第一期一次性生成完整前端、Agent、向量库、所有 OCR/VLM 模型，也不要自动修改外部上游源码。

### 当前环境事实

- 项目根目录是 `E:\GoLandCode\Bio-patent-parser`，PowerShell。
- `docling/` 是独立上游仓库，业务代码建立在根目录 `src/bio_pdf/`。根目录尚未初始化 Git。
- 7 个文件有 PDF 签名；两个 EP 样本与 `probe.pdf` 是 HTML。不得对它们做“解析成功”的报告。
- 依赖、模型、GPU 能力和实际页级样本质量尚未验证。
- Docling 参考提交为 `f0d90989b461656102da5d449db98e6be9285f93`，参考源码版本 2.138.0。

## 1. 执行规则与阶段依赖

每个任务按“明确预期行为 → 编写有意义回归 → 确认原缺失/缺陷 → 实现 → 定向验证 → 记录制品”执行。不是所有测试都需要模型；原生 PDF 测试、领域规则、合同、恢复和审核测试应离线可运行。

代码中可以把 SDK 类型限制在 adapters 内；领域模块只依赖 contracts。下面纯函数可供 CLI/API/未来 Go/TS 对照使用。命令中测试文件名是稳定验收入口，执行模型应创建这些路径。

| 里程碑 | 任务 | 可运行结果 |
|---|---|---|
| M1A | 0-5 | 项目骨架、合同、文件检查、持久化、原生证据、真实 Docling 小页解析 |
| M1B | 6-10 | 序列/活性规则、校验、可恢复 CLI、JSON/CSV/审核报告 |
| M1C | 11 | 人工标注与对照报告，完成首个 Demo |
| M2A | 12-13 | MinerU V1 对照、官方序列附件 |
| M2B | 14-15 | 第二路局部复核、受约束 LLM 候选 |
| M3 | 16 | 轻量审核 API/页面 |
| M4 | 17 | Go/TS 合同迁移验证 |

新增行为要完成定向测试，不反复运行无关的大型上游测试。只在真实集成需要时安装对应模型。每个任务可形成一个小提交；提交只包含业务源码、合同、夹具和文档，不提交 PDF、模型、密钥、SQLite DB、上游嵌套仓库。若当前执行环境不允许某项命令，记录原因，不声称通过。

## Task 0：初始化业务项目和可重复环境

**Create:** `pyproject.toml`、`.gitignore`、`README.md`、`src/bio_pdf/__init__.py`、`src/bio_pdf/cli.py`、`configs/demo.toml`、`tests/conftest.py`、`reports/environment.json`。

- [ ] 检查根目录是否已出现业务代码/AGENTS.md；若有，按当前事实调整，不覆盖用户修改。
- [ ] 读取 Python、Git、CPU/内存及可用 GPU 信息；无需为硬件查询安装驱动。记录实际信息与 unknown，不能假定 CUDA 可用。
- [ ] 创建独立 `.venv`，采用 Python 3.12；若本机无法获取 3.12，选解析器共同支持版本并记录变更原因。
- [ ] 初始化业务 Git 仓库（如果还没有），先写忽略规则再添加文件。必须忽略 `docling/`、`pdf/`、`.venv/`、`data/`、模型缓存、环境密钥文件。
- [ ] 建立 Typer CLI，先提供 `doctor` 和 `--help`；doctor 只探测，不隐式安装模型或开始推理。
- [ ] 采用独立可选 extras：core 不带 Docling/MinerU；docling extra 只在任务 5 安装，model/HTTP provider 后续按需。至少锁定首次可运行解析版本和模型版本。

初始 CLI 行为示例（写入 `src/bio_pdf/cli.py`，其余命令后续注册）：

```python
import importlib.util
import json
import platform

import typer

app = typer.Typer(no_args_is_help=True)

@app.command()
def doctor() -> None:
    result = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "modules": {
            name: importlib.util.find_spec(name) is not None
            for name in ("pydantic", "pypdfium2", "docling")
        },
        "model_readiness": "not_checked",
    }
    typer.echo(json.dumps(result, ensure_ascii=False))

if __name__ == "__main__":
    app()
```

- [ ] 注册 `[project.scripts] bio-pdf = "bio_pdf.cli:app"`，core 依赖包括 pydantic/typer/pypdfium2，dev 包括 pytest。执行 `uv sync` 后记录锁定版本。
- [ ] 运行 `uv run bio-pdf --help`、`uv run bio-pdf doctor`，预期退出 0，输出可机器读取且不误报模型已就绪。
- [ ] 在 progress.md 记录环境、文件变更和真实命令输出。

## Task 1：建立合同、状态枚举和 Schema 导出

**Create:** `src/bio_pdf/contracts/{common,document,records,job,export_schema}.py`、`tests/contracts/test_schema.py`、`tests/contracts/test_evidence_geometry.py`、`schemas/v1/`。

- [ ] 实现设计第 6 节列出的完整模型。额外字段默认禁止，避免拼错字段静默丢弃。跨语言 number/value 字段用字符串，页码从 1 开始。
- [ ] 定义 SequenceRecord 和 Measurement 的 validation_state 与 acceptance_state；默认 unchecked/candidate。
- [ ] 坐标验证拒绝 x0>x1、负页码、NaN；在知道页尺寸时拒绝越界。缺 bbox 与超出页边界是不同情况。
- [ ] 提供纯函数 `validate_bbox(bbox: tuple[float,float,float,float], *, width_pt: float, height_pt: float) -> None`，违约抛 ValueError。
- [ ] 验证 JSON 能完整往返保存 `>128`、`1.2 ± 0.1`、µ、未知位置及状态；检查 JSON Schema required、enum、nullable 与 additionalProperties。

明确回归代码：

```python
import pytest

from bio_pdf.contracts.common import validate_bbox

def test_bbox_does_not_allow_swapped_axes_or_out_of_page():
    validate_bbox((10, 20, 50, 80), width_pt=100, height_pt=100)
    with pytest.raises(ValueError):
        validate_bbox((50, 20, 10, 80), width_pt=100, height_pt=100)
    with pytest.raises(ValueError):
        validate_bbox((10, 20, 150, 80), width_pt=100, height_pt=100)

def test_bbox_rejects_nonfinite_coordinates():
    with pytest.raises(ValueError):
        validate_bbox((0, 0, float("nan"), 10), width_pt=100, height_pt=100)
```

- [ ] 运行 `uv run pytest tests/contracts -q`；确认模型与数据没有未定义字段/测试依赖。
- [ ] `export_schema.py` 给出确定性 JSON Schema 导出命令，排序输出；schema 版本与设计一致。
- [ ] 用设计的示例 JSON 建合同夹具，再补 range/categorical/跨页证据/缺 bbox 示例。

## Task 2：输入校验、文件清单和页选择

**Create:** `src/bio_pdf/ingest/inspector.py`、`tests/unit/test_inspector.py`、`tests/fixtures/pdfs/native_minimal.pdf`（可在测试时生成）、`src/bio_pdf/errors.py`。

接口：`inspect_pdf(path: Path) -> DocumentInspection`；Inspection 含 document_id、sha256、bytes、page_count、is_encrypted、pages。`WorkflowError.code` 使用设计的错误码。

- [ ] 第一个回归确保 HTML 即使扩展名为 .pdf 也被拒绝。

```python
import pytest

from bio_pdf.errors import WorkflowError
from bio_pdf.ingest.inspector import inspect_pdf

def test_html_disguised_as_pdf_is_rejected(tmp_path):
    source = tmp_path / "patent.pdf"
    source.write_text("<!DOCTYPE html><title>download page</title>", encoding="utf-8")
    with pytest.raises(WorkflowError) as caught:
        inspect_pdf(source)
    assert caught.value.code == "INPUT_NOT_PDF"
```

- [ ] 再覆盖：只有 PDF 签名但对象损坏报 INPUT_CORRUPT；需要密码的文件报 INPUT_ENCRYPTED；合法空白 PDF 可打开并有页记录；文件哈希按原字节计算。
- [ ] 实现 `parse_page_selection(selection: str, *, page_count: int) -> tuple[int,...]`，支持 `all` 与 `1-3,7`；去重排序；拒绝 0、负数、超范围、倒置范围。不在首期做任意复杂页表达式。
- [ ] inventory 命令不删除/重命名原文件；生成包含每文件签名、可读性、错误码、哈希的 reports/inventory.json。
- [ ] 运行 `uv run pytest tests/unit/test_inspector.py -q`；再运行真实目录 inventory，比较现有审计 7/3 签名分组。若有效签名 PDF 实际损坏，另报，不强行匹配 7 个可解析文件。

## Task 3：不可变制品、SQLite 仓库和缓存键

**Create:** `src/bio_pdf/storage/{artifacts,repository}.py`、`src/bio_pdf/storage/migrations/001.sql`、`tests/unit/test_artifacts.py`、`tests/unit/test_cache_key.py`、`tests/integration/test_repository.py`。

公共函数：

- `write_json_atomic(path: Path, payload: dict) -> ArtifactRef`，创建后返回路径/哈希。
- `read_verified_json(ref: ArtifactRef) -> dict`，哈希不符报 ARTIFACT_CORRUPT。
- `make_cache_key(payload: dict) -> str`，SHA-256 canonical JSON。
- Repository：create_job、start_stage、complete_stage、fail_stage、get_job、find_successful_stage、append_review_decision。

- [ ] 缓存回归必须证明改模型或页范围会失效、JSON key 顺序不会使同配置失效。

```python
from bio_pdf.storage.artifacts import make_cache_key

def test_cache_separates_model_and_source_pages():
    base = {
        "source_sha256": "a" * 64,
        "pages": [1, 2],
        "engine": "docling",
        "model_revision": "r1",
        "options": {"ocr": False},
        "adapter_version": "1",
        "upstream_sha256": "b" * 64,
    }
    assert make_cache_key(base) != make_cache_key({**base, "model_revision": "r2"})
    assert make_cache_key(base) != make_cache_key({**base, "pages": [2, 3]})
    assert make_cache_key(base) == make_cache_key(dict(reversed(list(base.items()))))
```

- [ ] 原子写失败测试应在 rename 前模拟失败，证明旧制品未被破坏，stage 未变 succeeded。制品 immutable：同路径不同 hash 不覆盖。
- [ ] 完成制品后 crash、DB 未提交的恢复测试识别孤立文件，不把它直接当成功；验证 manifest 后可复用或重开 attempt。
- [ ] SQL 外键开启、短事务、busy_timeout；主进程单写；连接本地盘可用 WAL。迁移脚本重复运行安全。
- [ ] 运行 `uv run pytest tests/unit/test_artifacts.py tests/unit/test_cache_key.py tests/integration/test_repository.py -q`。

## Task 4：原生字符、几何、渲染和质量信号

**Create:** `src/bio_pdf/pdf/{native,geometry,render,quality}.py`、`src/bio_pdf/parsers/native_adapter.py`、`tests/unit/test_quality.py`、`tests/unit/test_geometry.py`、`tests/integration/test_native_pdf.py`。

公共函数：

- `inspect_page_signals(page_payload: dict) -> PageQuality`
- `classify_page(signals: PageQuality, config: QualityConfig) -> RouteDecision`
- `normalize_pdfium_bbox(box, *, page_transform) -> bbox`
- `read_native_pages(path: Path, pages: tuple[int,...]) -> NormalizedDocument`
- `render_evidence_crop(path, page_number, bbox, dpi, output_path) -> EvidenceImage`

- [ ] 在 test_native_pdf.py 用 ReportLab 开发依赖生成两页测试 PDF：第一页左右栏各有一条不同序列，第二页有 `IC50 ≤ 1.2 nM` 及单独行数字 `1 5 10`。所有是本地构造夹具，不列入真实文献成绩。
- [ ] 确认原生抽取没有全局删除数字/合法字母，左右栏不交错拼成一条序列；保存 glyph 原文可回看。
- [ ] 几何夹具覆盖 PDF bottom-left→top-left、90°/180°旋转、非零 CropBox 偏移、区域 PNG 放大映射、选择源第 7 页后仍 page_number=7。
- [ ] 同一 OCR 图片旋转前后都能把坐标回映到原 PDF；缺坐标标 unknown，不能靠估算生成精确 bbox。
- [ ] 质量回归：图版占 90% 但有可靠标题文字不直接断言扫描；字符多但乱码率高需 text_suspect；纯图像页标 scan_suspect；空白页独立标 blank；未知矢量文字不误当空白。
- [ ] 用图片并集覆盖率；重叠图片不会产生 >1 的最终覆盖率。区域像素预算超限时失败或有记录地分片，不能 OOM 后无限重试。
- [ ] PDF 句柄进程内串行，Windows spawn 子进程独立打开文件；不要把底层 PDF 对象放入进程队列。
- [ ] 运行 `uv run pytest tests/unit/test_quality.py tests/unit/test_geometry.py tests/integration/test_native_pdf.py -q`。

## Task 5：Docling 真实适配器和规范化

**Create:** `src/bio_pdf/parsers/{base,docling_adapter,normalize_docling}.py`、`tests/contracts/test_docling_normalizer.py`、`tests/integration/test_docling_live.py`、`tests/fixtures/providers/docling/`。

- [ ] 从实际 SDK 导出至少一个小 PDF 的完整 JSON，脱敏保存可复核 provider fixture；fixture 标注来源版本和页范围，不能凭印象编造 SDK 字段。
- [ ] Normalizer 保留文本块、原文、table cells、rowspan/colspan、每项 provenance；表格解析有缺失字段时记录降级能力。
- [ ] 回归：bottom-left bbox 必须转换；选第 7-8 页保持源页号；空页保留；缺表格坐标不伪造；partial ConversionResult 不变 completed。
- [ ] 初始化独立 Docling profile。以下是核对当前源码后可作为接入起点的调用示例，执行前确认安装版本与 API 一致：

```python
from pathlib import Path

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import (
    OcrMode,
    PdfPipelineOptions,
    TableStructureOptions,
    TesseractCliOcrOptions,
)
from docling.document_converter import DocumentConverter, PdfFormatOption

def make_converter(*, ocr: bool) -> DocumentConverter:
    options = PdfPipelineOptions()
    options.do_ocr = ocr
    options.do_table_structure = True
    options.force_backend_text = not ocr
    options.table_structure_options = TableStructureOptions(do_cell_matching=True)
    if ocr:
        options.ocr_options = TesseractCliOcrOptions(
            mode=OcrMode.FULL_PAGE, lang=["eng", "chi_sim"], scale=300 / 72
        )
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )

def convert_range(source: Path, output: Path, start: int, end: int, *, ocr: bool):
    converter = make_converter(ocr=ocr)
    result = converter.convert(source, page_range=(start, end), raises_on_error=False)
    result.document.save_as_json(output)
    return result
```

该示例的 OCR profile 需要 Tesseract CLI 和语言包；不是要求所有机器必须选 Tesseract。机器缺少它就选择已验证的 OCR backend 并记录变更；不得 silent fallback。关闭 OCR 仍可能需要 layout/table 模型，doctor 应明确显示模型需求。

- [ ] 能按离散页集合工作：先划连续窗口；保留源页映射；不要抽出几页另存 PDF 后忘记 remap。
- [ ] 解析在独立子进程，超时产生明确 stage error；保存原始响应、模型/参数与规范化结果。
- [ ] 运行 offline normalizer 合同测试。然后明确开启 live integration：用论文前 1-3 页真实解析，产出 raw.json、normalized.json、manifest、证据图；记录耗时而不宣称准确率。
- [ ] `uv run pytest tests/contracts/test_docling_normalizer.py -q` 默认离线；live 用 `BIO_PDF_LIVE_TESTS=1` 标记并有清晰 skip 原因。未运行时记录 integration_not_run。

## Task 6：ST.25、正文序列和引用抽取

**Create:** `src/bio_pdf/extract/sequences/{amino_acids,st25,inline,references,variants}.py`、`tests/unit/test_st25.py`、`tests/unit/test_sequence_candidates.py`、`tests/unit/test_sequence_references.py`。

纯函数合同：

- `parse_st25_text(text: str, *, document_id: str, page_number: int=1) -> list[SequenceRecord]`，便于单页规则测试；实际多页用 `parse_st25_blocks(blocks, document_id)` 保留 EvidenceSpan。
- `extract_sequence_references(text: str, *, document_id: str) -> list[SequenceReference]`。
- `extract_inline_sequences(blocks, document_id) -> list[SequenceRecord]`。
- `extract_variant_relations(blocks, entities) -> list[SequenceRelation]`。

最小黄金回归：

```python
from bio_pdf.extract.sequences.st25 import parse_st25_text

def test_three_letter_sequence_keeps_unknown_residue_and_length():
    text = "\n".join([
        "<210> 7", "<211> 5", "<212> PRT", "<213> Artificial Sequence",
        "<400> 7", "Met Lys Thr Xaa Gly", "1 5",
    ])
    records = parse_st25_text(text, document_id="sha256:fixture")
    assert len(records) == 1
    record = records[0]
    assert record.seq_id_no == "7"
    assert record.normalized_sequence == "MKTXG"
    assert record.declared_length == 5
    assert record.observed_length == 5
    assert record.acceptance_state != "accepted"
```

- [ ] 使用状态机读取标签：<210> 开始、<211>/<212>/<213>/<220> 元信息、<400> 残基区、新 <210> 结束上一序列。不得 regex 一次贪婪吃掉所有序列。
- [ ] 三字母转一字母映射保存位置；Xaa→X 且保留 unknown 注释；未知三字母词保留 unresolved，不能删词后装作长度正确。
- [ ] 覆盖核酸 acgt nry 模糊字符、RNA U、普通 protein U/O、终端修饰、位置数字移除、跨页续段、新 SEQ ID、未完末尾。
- [ ] 正文中的 SEQ ID NO:7、SEQ ID NOs:7-9 只生成引用。`at least 80% identity` 和没有明确上下文的英文标题不得变成完整序列。
- [ ] A23G 等变体只有参考序列与编号体系明确时才重建，并记录派生来源；不确定就保存关系候选。
- [ ] 运行 `uv run pytest tests/unit/test_st25.py tests/unit/test_sequence_candidates.py tests/unit/test_sequence_references.py -q`；在中文专利中人工选序列表页建立少量真实样本，并保留原文截图。

## Task 7：数值语义、单位与活性表格候选

**Create:** `src/bio_pdf/extract/activities/{values,tables,prose,assays}.py`、`tests/unit/test_activity_values.py`、`tests/unit/test_activity_tables.py`、`tests/fixtures/domain/activity_table.json`。

纯函数合同：`parse_activity_value(raw: str) -> ActivityValue`；`normalize_unit(raw: str) -> str|None`；`extract_table_measurements(table, *, document_id, evidences) -> list[Measurement]`。

实际回归：

```python
from bio_pdf.extract.activities.values import parse_activity_value

def test_comparator_interval_and_missing_value_are_distinct():
    greater = parse_activity_value(">128")
    assert greater.kind == "scalar"
    assert greater.operator == ">"
    assert greater.number == "128"
    interval = parse_activity_value("0.5–1.0")
    assert interval.kind == "range"
    assert interval.lower == "0.5"
    assert interval.upper == "1.0"
    missing = parse_activity_value("ND")
    assert missing.kind == "categorical"
    assert missing.number is None
    assert missing.qualifier == "ND"
```

- [ ] values tokenizer 先识别 NA/ND/未测等 categorical，再比较符、范围、±、科学计数法；不认识的保留 unparsed/raw。
- [ ] Decimal 保持小数与指数语义，输出十进制字符串；不得直接 `float()` 后造成截断/精度错误。
- [ ] normalize_unit 只做明确等价书写；未知单位保留原文并报告。测定温度 37°C、浓度配方和结果 IC50 不能混抽。
- [ ] activity_table.json 写一个 3 行 3 列夹具：第一行 `Peptide | MIC (µg/mL), E. coli | MIC (µg/mL), S. aureus`；第二行 `A | >128 | 2`；第三行 `B | 4 | ND`。为四个结果单元格、行标签和两列标题分别给 evidence_id。
- [ ] 预期 4 条测量，含 B/S.aureus 的 categorical ND；每条 entity/target/单位对应正确表头和行标签；ND 不变 0。
- [ ] 增加上级合并表头、多单位、重复表头、跨页 continuation、不完整单位、正文 claim_threshold 夹具；跨页拼接证据不足时 needs_review。
- [ ] 运行 `uv run pytest tests/unit/test_activity_values.py tests/unit/test_activity_tables.py -q`。

## Task 8：实体、链配对、assay 和关系证据

**Create:** `src/bio_pdf/extract/relations.py`、`tests/unit/test_relations.py`、`tests/unit/test_evidence_kind.py`、`tests/fixtures/domain/antibody_pairs.json`。

接口：`link_explicit_relations(entities, sequences, measurements, evidence_spans) -> RelationResult`，Result 含 relations、unresolved references、review reasons，不覆盖原始候选。

- [ ] 夹具：Antibody A 对应 VH SEQ 1 / VL SEQ 2；Antibody B 对应 VH SEQ 3 / VL SEQ 4；表格 A=IC50 1.2 nM、B=IC50 3.4 nM。预期两组链配对，无 A-VL4/B-VL2，无组合笛卡尔积。
- [ ] 缺实际 SEQ 3 时保留 B 的 unresolved reference，不能根据已知同类抗体补序列。
- [ ] 同一实体在两个 target 和两个 assay 里有结果，保留为 4 条不同测量；不按实体唯一去重。
- [ ] 对“antibody of claim 1 with IC50 less than 1 nM”标 claim_threshold；有具体实验表行+方法的结果才可以 measured；不明确的 undetermined。
- [ ] 同族文件 Seq 1 内容不同，命名空间阻止错误合并。
- [ ] 运行 `uv run pytest tests/unit/test_relations.py tests/unit/test_evidence_kind.py -q`。

## Task 9：检查规则、审核项与接受策略

**Create:** `src/bio_pdf/validation/{sequences,activities,evidence,acceptance}.py`、`src/bio_pdf/review/{plan,compare}.py`、`tests/unit/test_acceptance.py`、`tests/unit/test_validation.py`。

接口：`validate_sequence(record, evidence_index) -> ValidationResult`；`validate_measurement(record, evidence_index) -> ValidationResult`；`decide_acceptance(record, *, auto_accept: bool) -> AcceptanceDecision`；`build_review_plan(records, budget) -> list[ReviewItem]`。

规则不能只返回 bool，必须保留 pass/fail/unknown 和原因。校验长度、位置、字符、值语义、单位、实体/靶标关系、evidence exists 与 geometry。

```python
from bio_pdf.validation.acceptance import decide_acceptance

def test_length_and_alphabet_success_do_not_prove_sequence_truth():
    record = {
        "record_kind": "sequence",
        "validation_state": "checks_passed",
        "acceptance_state": "candidate",
        "is_complete": True,
        "checks": [{"check_id": "SEQ_DECLARED_LENGTH_MATCH", "outcome": "pass"}],
        "evidence_ids": ["ev1"],
    }
    decision = decide_acceptance(record, auto_accept=False)
    assert decision.acceptance_state == "needs_review"
```

这里 decide_acceptance 接收 records 的语言中立 dict 视图，返回 AcceptanceDecision；调用方先验证模型、传 model_dump()。不要直接接受 arbitrary 未校验 dict。

- [ ] 双路字符串长度相同但存在 MKT/MRT 的合法残基差异时标 SEQUENCE_CONFLICT；不按多数或“更像蛋白”自动改。
- [ ] 关键单位/evidence 缺失时不能 accepted。Reference/human 验证也不能掩盖当前修正后仍有未解决证据冲突。
- [ ] 每个 review item 绑定具体 record revision 和 evidence；排序优先字符/数值冲突，其后关系/条件缺失。
- [ ] 首版自动接受默认 false；后续启用必须有被评测的 policy version 和 calibration report。
- [ ] 运行 `uv run pytest tests/unit/test_acceptance.py tests/unit/test_validation.py -q`。

## Task 10：串起状态机、CLI、导出和审核修订

**Create:** `src/bio_pdf/workflow/{stages,runner,worker}.py`、`src/bio_pdf/export/{json_export,csv_export,review_html}.py`、`src/bio_pdf/review/decisions.py`；**Modify:** `cli.py`；**Test:** `tests/integration/test_workflow_resume.py`、`tests/integration/test_cli_demo.py`、`tests/unit/test_review_revision.py`。

- [ ] Runner 固定使用设计中的阶段顺序，StageHandler 不依赖 CLI/Typer；LLM 没有流程控制权限。
- [ ] 第一次以 native profile 和本地夹具跑完整任务，接着接真实 Docling，小页范围产生同样结果合同。
- [ ] 在 native_parse 已成功、layout_parse 未完成时模拟进程停止；恢复后核验制品哈希，native_parse 不重跑，layout_parse 重新 attempt；导出未标成功之前不能说任务完成。
- [ ] 记录独立 job/stage/attempt，有限 max attempts；重试耗时在清晰预算内；页面覆盖缺失写 errors/review，不能默认整本成功。
- [ ] 审核导出同时提供 result.json、sequences.csv、measurements.csv、review.html；HTML 转义原文本，不把文档标签当脚本执行；原文 PNG 与标框可定位。
- [ ] CSV 保留 raw、normalized、operator、unit、assay、entity 和状态，不能只输出一个 sequence 和一个 value 两列。
- [ ] `--accepted-only` 的结果来自明确 accepted 状态；若首版没有确认记录，可输出空 accepted 列表，但报告 candidates/review 数量，禁止称“没有序列”。
- [ ] 人工 correct/reject 操作追加 decision/revision，raw 证据不变；旧 base_revision 必须报 REVIEW_REVISION_CONFLICT。
- [ ] 默认 CLI 不需要 API key。引擎缺依赖与 native-only 未处理扫描页必须明确报出。
- [ ] 运行 `uv run pytest tests/integration/test_workflow_resume.py tests/integration/test_cli_demo.py tests/unit/test_review_revision.py -q`。
- [ ] 用真实论文 1-3 页和中文专利人工选出的序列表页跑 Demo；记录具体命令、job_id、制品目录、人工核对结果与已知错误。

## Task 11：人工标注、评测脚本和第一期交付

**Create:** `annotations/v1/{manifest.json,annotation_guidelines.md}`、`src/bio_pdf/evaluation/{annotation,matching,metrics,report}.py`、`tests/unit/test_metrics.py`、`tests/contracts/test_annotation_schema.py`。

字段规则与匹配键见设计第 11 节。benchmark 只计算人工确认的有效标注页面；不同任务模型生成的结果不能成为 gold。

- [ ] 标注流程先用渲染原文确认答案；模型可提出草稿但 human_verified 必须由人工实际确认。没有人工确认标为 draft 并排除正式准确率。
- [ ] 从当前样本选择约 30 页；若尚无真实扫描，报告 coverage gap，不把渲染文字 PDF 当真实扫描。分文档/同族 dev/holdout。
- [ ] 优先标序列完全一致、对应编号、活性字段和实体/靶标关系。不可读对象独立记录，不在正确率分母中偷偷删除。
- [ ] 序列长度一样但残基不同：序列 exact match=0；检测 recall 可为 1；字符错误率单独算。测量数字正确但实体串行：tuple correct=0。

```python
from bio_pdf.evaluation.metrics import safe_ratio

def test_zero_denominator_is_unknown_not_perfect_accuracy():
    assert safe_ratio(0, 0) is None
    assert safe_ratio(0, 2) == 0.0
    assert safe_ratio(1, 2) == 0.5
```

`safe_ratio(numerator: int, denominator: int) -> float|None` 是报告工具，不是核心正确性验收；真正指标测试还必须覆盖重复候选、漏检序列、错残基、串行关联和 `>128` 被改 128。

- [ ] 评测匹配避免一个 gold 被多个候选重复计 true positive；SEQ ID 相同但内容不符不可算 exact 正确；无 SEQ ID 使用预定义 evidence/实体匹配，不以模型语义自评。
- [ ] 输出 metrics.json、errors.jsonl、report.md：每指标分母、配置、有效页、失败页、待审核率、accepted coverage 与耗时。
- [ ] 仅约 30 页时不声称统计证明 99% 正确；auto_accept 仍 false。
- [ ] 运行 `uv run pytest tests/unit/test_metrics.py tests/contracts/test_annotation_schema.py -q`；实际 benchmark 没有 gold 时明确拒绝，不能自动造答案。
- [ ] 第一期报告必须含一条真实解析命令、对应制品、人工确认/未确认标记和当前不足。记录第一次 baseline，然后再迭代。

## Task 12：MinerU V1 接入与同输入对测（第二期）

**Create:** `src/bio_pdf/parsers/{mineru_v1_adapter,normalize_mineru}.py`、`tests/contracts/test_mineru_protocol.py`、`tests/integration/test_mineru_live.py`、`tests/fixtures/providers/mineru/`。

- [ ] 接入前锁定 MinerU 版本与模型；读取真实 `/v1/health`、capabilities 和 OpenAPI。当前官方 V1 SDK/HTTP与旧 root `/file_parse` 不能混用。
- [ ] 采用“上传→提交 job→轮询→下载”接口，保留 remote job_id/upload_id/file_id，服务状态 `completed/partial/failed/canceled` 与业务状态分别转换。SDK 同步包装只有证明满足恢复合同后才替代直接 HTTP。
- [ ] 严格核验返回 JSON 结构与 source page mapping。未知 schema/version 报 ENGINE_PROTOCOL_UNSUPPORTED；CLI `--json` 不能当 middle_json。
- [ ] 协议夹具至少 6 个场景：成功、缺 output file、partial、failed、客户端轮询超时后恢复、服务重启旧 job 404。
- [ ] 轮询超时只结束本地等待，不推断远端任务失败；下次 resume 继续相同 job_id。若服务重启使 ID 消失，记录 REMOTE_JOB_LOST，再按原文档/配置新建 attempt。MinerU V1 resource index 目前在内存，不承诺服务重启后 job 持久化。
- [ ] 只把 API key 附到同 origin 请求；预签名 upload URL/跨 origin redirect 不携带服务密钥。服务给出的 upload_headers 保留原意。
- [ ] 使用 capabilities 中实际支持的 structured_content/middle_json，保存原始 response，table/figure 依旧保持证据引用。
- [ ] 区别 producer.version 与 schema_version；source page_idx 0-based转换到业务 1-based；当前 provider 可能输出 points/pixels/归一化 bbox，按声明和页尺寸处理，不写死一个坐标缩放。
- [ ] 运行离线协议测试后，使用和 Docling 完全相同 PDF/页范围对测；记录规范化损失、模型运行时间和领域错误。
- [ ] 以 gold 的整序列和活性关联结果决定 primary；允许 MinerU 成为默认引擎，不能因之前克隆 Docling 就固定赢家。

依据：[官方 SDK](https://opendatalab.github.io/MinerU/usage/sdk_api/)、[V1 HTTP](https://opendatalab.github.io/MinerU/usage/http_api/)、[输出合同](https://opendatalab.github.io/MinerU/reference/output_files/)。

## Task 13：官方 ST.25 TXT / ST.26 XML 附件（第二期）

**Create:** `src/bio_pdf/ingest/attachments.py`、`src/bio_pdf/extract/sequences/st26.py`、`tests/unit/test_attachments.py`、`tests/unit/test_st26.py`、`tests/fixtures/sequences/`。

- [ ] 用户绑定 manifest 明确 document_id/publication/version/source_url/attachment hash；默认不在解析任务中联网自动搜集。
- [ ] ST.25 TXT 复用 tokenizer，原文定位为 byte/token locator，无 PDF bbox。
- [ ] ST.26 先下载并保存实际官方 XSD/schema 版本与最小合法夹具，在独立适配器中读取序列、length、molecule、feature/qualifier；未知 schema 版本报错，禁止套用 USPTO 文档 XML 后端。
- [ ] 原附件序列与 PDF 一致且上下文属于同一版本，记录 verified_reference；PDF 差一个合法残基时保留冲突和来源，不能无声替换原 OCR 串。
- [ ] 长度不符、修饰未知、同 seq_id 跨文档冲突、附件所属版本不明确均需要复核。
- [ ] 校验反映官方 schema，不能为了让 XML 通过而修改真实序列。
- [ ] 运行 `uv run pytest tests/unit/test_attachments.py tests/unit/test_st26.py -q`。

## Task 14：有限预算的第二路局部复核（第二期）

**Create:** `src/bio_pdf/review/reparse.py`、`tests/unit/test_reparse_budget.py`、`tests/integration/test_reparse_geometry.py`；**Modify:** workflow runner、review plan/compare。

- [ ] 将长度/字符/数字/单位/配对冲突转为 region/window 请求；每候选最多一次，整个 job 最多配置数量，budget exhausted 标记 unresolved。
- [ ] 序列裁剪保留位置号/SEQ ID 上下文，活性表格裁剪保留表头、行标签、单位和脚注；不能只截一小块数字就期望正确关联。
- [ ] 引擎若不支持 crop 就用源页窗口并记录实际处理页范围；不能把裁剪图坐标直接当原页坐标。
- [ ] 300/450 DPI参数预算可调，max_pixels 避免极大图；原始低分辨率截图不能通过放大宣称“恢复真值”。
- [ ] 不做逐字符多数投票；独立引擎也可能共享 OCR/VLM模型，记录 lineage 和相关错误风险。
- [ ] 剩余冲突保持原始多路候选并进入审核，不能用第二路直接覆盖第一路 raw。
- [ ] 运行 `uv run pytest tests/unit/test_reparse_budget.py tests/integration/test_reparse_geometry.py -q`，以及同一 holdout 页面增强前后评测。

## Task 15：可选 LLM 的结构化候选与字段证据（第二期）

**Create:** `src/bio_pdf/providers/{base,http_provider}.py`、`src/bio_pdf/providers/prompts/{assay_classification,relation_candidates}.txt`、`tests/unit/test_llm_evidence_gate.py`、`tests/fixtures/providers/llm/`。

- [ ] 明确接口：`ModelProvider.extract(request: ExtractionRequest) -> ExtractionResponse`；request 包含有限 evidence blocks、任务种类、JSON Schema、调用预算；response 包含字段候选、evidence refs、模型元信息、原响应 artifact ref。
- [ ] 先定义完整离线 fixture；默认 no keys/no network 可测试。远端调用由显式 llm enabled 与 allow_remote 控制，不自动上传 PDF。
- [ ] 提示词要求逐字段 evidence_id、原文短片段及位置；无法确定返回 null/needs_review；不重建不存在的序列或数值。
- [ ] 接受门前必须核对 evidence 引用存在、原文足以支持字段、数值/比较符/单位由确定性规则复查。合法 JSON 不代表事实正确。
- [ ] 回归：模型输出不存在 evidence_id → EVIDENCE_MISSING；已存在证据但原文没有输出残基/数字 → needs_review；模型建议把 MKTXG 改 MKTAG → 保留 unknown，拒绝无依据补齐。
- [ ] 客户端 429/超时有限重试，JSON格式最多一次纠正；缓存含 model/prompt/schema版本；token/费用不可得时 null。
- [ ] 同 holdout 比较无 LLM 与有 LLM 的实体/条件/tuple准确率，不能把模型自评写入 gold。
- [ ] 运行 `uv run pytest tests/unit/test_llm_evidence_gate.py -q`。

## Task 16：轻量审核 API 和页面（第三期）

**Create:** `src/bio_pdf/api/{app,routes}.py`、`src/bio_pdf/api/static/`、`tests/integration/test_api_jobs.py`、`tests/integration/test_review_api.py`。

- [ ] 实现设计第 10 节 API；POST jobs 返回 202，不在 web request 中阻塞解析。
- [ ] API/CLI 共用 Repository 和应用服务，不重复实现序列规则。
- [ ] 审核页展示原文图片/框、raw/normalized、表格行列、理由、候选值、对应 entity/assay 与来源链。
- [ ] correct/reject 使用 base_revision，追加审计历史；旧 revision 报 409。确认人标識由本地配置/用户输入，不能由模型伪造。
- [ ] 下载 accepted_only/result revision明确；任务完成与待审核数量同时展示。
- [ ] 上传保存到受控目录，限制大小，真实文件识别沿用 inspector；无效 HTML 返回可解释错误。
- [ ] 使用轻量原生 HTML/JS或明确需要时再选前端框架；不让 UI 工作取代质量评测。
- [ ] 运行 `uv run pytest tests/integration/test_api_jobs.py tests/integration/test_review_api.py -q`。

## Task 17：Go/TS 迁移合同（第四期）

**Create:** `tests/contracts/cross_language/`、`reports/migration_comparison.md`；新语言目录在迁移时确定，不在 Python Demo 阶段生成空项目。

- [ ] 先冻结 JSON Schema、错误码、状态语义、十进制字符串、页坐标与 Unicode offsets，记录 contract revision。
- [ ] 将业务 API/任务/存储/导出迁移，再逐个迁移纯规则；原生模型服务按现有 HTTP 接口继续使用。
- [ ] 使用完全相同黄金输入，比较业务语义，不比较 UUID/时间戳；hash canonicalization 要先做跨语言向量测试。
- [ ] 覆盖 UTF-8/UTF-16/rune 差异、≤/µ/中文、range、±、categorical、修饰、错误复核、review revision与resume。
- [ ] 一次只做语言迁移或规则改动，不同时更换引擎/模型造成回归归因不清。
- [ ] 迁移后重新跑同 holdout，指标无下降且coverage相同才视为迁移行为一致；解析器服务的 Python 运行环境仍列入部署清单。

## 2. 第一轮真实验收操作顺序

1. doctor 与 inventory，先证明文件/环境能被正确识别。
2. 原生文字 PDF 小页范围，观察字符、编号与坐标是否保留。
3. Docling 同范围，观察表格、块与证据。
4. 选出序列表和活性表格页，人工确认小批对象。
5. 跑候选提取/校验/导出，不确定结果展示待审核。
6. 加人工标注并输出 baseline；确认有哪些漏检、残基错位、单位或实体串行。
7. 按错误类型加第二路和领域规则，再比较相同 holdout。

每轮只修复一类问题，优先序列字符错/完整性，再活性行列与单位，再复杂关系。不要一开始全篇输入 LLM 后凭“看起来合理”验收。

## 3. 执行完成报告模板

```text
完成里程碑：M1A / M1B / M1C
依赖与模型：实际版本、模型 revision、硬件
已实现命令：明确列出
定向测试：实际命令、通过数、失败/skip原因
真实样本：文件哈希、页范围、job_id、制品路径
质量结果：标注状态、分母、exact/tuple precision与recall、待复核比例
已知错误：具体原文页/框、候选及影响
未实现范围：对应第二/三期任务编号
下一步：最有价值的错误类型或接入任务
```

若没有实际运行模型，不得写“解析正常”；若没有人工 gold，不得写“准确率 99%”；若仅规则通过，不得写“所有序列正确”。

## 4. 本计划自检要求

- 架构、接口、页码和状态名与设计文档一致。
- 所有示例的 schema version、value字段、SEQ ID 字符串类型与合同一致。
- 真实样本和构造夹具明确区分；没有伪造 golden truth。
- 缺依赖/模型/设备可以定位到具体阶段，不用吞异常或静默降级。
- 首期不创建后期空模块、不修改上游、不需要 LangChain 账户或云 tracing。
- 交付 Demo 的同时交付 evidence、review和baseline，不只交一个 Markdown 文件。
