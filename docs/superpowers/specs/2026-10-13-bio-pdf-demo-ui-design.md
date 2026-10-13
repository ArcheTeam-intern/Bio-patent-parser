# 生物 PDF 解析 Demo：前端与审核工作台设计

日期：2026-10-13。

状态：供用户审阅的设计稿。本轮交付设计与页面草图，不实施应用代码。

目标用户：**研发调试与人工审核**，已由用户确认。

## 1. 目标与成功标准

做一个能实际操作的 PDF 解析 Demo：上传生物论文或专利，运行解析，查看序列和活性候选，点击候选定位原文，人工核对并修正，导出带状态与证据的结果。

页面应帮助用户回答：

1. 这份 PDF 哪些页走原生文本，哪些页需要 OCR，路由理由是什么？
2. 序列、SEQ ID、活性数值和实验条件从哪里提取？
3. 有哪些未知残基、断页、区间、合并表头或关系歧义？
4. 规则检查通过的记录有哪些，哪些已经由人确认？
5. 换解析器或规则后，同一输入的结果发生了哪些变化？

首个真实闭环的验收路径：上传 PDF → 创建任务 → 观察真实阶段状态 → 取得领域结果 → 点击证据 → 修正一条记录 → 刷新仍可见 → 导出反映修正的新版本。

不以候选数量、任务成功或页面美观证明准确率。没有人工 gold 时，界面显示“尚无准确率基线”。

## 2. 现有基础与必须处理的问题

复用当前 Python 的 `contracts/`、`workflow/`、`storage/`、解析器和领域规则。新增 API 与前端，业务规则只在后端实现。

先阅读 `reports/2026-10-13_implementation_review.md`。前端依赖的阻塞项：

| 审查问题 | 对页面的影响 | 必须完成的处理 |
|---|---|---|
| PDF 源码被忽略 | 干净检出无法启动 | 修正根目录忽略规则并收录源码 |
| 完整阶段未接通 | 页面拿不到序列/活性/审核结果 | CLI 和 API 共用完整应用服务 |
| normalized.json 合同不一致 | 前端、恢复和导出内容不同 | 同一严格合同保存、返回、重载 |
| 来源身份不一致 | 原文和候选可能来自不同 PDF | 单一源文件哈希贯穿解析、截图和结果 |
| Docling bbox / MinerU 表格丢失 | 无法核对位置或查看活动表格 | 保留 provenance、表格结构与损失警告 |
| 校验 checks 类型冲突 | 审核结果无法写回 | 统一 CheckResult |
| 未知字符和复杂数值处理错误 | 可能展示错误的确定结果 | 保留未知位置、区间和误差类型 |
| 状态和缓存核验不完整 | 假成功、假恢复 | 必要阶段校验、待审核计数、制品哈希验证 |
| 评测匹配错误、gold 为空 | 展示虚高准确率 | 完整元组一对一匹配并人工标注 |

前端静态布局可以与这些修复并行推进；真实演示和验收必须经过修复。不得把 mock 数据默认展示为真实解析结果。

## 3. 方案比较与选择

| 方式 | 优点 | 限制 | 本项目判断 |
|---|---|---|---|
| React + TypeScript + FastAPI | PDF 证据交互可控、前后端边界清楚、以后替换业务后端方便 | 需要实现 HTTP 契约和前端工程 | **推荐** |
| Streamlit / Gradio | 单 Python 环境、快速展示模型输入输出 | 复杂 PDF 高亮、版本审核和多视图状态需要额外定制 | 可用于临时解析器试验页 |
| 改造 WeKnora UI | 已有知识库和文档界面可参考 | 项目目标与本项目的残基审核、活性字段证据不同，裁剪面较大 | 不作为首版底座 |

这张表是基于本项目需求的工程判断，不代表解析质量排名。

选择独立 React 前端。保留 Python 后端时，首版部署为 Python 服务 + 静态前端文件；若以后业务后端换 Go/TS，保持 HTTP/OpenAPI 合同即可复用前端。解析引擎是否仍需 Python 是另一条迁移边界。

## 4. 技术栈与运行方式

| 部分 | 选择 | 职责 |
|---|---|---|
| 前端 | React + TypeScript + Vite | 页面、组件、构建 |
| 路由 | React Router 声明式路由 | 任务、结果、审核、对比视图 |
| 服务端状态 | TanStack Query | 请求缓存、状态轮询、审核后的定向失效 |
| 样式 | 项目 CSS tokens + 小组件 | 克制的科研工具外观，避免重型主题系统 |
| PDF 预览 | PDF.js（pdfjs-dist） | 原 PDF 渲染、缩放、旋转、文字层与定位 |
| API | FastAPI + Pydantic | 上传、任务、结果、证据、审核和导出 |
| 执行 | 独立 Python worker + 现有 WorkflowRunner | 持久任务调度、重解析隔离 |
| 存储 | SQLite + 不可变文件制品 | 任务、审核历史、版本和源证据 |

Vite 官方提供 React/TypeScript 模板；React Router 和 TanStack Query 分别提供路由与服务端数据状态管理。实施时根据相容版本解析依赖并提交 lockfile，不在方案里臆定“最新版本”。PDF.js 主库和 worker 文件必须来自同一安装版本。

开发：Vite 页面通过 `/api` 代理到本地 API。演示部署：构建后的静态文件由 API 同源提供，API 前缀 `/api/v1`；前端路由 fallback 仅用于页面路径，不能吞掉 API 404。

首版绑定 localhost，单用户，不包含登录、租户或云发布。密钥由后端环境读取，浏览器只见配置是否可用。

工作流继续用普通 Python 状态机；不引入 LangChain/LangGraph。

## 5. 信息架构

四个主视图：

| 路由 | 页面 | 核心用途 |
|---|---|---|
| `/jobs` | 任务列表 | 上传、选择样本、启动和观察任务 |
| `/jobs/:jobId/workbench` | 解析工作台 | 原 PDF 与领域结果、证据联动 |
| `/reviews` | 审核队列 | 按风险、文档和记录类型处理问题 |
| `/compare` | 解析器对比 | 同文档同页的结果差异；有 gold 才显示准确率 |

任务详情以 `job_id` 为单位，文档身份以源字节 SHA-256 为单位。一个文档可以有多个任务/解析器/配置版本。

工作台 URL 查询参数保存可分享的视图状态：`tab=sequences|activities|tables|text|run`、`record=<id>`、`page=<源页码>`、`revision=<结果版本>`。刷新恢复所选内容，不能依赖组件内存。

全局主导航保持紧凑。日志、配置与版本信息放在“运行详情”中，不占据主结果区域。

## 6. 页面 A：任务列表与上传

顶部标题“生物 PDF 解析”，副标题“序列、活性与原文证据”。主动作“添加 PDF”。

内容：

- 拖放上传或文件选择；支持一次选择多份文件，每份独立任务。
- “本地样本”从后端受控样本清单选择，前端不能输入任意服务器路径。
- 表格列：文件名、文档类型、页数、解析配置、当前阶段、处理状态、待审核数、创建时间。
- 行动作：打开工作台；失败任务查看错误并重试。待审核不是失败状态。
- 空状态展示上传入口和三种样本类别：论文、专利、扫描件。未存在的类别标“样本待补充”。

上传后先做字节签名和完整性检查。扩展名为 PDF 的 HTML 必须拒绝；加密、损坏、超过大小限制的文件给出明确原因。

解析配置对话框：

1. 文档类型：自动 / 论文 / 专利（用户选择是提示，结果元数据保留该选择）。
2. 解析模式：原生快速 / 结构增强；结构增强选择可用 Docling SDK 或服务。MinerU 放在“对比实验”，可用性来自 capabilities。
3. 页范围：全部或 `1-3,7`，服务端校验去重与边界，使用源页码。
4. 目标：序列、活性，默认都开启；明确哪些任务只跑解析。
5. OCR 策略：自动 / 强制 / 禁用，只有对应引擎支持时可选；禁用且为扫描件时给出无法充分提取的提示。

首版上传上限建议 100 MiB，可配置；默认并发解析任务数为 1。不是质量承诺，设置值在运行详情中可查。

## 7. 页面 B：解析工作台

### 7.1 桌面主布局

```text
┌────────────────────────────────────────────────────────────────────┐
│ 文件名 / 文档类型   任务状态   解析配置   结果版本   导出              │
├────────────────────────────────────────────────────────────────────┤
│ 源文件检查 → 页分类 → 解析 → 归一化 → 领域提取 → 校验 → 导出           │
├───────────────────┬────────────────────────┬───────────────────────┤
│ 原始 PDF          │ 结构化结果             │ 记录详情与审核        │
│                   │ 序列 / 活性 / 表格     │                       │
│ 页码、缩放、旋转  │ / 原文 / 运行详情      │ 原始值与规范值        │
│                   │                        │ 字段证据、检查原因    │
│ PDF 页面+高亮     │ 候选列表、筛选         │ 修正表单、历史版本    │
│                   │                        │ 确认 / 修正 / 拒绝    │
└───────────────────┴────────────────────────┴───────────────────────┘
```

正式产品在宽度 ≥1440px 时采用约 45% / 31% / 24% 的三栏可拖动布局；≥1100px 时采用 PDF + 结果两栏，详情抽屉覆盖结果侧；窄屏保留结果列表和全屏 PDF/详情切换。研发主要场景是桌面，首版不追求手机上的三栏体验。

两种工作台模式：

- **证据工作台（默认）**：列表与原文并排，适合浏览和发现错误。
- **审核聚焦**：PDF 和当前记录详情两栏，通过上一条/下一条遍历审核队列，适合大量核对。

切换只是显示布局，选中记录和结果版本保持一致。

### 7.2 顶部与进度

顶部显示“处理完成 · 待审核 N 条”等真实状态。显示记录数量与证据缺失数量，不显示未经标定的综合质量分或准确率。

阶段条映射现有 STAGE_ORDER：ingest/inspect/native_parse/layout_parse/normalize/extract/validate/export。业务文案合并 ingest+inspect 为“源文件检查”，其余详细状态仍可展开。

每阶段显示待执行、处理中、完成、跳过或失败。只显示真实可测的进度，如已完成页数和当前页；引擎没有页进度时显示当前阶段与耗时，不模拟百分比。

失败后保留已成功制品，明确“部分结果，尚未完成”；不能把部分解析结果混作完整结果。断网时显示“连接中断，任务状态待同步”，不能直接将任务标失败。

### 7.3 原始 PDF 面板

- 原文件由 PDF.js 加载，源页码始终 1-based，不按选中页集合重编号。
- 按需渲染当前页及邻页；长文档不一次渲染全部页面。
- 工具：页码跳转、适合宽度、缩放、旋转、查看源文件。
- 缩略图导航可收起；扫描件也可预览，文字层为空时仍保留图像。
- 点击记录/字段证据后跳到对应页，绘制一个或多个证据框；只高亮当前选择。
- 点击证据框反向选中对应记录；一框关联多条记录时弹出小列表供选择。
- 多页序列显示“证据 1/N”，可在多个页位置间移动。
- bbox 缺失时只跳页并显示“只有页码，无法精确定位”；页码也缺失时显示“缺少位置证据”，禁止伪造高亮。

### 7.4 结构化结果：序列

列表列：SEQ ID、分子类型、声明/观察长度、完整性、检查状态、审核状态、源页。

详情：

- 原始序列与规范序列并排或上下显示，等宽字体；每 10 个残基分组，显示位置。
- 三字母转单字母结果保留转换信息；修饰/未知字符独立标注，不删去位置。
- 差异逐字符高亮；不是只比较长度。
- 声明长度、观察长度、跨页连续性、未识别字符、序列来源与证据。
- SEQ ID 引用没有解析到序列时作为“未解析引用”，不伪造 SequenceRecord。
- 关联抗体链或实体只展示有明确证据的关系，歧义展示“关联待确认”。
- 复制候选序列时提示其状态；FASTA 默认只导出已接受且完整、表达可确定的序列。

### 7.5 结构化结果：活性

列表列：实体、指标、原始数值/单位、靶标、实验类型、证据类型、审核状态、源页。

详情忠实展示 ActivityValue：

| kind | 展示 | 编辑字段 |
|---|---|---|
| scalar | `>128 µg/mL` | operator、number |
| range | `1–10 nM` | lower、upper；保留原始表达 |
| plus_minus | `1.2 ± 0.3 nM` | number、uncertainty |
| categorical | `inactive` | qualifier |
| unparsed | 原文 + “待解析” | 保留 raw，人工选择解析类型 |

所有数值使用十进制字符串；浏览器不转成 float 后再保存。原始单位与规范单位分别展示，不自动做依赖分子量的质量/摩尔单位换算。

字段旁边提供证据按钮：实体、数值、单位、指标、靶标、实验条件。点击单位时可高亮表头，点击数值时高亮数据格；不是只定位整张表。

证据类型用明确文字区分“实测”“权利要求阈值”“一般陈述”“引用结果”“未确定”。claim_threshold 不作为实测活性进入默认实测视图；用户可以切换查看。

### 7.6 表格与原文

表格视图保留行列、rowspan/colspan、层级表头、标题和脚注；缺失部分显示解析警告。选择单元格可定位 PDF，查看其关联测量。

原文视图按页/块显示原生文本与结构解析文本，可切换，不合并成一个不可追溯的 Markdown。首版以纯文本方式渲染外部字符串；表格从规范单元格构建，不直接插入上游 table_body HTML。

### 7.7 运行详情

提供引擎、模型/规则版本、配置哈希、源哈希、请求与实际页集合、阶段耗时、路由理由、warnings/errors、制品清单和日志尾部。raw JSON 可查看与下载，不能泄露凭据。

## 8. 页面 C：审核队列与审核语义

队列列：风险原因、记录类型、文档、源页、当前版本、状态。筛选：未审核 / 存在冲突 / 缺证据 / 长度异常 / 数值语义异常 / 关系歧义。

优先级按原因规则排列，不虚构概率置信度。点击一条进入工作台的“审核聚焦”模式。

### 8.1 三种动作

1. **确认**：人工核对当前记录。checks_passed 与 verified_human 分开；前端明确“确认后纳入已接受结果”。后端校验所引用证据属于同一文档与版本。
2. **修正并确认**：编辑结构化字段，预览差异，提交 correct。后端重跑校验；仍有未知残基、缺少必需证据或阻塞冲突时不允许直接接受，返回可解释的失败信息，保留草稿供继续修改。
3. **拒绝**：填原因，记录进入 rejected；保留历史、源候选和证据。

另提供“保留待审核”作为草稿/跳过操作，不产生确认决策。首版不允许“全部一键接受”。

没有准确位置但有可核对原文和页码时，可人工补选证据区域后确认。不得以人工点击按钮跳过未解决的字符/语义歧义；无法确定就保留待审核。

### 8.2 服务端原子提交

现有 `append_review_decision()` 不能直接作为 UI 审核服务，它只追加历史。需要 ReviewService 在一次事务中：

1. 检查 review_id、record_id、base_revision 和结果基线仍有效。
2. 校验字段白名单与值类型，保护 document_id、原始解析制品、原始候选快照和 EvidenceSpan.raw_text。当前修正记录的 value.raw 只能改为证据中实际存在的表达，原始候选保持不变。
3. 应用 patch 得到新记录，运行领域和证据检查。
4. 如果通过确认条件，更新 verified_human + accepted；拒绝更新 rejected。
5. 追加决定与新记录版本、更新当前指针、关闭对应 review item，增加 result_revision。
6. 保存导出快照的准备状态，返回新 revision、记录和审核计数。

版本文件先写入不可变路径并校验哈希，再在事务中注册并切换引用；事务失败留下的未引用文件可回收。不能先把旧文件覆盖再写数据库。

客户端在响应成功后更新缓存。409 冲突时显示旧值与当前值，要求重新核对；不覆盖其他页面的修改。不将无身份认证的本地 actor 名称包装为可验证的人员身份。

## 9. 页面 D：解析器与版本对比

对比单位：相同 document_id、相同请求源页集合、相同领域规则版本；否则只能展示运行信息，并提示不可直接比较。

操作：选择同文档的两个完成任务，查看文字块/表格/序列/活性差异与源页位置。并列呈现“仅 A”“仅 B”“字段不同”“一致”；一致不等于正确。

第一版至少完成候选差异和证据跳转。有人工 gold 且评测修复后才增加：序列精确匹配、残基错误、完整活性元组匹配、字段级错误和漏提。

没有 gold 时，不显示准确率百分比、雷达图或“最优引擎”结论。当前来源错误的 MinerU 运行不得出现在有效对比候选中。

## 10. 页面状态与空值约定

| 后端/数据状态 | 界面表达 |
|---|---|
| queued | 等待解析 |
| running | 正在执行某阶段 |
| succeeded | 处理完成；另显示当前审核统计 |
| succeeded_with_review | 处理完成 · 有记录待审核 |
| failed | 处理失败；可查看失败阶段与已有部分制品 |
| cancelled | 已取消；仅在后端取消能力真实实现后提供按钮 |
| checks_passed | 规则检查通过 |
| verified_reference | 已与指定参考来源核对 |
| verified_human | 人工核对通过 |
| candidate / needs_review | 候选 / 待审核 |
| accepted / rejected | 已接受 / 已拒绝 |
| null / 未提取 | 未提取；不显示 0 或空字符串冒充真实值 |

job.status 是处理状态快照，审核汇总是当前记录状态；审核结束不能悄悄改写已经保存的历史运行 manifest。

长序列允许选择复制但不截断导出；列表可摘要，详情完整可读。序列未知字符、数值比较符与单位属于关键内容，不能仅靠 hover 才出现。

## 11. API 契约

下面是新增 API 设计，不是当前已经存在的接口。

### 11.1 接口清单

统一前缀 `/api/v1`，成功响应含 api_version，结构化错误含 code/message/details/request_id。集合默认 limit=50、最大 200，使用稳定 cursor；结果检索明确 result_revision。

| 方法与路径 | 功能 |
|---|---|
| GET `/capabilities` | 引擎可用性、OCR/页范围能力、是否对外发送、schema/rule 版本、上传限制 |
| GET `/samples` | 受控本地样本目录清单与有效性 |
| POST `/documents` | multipart 单文件上传；保存源字节，返回 identity/inspection |
| POST `/documents/import-sample` | 以 sample_id 导入已列出的本地样本 |
| GET `/documents/{id}` | 文档元数据与真实页几何 |
| GET `/documents/{id}/source` | 原 PDF，支持 Range 与源哈希 ETag |
| POST `/jobs` | 选择文档、引擎、页范围和提取目标，202 入队 |
| GET `/jobs` | 任务列表及状态/文档筛选 |
| GET `/jobs/{id}` | 当前处理状态、阶段、审核统计、当前结果版本 |
| GET `/jobs/{id}/stages` | 阶段、尝试、耗时与错误 |
| POST `/jobs/{id}/resume` | 校验缓存后恢复同配置失败/中断任务；成功完整任务直接返回现状 |
| GET `/jobs/{id}/records` | 按 kind、接受状态、查询词和 revision 检索 |
| GET `/jobs/{id}/records/{recordId}` | 一条记录、revision 与 checks |
| GET `/jobs/{id}/evidence/{evidenceId}` | 原文、页码、bbox、来源与几何 metadata |
| GET `/jobs/{id}/pages/{pageNumber}` | 当前版本的文本块、表格、质量信号、证据关联 |
| GET `/jobs/{id}/artifacts` | 制品清单，按 ID 获取文件，不接受任意路径 |
| GET `/artifacts/{id}/content` | 校验并返回允许访问的制品 |
| GET `/reviews` | 可按 job、原因和状态筛选的审核队列 |
| GET `/reviews/{id}/decisions` | 审核历史 |
| POST `/reviews/{id}/decisions` | confirm/correct/reject 原子应用 |
| POST `/jobs/{id}/exports` | 生成指定 revision、格式和接受状态的导出，202 入队 |
| GET `/exports/{id}` | 导出状态、revision、记录数和下载制品 ID |
| POST `/comparisons` | 校验同源可比条件并生成差异制品，202 入队 |
| GET `/comparisons/{id}` | 对比状态、条件和差异结果 |

长时解析、导出和对比不阻塞 HTTP 请求，创建时先提交持久状态再返回。变更解析配置创建新 job；恢复不改变配置哈希。

### 11.2 创建任务示例

```json
{
  "document_id": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "parser": "docling_sdk",
  "page_numbers": [1, 2, 3],
  "targets": ["sequences", "activities"],
  "ocr_policy": "auto",
  "document_type_hint": "patent"
}
```

这里的哈希仅作合同示例，不指代真实样本。parser 是 API 层的稳定名称，映射到现有具体适配器；选项由 capabilities 驱动。

自动接受策略后端固定 false；请求不允许通过任意参数开启。配置必须严格白名单验证。

创建类 POST 支持 Idempotency-Key，重复键和同内容返回原资源，键相同但内容不同返回 409。禁止为方便演示给所有 POST 自动重试。

### 11.3 活性修正示例

针对将 `1–10 nM` 错当为 10 nM 的候选，API 支持 typed patch：

```json
{
  "action": "correct",
  "base_revision": 1,
  "actor": "local-reviewer",
  "patch": {
    "value": {
      "kind": "range",
      "operator": null,
      "number": null,
      "lower": "1",
      "upper": "10",
      "uncertainty": null,
      "qualifier": null,
      "raw": "1–10"
    }
  },
  "evidence_ids": ["ev-demo-value-1"],
  "reason": "原文给出区间，候选误取上界作为标量"
}
```

API patch 用领域专用 Pydantic 请求模型，并转换为审计存储结构。现有 ReviewDecision.patch_json 只支持平面标量字典，需要升级或引入新的版本化 typed patch 合同；不能用临时 JSON 字符串塞入旧字段绕过类型。示例中的 raw 只能与既有源证据相符，原始记录/raw 仍不可改写。

### 11.4 轮询与将来的事件流

首版使用 1.5 秒轮询活动 job；终态停止，窗口不可见或连续失败时降低频率。轮询返回版本与可用制品，不返回整份 PDF 或全部日志。

审核提交后只失效对应记录、审核计数、队列和导出列表。PDF 源文件不重新加载。

SSE 留作后续能力。需要持久 event_id、Last-Event-ID 恢复、心跳与重新拉取状态策略时再实现；首版不为“实时”引入 WebSocket。

## 12. 后端应用服务与 worker

调用链：React → API → 应用服务 → SQLite 入队 → worker → WorkflowRunner → 解析/提取/校验 → 不可变制品与当前结果指针。

新增 `application/` 层供 CLI/API 复用，避免前端通过 subprocess 拼接 CLI 输出或直接读取目录推断结果。

首版 worker 单独进程，并发 1；API 只做轻量请求。FastAPI 官方也提醒重型计算需要考虑独立执行方案；本项目已有隔离解析基础，延续它即可，不必首版新增 Redis/Celery。

worker 通过 SQLite 的短事务认领任务，使用 worker_id、lease_until、heartbeat_at、generation。重启后过期租约可恢复；旧 generation 的结果不得切换当前引用。认领和长时间模型执行不能放在同一个数据库事务。

每进程打开自己的 SQLite 连接；API 不能向 Windows 子进程传递连接/PDF handle/模型对象。日志和状态落库；重启后不是依赖内存队列恢复。

新增表或明确迁移：record_versions（job、类型、ID、版本、结果制品引用）、record_heads、result_revisions、worker_leases、exports、comparisons、idempotency_requests；重用现有 documents/jobs/stage_runs/review_items/review_decisions/artifacts。采用 schema migration version，不依赖 CREATE IF NOT EXISTS 来修改旧表字段。

## 13. PDF 坐标与证据契约

现有 BoundingBox 约定 points、左下原点；PageGeometry 描述可见页。该约定必须贯穿，前端不能默认 bbox 是图片像素或左上坐标。

增加明确的坐标 metadata：

- canonical_frame = `visible_page_pt_bottom_left`。
- canonical width/height 与当前 PageGeometry 一致，裁切和源旋转已处理。
- source PDF page 的 CropBox、MediaBox、原始旋转和 user_unit。
- `canonical_to_pdf_user` 六元素仿射变换，用于把规范框转换回 PDF.js 所需的源 PDF user space。
- 每证据的 parser_run_id、原始 provider locator、表格/单元格 ID；多框证据通过多个 span 表达。

前端先将 bbox 四个角应用 canonical_to_pdf_user，再经 PDF.js 当前 PageViewport 变换为 CSS 坐标，取范围绘制 overlay。Canvas 物理像素按 devicePixelRatio 放大，但 overlay 使用 CSS 像素。不得把源 rotation 和用户主动旋转重复应用。

用户框选证据时执行逆变换并在后端检查页边界。截图若作为辅助制品，应保存从截图像素到 canonical page 的逆变换。缺少有效几何时返回 null/警告，页面降级，不按 612×792 猜测。

验收必须覆盖 0/90/180/270 度源旋转、非零 CropBox 原点、缩放和高 DPI、多页/多框序列。同一证据缩放后仍应框住相同原文。

## 14. 导出设计

导出弹窗显示结果版本、格式、过滤条件和预计记录数。

- JSON：完整领域记录、checks、证据引用、审核状态和 manifest。
- CSV：活性及序列表格；区间、误差和比较符独立列，不能压成数值。对可能成为表格公式的文本做导出转义并保留 JSON 原文。
- FASTA：默认只含 accepted、完整且可确定的序列；其余列为排除原因。修饰序列不能强行转成普通 FASTA。
- 待审核包：候选 JSON/CSV、原因、证据引用和局部截图，用于复核。

第一版实现 JSON、活性 CSV、已接受序列 FASTA；待审核包作为下一步。同一次导出固定 result_revision；审核并发发生时导出仍读取指定不可变快照，不能混入两个版本。

## 15. 视觉与交互规范

- 名称建议：“Bio PDF 工作台”；中文主界面，指标和序列保留原始语言。
- 科研工具风格：浅色中性背景、细分隔线、蓝绿色用于选中与定位、琥珀色提示待审核、红色提示冲突；状态同时有文字。
- 正文 14px 左右，表格保持清楚；序列用等宽字体，不靠缩小字号容纳长序列。
- 顶部操作紧凑，主区域留给 PDF 和数据，避免大面积欢迎横幅与无依据 KPI。
- 支持键盘选择列表、跳转证据和上一条/下一条。提交确认不绑定容易误触的单键快捷键。
- 加载、空结果、缺证据、失败和版本冲突均有专门状态。
- 用户离开未保存的修改时提示；后端重新解析不会覆盖人工修改，旧审核绑定原结果基线并在新任务中明确显示为历史记录。

可交互设计草图使用明确标记的示例数据，仅展示布局、候选选择和审核交互，不代表真实解析器输出。

## 16. 工程目录建议

```text
web/
  package.json / package-lock.json / vite.config.ts
  src/
    app/router.tsx
    api/client.ts / generated/ / queries.ts
    components/AppShell.tsx / StatusLabel.tsx
    features/jobs/JobsPage.tsx / UploadDialog.tsx / JobConfigForm.tsx
    features/workbench/WorkbenchPage.tsx / StageStrip.tsx
    features/pdf/PdfViewer.tsx / EvidenceOverlay.tsx / coordinates.ts
    features/sequences/SequenceList.tsx / SequenceDetail.tsx
    features/activities/ActivityList.tsx / ActivityDetail.tsx / ValueEditor.tsx
    features/tables/TableView.tsx
    features/reviews/ReviewQueuePage.tsx / ReviewPanel.tsx / RevisionConflictDialog.tsx
    features/compare/ComparePage.tsx
    styles/tokens.css / app.css
  e2e/
src/bio_pdf/
  api/app.py / dependencies.py
  api/routes/documents.py / jobs.py / results.py / reviews.py / exports.py / comparisons.py
  api/contracts.py
  application/job_service.py / result_service.py / review_service.py / export_service.py
  workflow/handlers.py / queue_worker.py
  storage/migrations.py
tests/api/ / tests/application/ / tests/integration/
```

以上是拟新增文件，不表示当前已存在。generated 包从 OpenAPI 生成 TS 类型并提交；现有领域 JSON Schema 和 API schema 共享 Pydantic 类型，不能维护两份不同的枚举。

## 17. 分阶段交付与验收

这部分是设计阶段的工作分解；逐行实现代码计划在设计确认后再展开。

### D0：后端修复与统一应用入口

修改 `.gitignore`、现有解析归一化/校验/评测/状态机；创建 `application/job_service.py`、`workflow/handlers.py`。

验收：干净检出可运行；合成 ST.25 和活性输入输出领域记录与 review items；磁盘合同可验证；真实 Docling 坐标与 MinerU 表格不丢失；源身份一致。覆盖审查报告的具体反例。

### D1：API、持久队列和静态前端框架

新增 API app、documents/jobs/results 路由、queue_worker、迁移、web 路由和 API client。

验收：合法文件上传成功；HTML 假 PDF 拒绝；POST job 快速返回 202；重启 API/worker 后任务可查询和恢复；前端列出真实 job 并显示阶段状态；能力不支持的配置不能提交。

### D2：PDF 与证据工作台

实现 PdfViewer、EvidenceOverlay、coordinates、StageStrip、序列与活性列表/详情、表格视图。

验收：点击值跳数据格，点击单位跳表头；缩放/旋转定位正确；跨页证据可遍历；缺 bbox 清楚降级；长专利按需渲染；范围/误差/比较符/未知字符正确展示。

### D3：人工审核与版本持久化

新增 review_service、审核 API、record_versions/current heads、队列、编辑器、冲突弹窗。

验收：修正写入新版本，刷新保留，原始候选不变；同时打开两个窗口提交旧版本返回 409；重复提交不重复产生决定；未知残基/缺必需证据不能直接接受；拒绝有原因并保留历史。

### D4：导出与完整演示闭环

新增 export_service、异步导出 API 和弹窗；完善 README 与 Windows 启动说明。

验收：上传到审核到导出的全过程可重复；JSON/CSV/FASTA 反映同一指定版本；FASTA 只含满足条件的已接受序列；缺结果不可导出空文件假装成功；显式 fixture 模式和真实模式不混用。

### D5：同源对比与人工 baseline

新增对比服务/页面，先修正 matching.py，再补人工标注和评测。

验收：不同源或不同页集合明确拒绝指标比较；无 gold 显示差异不显示准确率；有 gold 时错误单位/数值不能计正确，同一候选不能重复匹配；展示漏提和错误实例并能跳回 PDF。

不建议给这些阶段直接承诺固定天数：当前底层缺口和模型运行环境决定实际工作量。先完成 D0–D4 的可审核闭环，再扩大对比与样本。

## 18. 验收矩阵

| 输入/场景 | 预期 |
|---|---|
| 原生论文与数字表格 | 完整活性字段与单元格证据 |
| ST.25 专利跨页列表 | 序列连续性、声明长度与多页证据 |
| 真扫描件 | OCR 路由与图像证据；未运行不能标完成 |
| HTML 伪装 PDF | 文件检查拒绝，提示具体原因 |
| `AC?GT` | 未知位置保留，不能按过滤后的长度标完整 |
| `1–10 nM` | range；不能变 scalar 10 |
| `1.2 ± 0.3 nM` | plus_minus；不能变 scalar 0.3 |
| `MIC >128 µg/mL` | operator/value/unit 均保留 |
| 多级合并表头 | 每测量有完整指标、单位、靶标上下文 |
| 缺坐标/缺页码 | 明确降级，无猜测高亮 |
| 旋转/裁切/高 DPI | overlay 与源文字一致 |
| Worker/API 重启 | 持久状态恢复，无重复版本发布 |
| 同记录并发审核 | 第二次旧版本提交 409 |
| 一边审核一边导出 | 导出固定 revision，无混合版本 |
| 无人工 gold | 不显示准确率 |

后续实施时增加后端集成与浏览器 E2E 覆盖这些行为。本轮仅核对方案，没有运行前端应用或宣称页面验收通过。

## 19. 官方资料

- [React 文档](https://react.dev/learn)：组件与交互。
- [Vite 入门](https://vite.dev/guide/)：React/TypeScript 模板与构建。
- [React Router 声明式路由](https://reactrouter.com/start/declarative/installation)：路由基础。
- [TanStack Query 文档](https://tanstack.com/query/latest/docs/framework/react/overview)：服务端数据缓存。
- [PDF.js PDFPageProxy](https://mozilla.github.io/pdf.js/api/draft/module-pdfjsLib-PDFPageProxy.html)：页面渲染与 getViewport。
- [PDF.js PageViewport 源码](https://github.com/mozilla/pdf.js/blob/master/src/display/display_utils.js)：坐标变换实现，实施时与安装版本对应。
- [FastAPI 文件上传](https://fastapi.tiangolo.com/tutorial/request-files/)：UploadFile，避免整份文件一次性装入内存。
- [FastAPI 后台任务说明](https://fastapi.tiangolo.com/tutorial/background-tasks/)：重型计算的执行边界。

## 20. 交给后续模型的实施说明

先阅读本设计、`docs/BIO_PDF_DEMO_UI_HANDOFF.md` 和实现审查报告。先修复 D0 并接通实际结果，再推进 D1–D4。保持现有 Python 核心，新增独立 web/，前后端以版本化 API 交互。前端不实现序列/活性规则，不伪造高亮、准确率、阶段百分比或审核成功。所有确认/修正/拒绝都须落库、版本化并影响对应版本导出。D5 在同源条件和人工 gold 完备后交付。
