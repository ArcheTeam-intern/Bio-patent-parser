# MinerU 比较失败诊断与 K01–K08 修改方案

> **For agentic workers:** 使用 `executing-plans` 按任务执行，每项完成后核对本文反例、回归与交付证据；本文件是修改方案，本次复核没有执行业务修复。

**Goal:** 修复当前 MinerU 比较任务的配置与展示问题，补齐 J 轮仍未覆盖的状态隔离、声明合并与配对边界。

**Architecture:** 比较服务统一读取后端运行配置，API 和页面共享引擎就绪契约；远端身份由一个受围栏保护的持久化入口管理；声明先形成逻辑结构，再生成解析输入和字段证据。保留现有 SQLite、不可变制品与两个解析器适配器。

**Tech Stack:** Python、FastAPI、SQLite、httpx、React、TanStack Query、Vitest、Playwright。

日期：2026-10-19。固定复核范围：`dc7409c...4893aaa`，完整 HEAD：`4893aaab2ffdf3519afbe215054e4eee241db47e`。

## 1. 先回答：为什么现在页面显示 MinerU 云 API 失败

### 1.1 当前失败已经定位到本地缺配置

读取真实运行服务 `GET /api/v1/comparisons`，最近的记录为：

| 字段 | 实际值 |
|---|---|
| comparison_id | `cmp-d60a9c9dd9b5` |
| 源文档 | `sha256:ffb2e5bb7a63a87ddb6f7d3bd6c5978e5004fece2045d836aeef4288130e9295` |
| 比较页 | 30、31 |
| Docling | done，attempt=1 |
| MinerU | failed，attempt=3 |
| MinerU 最后结束时间 | 2026-10-19 09:45:15，Asia/Shanghai |
| 错误 | `mineru_http requires MINERU_TOKEN; the cloud side is not runnable here`，`ENGINE_UNAVAILABLE` |

另外，直接读取当前 API 和队列 worker 的进程环境，只输出“有/无”布尔值，结果两者都没有 `MINERU_TOKEN`。没有输出任何 token 值。

失败入口是 `E:/GoLandCode/Bio-patent-parser/src/bio_pdf/application/comparison_service.py:49`。比较任务由 **API 服务自己的比较执行链** 启动，不经过普通 queue worker。只给普通 worker 设置 token 不能解决比较页的问题。

因此这次任务在本地就失败了，尚未请求 MinerU。**当前证据不能说明旧 token 已失效、额度不足、MinerU 云服务故障或 CDN 下载失败。** 配置恢复后是否遇到其他问题，必须按实际返回继续诊断。

### 1.2 配置和提示的缺口

- 当前没有 `.env`、`.env.local` 或 `.env.example`；代码也没有相应配置文件加载入口，运行时只读进程环境。
- 无法从现有证据追溯此前 token 究竟配置在哪个终端或旧进程；不能把“之前验证过可用”当成“本次启动已配置”。
- `E:/GoLandCode/Bio-patent-parser/src/bio_pdf/api/app.py:103` 的 MinerU 能力是硬编码 `available=False`，名字是 `mineru`；实际比较使用 `mineru_http`。
- 比较页面没有消费引擎就绪状态，配置缺失时创建和重试按钮仍可用。当前真实页面已确认“MinerU 云 API：✗”和可点击的“重试失败侧”。
- API/worker 的 venv launcher 与其基础 Python 子进程是父子关系，本次没有把它们误判为重复启动的两套服务。

### 1.3 详情存在旧快照问题

页面把整个 `ComparisonInfo` 保存在 `selected` state。列表轮询或重试后只更新查询缓存，没有同步这个对象。

主审在独立浏览器页面拦截比较接口，模拟“失败→点击重试→成功”，没有向真实服务发送 retry POST。结果：

```text
列表行：完成，Docling ✓，MinerU ✓
展开详情：仍显示 PROBE_MISSING_TOKEN
差异区域：仍提示两侧完成后显示差异
```

这解释了另一种现象：后端恢复后，页面详情仍可能让人以为没有恢复。本次真实失败本身依然是缺少 token，不能用页面问题替代该原因。

## 2. J01–J06 复核结论

### Standards / 可靠性

| 原项 | 结论 |
|---|---|
| J01 远端终态重试 | 主路径已修；迟到旧响应仍可覆盖新 generation，见 K04 |
| J02 进程根归属 | 根归属已修；完整回归中的预算测试不稳定，见 K08 |
| J03 spawn 入口 | 已加入口防护并绑定 owner/run token，原问题符合预期 |

确认 2 项可靠性残留：旧尝试的写回没有代际/所有权围栏，以及预算测试依赖真实解析必须慢于30秒。前者是适配器并发和残留执行的持久化缺口；正常 API 的有效 lease/claim 会阻止许多重复执行，不能描述为正常单实例必现。后者本次已造成完整回归失败，不能把此前一次全绿视为稳定验收。

### Spec / 需求实现

| 原项 | 结论 |
|---|---|
| J04 声明合并 | 单行互补字段已修；跨行又生成重复声明，字段来源也未完整进入常规 OCR 制品，见 K05/K06 |
| J05 配对字形 | µ/μ 等价已修；普通词语大小写引入漏配对回归，见 K07 |
| J06 回放可信度 | 尺寸、旋转失配严格拒绝，诊断模式降级；本次未确认新缺陷 |

确认 3 项 Spec 残留。当前运行配置和页面问题作为现场诊断单列，不冒充均由 J 轮提交引入。

## 3. 修改任务与文件边界

| 任务 | 类型 | 目标 |
|---|---|---|
| K01 | 现场配置恢复与配置入口 | API 重启后能稳定加载 MinerU 配置 |
| K02 | 既有服务/页面契约缺口 | 就绪状态、创建/重试预检、分阶段错误一致 |
| K03 | 既有前端缺陷 | 列表、详情、差异使用最新同一份任务状态 |
| K04 | J01 可靠性残留 | 迟到响应不能回滚新代身份或历史 |
| K05 | J04 领域语义残留 | 多行 OCR 累积为一个逻辑声明 |
| K06 | J04 溯源残留 | 字段级双通道来源进入制品及证据读取 |
| K07 | J05 配对回归 | 普通词规范与 SI 单位大小写分开 |
| K08 | J02 测试证据缺口 | 进程预算测试确定性触发，真实正常完成不误判 |

K01→K02→K03 完成后，可以恢复当前失败侧并真实验收比较页。K04 独立完成；K05→K06 连续验收；K07 独立完成；K08 在下一次完整回归之前完成。每项单独提交并记录提交号，避免一次重构所有链路。

## 4. K01：统一、持久的后端配置入口

**文件职责**

- 新建 `E:/GoLandCode/Bio-patent-parser/src/bio_pdf/runtime_config.py`：统一配置读取和本地配置文件加载；秘密字段不进入 repr/公开序列化。
- 修改 `E:/GoLandCode/Bio-patent-parser/src/bio_pdf/api/settings.py`、`src/bio_pdf/cli.py`、`src/bio_pdf/workflow/queue_worker.py`：API 和 worker 在启动子进程前解析同一显式配置入口。后三个路径均相对于仓库根 `E:/GoLandCode/Bio-patent-parser/`。
- 修改 `E:/GoLandCode/Bio-patent-parser/src/bio_pdf/application/comparison_service.py`、`src/bio_pdf/application/job_service.py`、`src/bio_pdf/workflow/handlers.py`：消费统一配置解析结果，替换各处零散环境判断。
- 新建 `E:/GoLandCode/Bio-patent-parser/.env.example`：仅放变量名和空占位；更新仓库 `.gitignore` 与 `README.md`。
- 新建 `E:/GoLandCode/Bio-patent-parser/tests/unit/test_runtime_config.py`。

**配置契约**

```text
优先级：显式进程环境 > 显式 --env-file 指定的本地文件 > 默认值
配置文件路径：启动时解析成绝对路径，避免工作目录改变后读到另一份文件
子进程：启动前完成加载，让 spawn 子进程继承有效配置
公开信息：仅 configured / config_source / 缺失变量名，不返回秘密值
configured：非空且非纯空白；不等于 authenticated
```

实现者可以选择现有受支持的配置解析库，也可以实现有限、明确定义的环境文件语法；不得把配置文件作为 Python、PowerShell 或 shell 脚本执行。不要仅放一个 `.env` 文件却不接加载入口。

- [ ] 写并运行反例：配置文件有值、当前环境无值，原比较路径仍报缺配置。
- [ ] 实现统一加载，API、worker 和比较 child 读取结果一致；显式环境优先。
- [ ] 测试相对路径转绝对、空白值、缺文件、环境覆盖、启动子进程继承，以及配置对象公开输出不含秘密值。
- [ ] 将真实本地配置放入被忽略的文件；文档和启动命令只引用路径。
- [ ] 执行下方命令验证，再提交本项。

```powershell
.venv/Scripts/python.exe -m pytest -o "addopts=" -q tests/unit/test_runtime_config.py
# 新增入口后的启动形式；配置值填写在本机文件中
.venv/Scripts/python.exe -m bio_pdf.cli serve --env-file .env.local --data-dir data/post-m1-demo --web-dist web/dist --port 8420
.venv/Scripts/python.exe -m bio_pdf.workflow.queue_worker --env-file .env.local --data-dir data/post-m1-demo
```

**真实验收**：重启前确认实际服务实例及其子进程归属；启动后 API 与比较 child 都报告配置已加载。只重试 `cmp-d60a9c9dd9b5` 的失败侧，Docling 已完成侧的 attempt 和制品不变。保存新的 attempt、batch_id 和错误阶段；如果进入 CDN 下载失败，另记为下一阶段错误，不再写“缺少 token”。

## 5. K02：能力、预检与错误状态形成一个服务端契约

**修改文件**

- `E:/GoLandCode/Bio-patent-parser/src/bio_pdf/api/contracts.py`
- `E:/GoLandCode/Bio-patent-parser/src/bio_pdf/api/app.py`
- `E:/GoLandCode/Bio-patent-parser/src/bio_pdf/api/routes/comparisons.py`
- `E:/GoLandCode/Bio-patent-parser/src/bio_pdf/application/comparison_service.py`
- `E:/GoLandCode/Bio-patent-parser/web/src/api/client.ts`
- `E:/GoLandCode/Bio-patent-parser/web/src/features/compare/ComparePage.tsx`
- 测试：`E:/GoLandCode/Bio-patent-parser/tests/api/test_comparisons.py`；新建 `E:/GoLandCode/Bio-patent-parser/web/src/features/compare/ComparePage.test.tsx`。

**建议能力响应**：扩展现有 `/api/v1/capabilities`，保留兼容读取规则；规范执行名使用 `mineru_http`，旧 `mineru` 明确列为 alias，不让页面自行猜名字。

```json
{
  "name": "mineru_http",
  "aliases": ["mineru"],
  "usage": ["comparison"],
  "configured": false,
  "credential_validation": "not_checked",
  "unavailable_reason": {
    "code": "ENGINE_NOT_CONFIGURED",
    "stage": "configure",
    "missing": ["MINERU_TOKEN"],
    "retryable": false,
    "message": "API 服务尚未配置 MinerU 凭据"
  }
}
```

能力是配置和本机支持状态，不能暗示已远程验证身份。不要在每次 capability GET 时提交 PDF 或请求云端解析。

**预检与错误要求**

1. 创建比较和 retry 均先核对请求的引擎配置，再写 comparison/attempt、启动线程或调用外部网络。
2. 缺配置返回 422、`ENGINE_NOT_CONFIGURED`，新建场景零任务/零远端调用；retry 场景零 attempt 增量，旧成功侧不动。
3. 修正 retry 路由目前把所有 WorkflowError 都映射为 404 的做法。不存在任务才用 404；配置、参数和运行错误各保留语义。
4. 持久化结构化 `error_info`，旧 `error` 字符串兼容展示。至少包含 `engine/code/stage/retryable/message/attempt`，远端身份存在时带 batch_id/generation。
5. 错误阶段至少区分 `configure/allocate/upload/poll/download/normalize/domain`。无证据时使用明确的 unknown，不根据异常字符串伪造阶段。
6. 旧记录只有字符串时兼容显示，不能伪装为新格式已验证的结果。

- [ ] 缺 token 的 create/retry API 回归先失败，再接预检修复。
- [ ] 前端消费 capability，显示“未配置”，禁用创建/失败侧重试并说明原因；配置刷新后恢复按钮。
- [ ] 显示 API 返回的结构化配置错误；禁用 mutation pending 期间的重复点击。
- [ ] 模拟 401/403、429、上传 5xx、远端 failed、CDN TLS/503，分别验证阶段与是否可重试。模拟证据和真实返回分开记录。
- [ ] 执行并提交本项：

```powershell
.venv/Scripts/python.exe -m pytest -o "addopts=" -q tests/api/test_comparisons.py tests/api/test_comparison_hardening_g.py
npm --prefix web run test -- --run src/features/compare/ComparePage.test.tsx
```

## 6. K03：详情由 ID 查询，不保存任务对象快照

**位置**：`E:/GoLandCode/Bio-patent-parser/web/src/features/compare/ComparePage.tsx:109`、`:139`、`:253`。

**实现边界**

- state 只保存 `selectedId`。
- 列表读取 `['comparisons']`；详情读取 `['comparison', selectedId]`，按当前任务 queued/running 状态轮询。
- retry 成功时同时更新或失效该 ID 的详情与列表缓存；完成后加载该任务当前结果对应的 diff。
- 详情与列表因短暂不同请求时序产生差异时，展示更新时间/加载状态，不能继续长期显示旧失败对象。
- 如果 diff 有缓存，retry 后使其失效，避免新 attempt 复用旧 diff。
- 切换 ID 的响应不能覆盖当前选择；沿用项目已有查询隔离方式。

**确定反例**

```text
初始 cmp-probe-stale：failed，MinerU error=PROBE_MISSING_TOKEN
点击“查看”后保持详情展开
浏览器拦截 retry 响应和后续列表，状态改为 done
现实现：列表 done，详情仍 failed
目标：无需再次点击“查看”，详情 done、旧错误消失、DiffView 出现
```

- [ ] 在 `ComparePage.test.tsx` 以受控 API 响应写上述回归。
- [ ] 将选择对象改为 ID，接入详情查询和相关失效。
- [ ] 补 queued→running→done、失败重试、切换两个比较 ID、旧 diff 失效用例。
- [ ] 浏览器复测时拦截所有测试 retry POST，先完成离线验收；恢复本机真实任务时再记录真实调用。
- [ ] 前端测试与 build 通过后提交。

## 7. K04：所有远端身份写回都必须有代际和所有权围栏

**位置**：`E:/GoLandCode/Bio-patent-parser/src/bio_pdf/parsers/mineru_adapter.py:1386`、`:1401`、`:1406`；通用写文件入口 `:115`。

**确证复现：只用 MockTransport 和事件延迟**

```text
A 对 b1 的旧轮询暂停
B 对 b1 返回 failed → terminal_failed
C 显式重试 → b2/gen2/completed/history=[b1]
放行 A 的旧 failed 响应
现实现最终变成 b1/gen1/terminal_failed/history=[]
POST 总次数为 2
```

当前 attempt_token 只被记录，没有在写回前核对。仅分配阶段加锁挡不住锁外轮询、上传和完成状态写回；公共 `.json.tmp` 还存在并发写竞争。

**文件职责**

- 新建 `E:/GoLandCode/Bio-patent-parser/src/bio_pdf/parsers/remote_identity_store.py`：短事务锁、重读、围栏检查、历史追加、唯一临时文件和原子替换。
- 修改 `E:/GoLandCode/Bio-patent-parser/src/bio_pdf/parsers/mineru_adapter.py`：所有身份转换都走该入口。
- 必要时修改 `E:/GoLandCode/Bio-patent-parser/src/bio_pdf/application/comparison_service.py` 和 `src/bio_pdf/parsers/base.py`：传递从有效 claim 得到的 attempt/owner 身份。
- 扩展 `E:/GoLandCode/Bio-patent-parser/tests/unit/test_mineru_batch_protocol.py`。

**转换契约**

```text
版本身份 = fingerprint + generation + batch_id + owner_attempt/token + revision
transition(expected_version, patch):
  短锁内读取最新状态
  不匹配 expected 或 owner 已失效 → 拒绝写回，保留新状态
  根据最新状态追加历史、应用 patch、增加 revision
  本次独有临时文件写入后原子替换
网络请求在短锁之外执行
```

owner 的认领必须来自有效的服务 claim；不能让任意迟到 parse 调用靠写 `last_attempt_token` 自己取得新所有权。锁过期回收和释放也要核对锁 owner，避免旧持有者删除新锁。

- [ ] 写网络事件控制的旧失败晚到用例，断言最终仍为 b2/gen2/completed，history 不丢。
- [ ] 增加旧成功晚到、新身份 fingerprint 改变、同代旧 revision 晚到反例。
- [ ] 接统一转换入口，确保 allocated/uploaded/polling/terminal_failed/completed 全覆盖。
- [ ] 保留下载失败复用、PUT 429/5xx 同 slot、并发只分配一个 generation 的现有回归。
- [ ] 连续重复并发测试，并核对身份文件始终可读、无临时文件竞争错误；测试通过后提交。

```powershell
.venv/Scripts/python.exe -m pytest -o "addopts=" -q tests/unit/test_mineru_batch_protocol.py
```

## 8. K05：按逻辑声明累积 OCR 多行，再统一生成解析输入

**位置**：`E:/GoLandCode/Bio-patent-parser/src/bio_pdf/workflow/handlers.py:1037–1075`。

当前每条 OCR 行都重新对静态 native 候选做并集；同一个 native 块被多个 OCR 行重复合并，导致输出多个 `<210>`。

**复现输入**

```text
native bbox=[10,100,200,140]：<210> 1 <211> 5
OCR y=100：<210> 1 <212> PRT
OCR y=120：<213> Homo sapiens
后续：<400> 1
      Ala Gly Leu Lys Val
```

真实融合→ST.25→inventory 当前输出：

```text
<210> 1 <211> 5 <212> PRT
<210> 1 <211> 5 <213> Homo sapiens
observed_declarations=2, conflict=2, complete=0
molecule_type=unknown
```

**目标结构**

先收集一个有边界的声明组：序列号、字段列表、来源行、来源块、冲突字段。处理完该组的所有 native/OCR 行后，再输出一个规范声明和来源关系。声明组必须结合段落/页/相邻顺序及原始声明边界，不能仅按整个文档的 seq_id 全局合并，否则会吞掉真实重复声明。

可在 `E:/GoLandCode/Bio-patent-parser/src/bio_pdf/extract/sequences/declaration_merge.py` 放纯合并逻辑，handler 负责区域和通道的输入输出。模块按职责提取即可，不扩大为重写完整 ST.25 parser。

- [ ] 在 `E:/GoLandCode/Bio-patent-parser/tests/unit/test_declaration_merge_j04.py` 写上述反例。
- [ ] 累积字段并集后一次输出；禁止同一 native 块按 OCR 行重复生成逻辑声明。
- [ ] 真实领域链断言：一个逻辑声明、length=5、protein、五残基 complete、伪 conflict=0；物种字段可追溯。
- [ ] 改为四残基必须保留真实长度不匹配；共享字段冲突继续保留双方。
- [ ] 覆盖三行 OCR、跨页续行、输入顺序交换、同页多个序列，以及文档中真正重复 seq_id 的冲突。
- [ ] 定向测试通过后提交：

```powershell
.venv/Scripts/python.exe -m pytest -o "addopts=" -q tests/unit/test_declaration_merge_j04.py tests/unit/test_declaration_conflicts_h05.py
```

## 9. K06：字段来源不能只存在于临时返回值

**位置**：新增字段记录在 `E:/GoLandCode/Bio-patent-parser/src/bio_pdf/workflow/handlers.py:1062–1100`；普通 region OCR 的消费筛选在 `:1484–1489`，制品输出在 `:1782–1800`。

**现状证据**

- 合并函数返回 `declaration_merge.fields`，native 字段能带 block_id；OCR 字段没有完整定位身份。
- aliases 实际只保留 kind/text，没有完整原始块引用。
- 普通常规 OCR 只筛入 content_conflict、coverage_supersede；region-ocr 制品没有保存 declaration_merge。
- 辅助领域链的 EvidenceIndex 最终只看到 `<400>1` 与残基，没有 `<211>/<212>` 字段证据。
- adopt 分支会保存 merge records，不能把它与普通 region OCR 的消费遗漏混为一谈。

证据边界：本次运行了合并→序列证据的辅助链，持久化遗漏另由普通 OCR 消费源码确认；没有把该探针冒充完整 ExtractStage 端到端实跑。

**持久化契约**

```text
每个逻辑字段：declaration_id / seq_id / tag / value / origins[]
每个 origin：source_parser / source_artifact_id / block_id / page_number
             raw_text / bbox / bbox_verified / location_precision
```

缺框时保持页级，不合成虚假精细坐标；证据引用原始不可变制品，显示合并值时同时能定位各通道的原始字段。

- [ ] 修改普通 region OCR 制品写入，持久化合并声明、字段来源和原始块别名；与 adopt 使用同一序列化契约。
- [ ] 在 `E:/GoLandCode/Bio-patent-parser/src/bio_pdf/extract/evidence.py` 及 ST.25 字段消费处连接声明字段证据，保留残基证据。
- [ ] 核对现有结果/证据 API 是否能够返回新增引用，旧制品读取保持兼容。
- [ ] 扩展 `test_declaration_merge_j04.py`，并新建 `E:/GoLandCode/Bio-patent-parser/tests/unit/test_declaration_provenance_k06.py`：实际跑普通 region OCR 的制品消费入口，重新打开落盘制品后再取字段证据。
- [ ] 断言 `<211>` 和 `<212>` 分别能回到 native/OCR 原始来源，字段、页号、源制品可核对；不是只检查函数内的 fields 数组。
- [ ] 验证无 bbox 降级、双通道同值、多来源字段、旧制品无新字段，以及区域采用分支；通过后提交。

## 10. K07：词语大小写规范与 SI 单位大小写拆开

**位置**：`E:/GoLandCode/Bio-patent-parser/src/bio_pdf/application/comparison_service.py:184–204`。

J05 使整个 table_anchor/row_context/column_context 都保留大小写。它保护了 SI 单位，却将普通词语也视为大小写敏感身份。

**真实领域链反例**

```text
caption 两侧：MIC at 10 µM
实体：Peptide A；值：4
A header：MIC (µg/mL)
B header：mic (µg/mL)
```

两侧的 metric=MIC、unit_normalized=ug/mL、实验条件一致。对相同生产测量数据，旧 `dc7409c` 配对 1，当前配对 0。

**修改要求**

1. 优先用已提取的指标、靶标、条件、行列身份形成配对键；原始上下文负责必要的消歧和溯源。
2. 上下文仍须进入身份时，识别其中单位 token：普通文字可作一致的词语规范，单位 token 用现有单位语义规则，µ/μ 字形等价且保留 SI 大小写。
3. 不对整行盲目 lower，也不对整行一律保留大小写；不要删除所有 row/column context 来绕过该反例。
4. 无法可靠拆解的上下文明确保留歧义，不把不同实验条件强行配对。

- [ ] 在 `E:/GoLandCode/Bio-patent-parser/tests/unit/test_table_conditions_g07.py` 写真实 `_doc→_domain_pass_spec→pair_measurements` 反例，目标 paired=1、only=0。
- [ ] 保留 `µg/mL` vs `μg/mL` paired=1、`µM` vs `µm` 不配对、`mM` vs `M` 差异、不同 dose 条件不强配。
- [ ] 增加普通 caption/表头词大小写、同实体多个单元格、同词不同单位，以及误差/时间条件的交叉矩阵。
- [ ] 确认歧义单元格仍报告 ambiguous，原文与两侧证据保留；通过后提交。

```powershell
.venv/Scripts/python.exe -m pytest -o "addopts=" -q tests/unit/test_table_conditions_g07.py
```

## 11. K08：预算测试必须控制执行时序

**位置**：`E:/GoLandCode/Bio-patent-parser/tests/api/test_process_tree_h01.py:108`、`:120`、`:126–134`。

**本次真实失败**

完整回归的 `test_budget_timeout_terminates_only_the_registered_tree` 捕获到了自有中继和 Docling 后代，但最终侧状态是 `done`。测试却要求状态为 `failed`。完整汇总为：

```text
1 failed, 690 passed, 4 skipped, 1 warning in 304.22s
pytest exit = 1
AssertionError: side status 'done' != 'failed'
```

随后仅独立复跑这个用例，仍为 `1 failed in 93.96s`、退出1，实际状态依然为 done、error=None。它不是仅在完整集资源竞争时出现的现象。

当前用例使用30秒预算，没有控制推理后代在预算到期时仍存活。单页解析如果在预算前正常结束，产品返回 done 是合法的。观察到引擎进程曾出现，并不证明它一定会活过30秒。

**修改原则**

- 测试侧准备 ready/release 握手，明确后代已经启动、仍处于受控运行窗口，然后再触发预算到期。
- 如果继续使用真实 Docling 链，可在测试专用启动包装层注入握手：子进程完成真实加载/转换步骤后保持存活，外层确认 ready 后触发受控预算；不改产品默认行为，不依赖大 PDF 或冷模型加载制造耗时。
- 将真实模型解析质量/正常完成与确定性的进程树终止契约分别断言。若采用受控测试后端，证据必须写明它是进程树契约测试，不冒充真实模型推理超时。
- 预算验证需要同时证明：到期前自有子树存活、到期后自有子树退出、迟到零发布、两个无关进程仍存活。
- 不能把 `done` 纳入该超时测试的通过条件，不能简单缩短预算导致后代还没启动就被杀或 skip，也不能不断放大预算碰运气。

**可直接采用的握手结构**

1. 新建测试辅助 `E:/GoLandCode/Bio-patent-parser/tests/helpers/docling_hold_bootstrap.py`，仅在本测试自有子树的隔离导入环境启用。包装真实 `docling_adapter._build_converter_from_request` 返回的 converter；先调用真实 `convert`，在其返回交回 `docling_child` 前写 ready 并等待 release。正式子进程入口仍为 `-m bio_pdf.parsers.docling_child`，不加入产品 sleep 开关。
2. ready 记录 `run_token/comparison_id/side/attempt/own_pid/parent_pid`。外层根据注册根及 ParentProcessId 链核对身份，检查 hold 尚未结束、结果尚未发布、两哨兵仍存活。
3. 在比较监督器预算计算处提取小的 monotonic/deadline 注入点，默认使用真实 `time.monotonic`。测试仅冻结/推进这一个预算时钟，ready 后推进到超限；不要全局替换时间函数而影响 lease/心跳。启动及 ready 等待另设真实墙钟上限，超时明确失败并清理自有树。
4. 断言实际 `EXECUTION_BUDGET_EXCEEDED`、release 未发送、自有树退出、迟到零发布。保留使用真实时钟的预算公式单测，防止受控时钟掩盖计算错误。

- [ ] 写握手测试辅助入口，在临时目录与仅本次子树内生效；所有异常路径释放/终止并 join 自有进程。
- [ ] 保留现有 comparison 身份注册表、owner/run token 校验和无关 mp/subprocess 双哨兵。
- [ ] 真实正常完成的烟测允许并验证 done；超时契约测试必须实际得到预算失败和子树退出。
- [ ] 对新预算测试至少连续运行3次，再执行完整集，记录每次状态和自有PID证据。
- [ ] 冷启动与热缓存环境都不依赖推理速度决定测试结果；通过后提交。

```powershell
.venv/Scripts/python.exe -m pytest -o "addopts=" -q -rs tests/api/test_process_tree_h01.py
```

## 12. 最终验收与交付

### 12.1 执行顺序

1. K01/K02 后配置可用性可见；缺配置请求立即给出可操作原因，不制造新失败 attempt。
2. K03 后列表、详情和差异同步；用浏览器离线反例先验收。
3. K04 完成迟到响应隔离；K05/K06 完成跨行声明与持久化证据；K07 完成配对规范。
4. K08 完成确定性预算验收后，重跑完整后端、前端和 build。
5. 对现有比较只重试 MinerU 失败侧，记录真实云链路，不重复消耗已完成的 Docling 侧。

```powershell
$env:PYTHONUTF8 = '1'
.venv/Scripts/python.exe -m pytest -o "addopts=" -m "not live" -q -rs
npm --prefix web run test -- --run
npm --prefix web run build
```

### 12.2 真实比较页验收表

| 情景 | 必须看到的结果 |
|---|---|
| API 无 token | capability 未配置；创建/重试不可用；API 预检零新 attempt |
| API 配置已加载 | capability configured；不声称已认证 |
| 失败侧重试 | 只更新失败侧；Docling 已完成结果不动 |
| 云端401/403 | 与本地缺配置分开显示；保留实际响应阶段 |
| 下载失败 | batch_id 可追溯；按既有恢复规则复用 |
| 任务完成 | 列表、展开详情、diff 都更新，无旧失败残留 |
| 关闭重开页面 | 从服务端读取现状，不依赖页面历史 state |

真实云结果为空时仍需展示 coverage、问题和未核验说明，不把“两边都是0”写成解析效果一致。

### 12.3 实现方交付

- 每个 K 编号对应提交号、文件、修复前后反例与测试命令。
- 配置来源与实际 API/child 读取结果，仅报告状态。
- 真实比较 ID、attempt、页范围、batch_id/generation、最终阶段和结果制品引用。
- 失败、mock、已有 ZIP 重放、真实云成功分别标记；不得用其中一类替代另一类。
- 当前仍未解决的限制如实列出。

### 12.4 本次独立验证记录

本次完整后端 **690 passed、4 skipped、1 failed，退出1，304.22秒**；失败项及原因见 K08。前端 **37 passed / 8 files**，构建退出 0。Standards 定向 **36 passed**、Spec 定向 **37 passed**，后两者是完整测试集的子集，不叠加统计，也不能抵消完整集的失败。

4项跳过均为现有live开关未开启：3项Docling live、1项MinerU live。没有把它们写成实跑成功。

本次日志与退出状态另存于 `%TEMP%/bio-J-review-20261019-pytest.log`、`%TEMP%/bio-J-review-20261019-pytest-result.json`。使用 `PYTHONUTF8=1`，没有发起live云解析；本次结果不是执行报告中691通过的历史数字。

原样例已知限制继续保留：普通 CDN 下载环境、真实 TABLE 3 上游网格失真、Docling 论文药敏为0、其他 API 版本坐标、人工 gold 未核准。本次没有重新发起云解析，没有修改业务代码、重启服务或改写真实比较状态，也没有得出新的解析准确率结论。
