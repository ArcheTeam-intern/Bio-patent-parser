# F01–F10 独立复核：代码缺陷、原文抽查与真实 MinerU 对比

日期：2026-10-17（验证从 10 月 16 日深夜开始）。固定版本：`c0ddf0e`；差异：`git diff 40f5f4d...c0ddf0e`。

输入：`reports/2026-10-16_E01_E10_independent_recheck.md`、`reports/2026-10-16_F01_F10_fixes.md`。

## 结论

**仍不满足整体验收。** 上一轮若干最小复现已修复，但出现新的完整性、并发和领域语义边界；真实 PDF 抽查也证实仍有序列遗漏和错误。

本轮已使用用户提供的凭证完成真实 MinerU 云端任务，并取得原始 ZIP。凭证只传给临时验证进程，不写入配置、仓库或报告。未修改生产业务代码、生产任务或系统网络配置；对比运行使用独立数据目录。

## 一、验证结果与已修复事项

| 验证项 | 本次独立结果 |
| --- | --- |
| Python 全量非 live 测试 | **590 passed、4 skipped**，113.91 秒，退出 0 |
| 前端测试 | **33 passed / 7 files**，退出 0 |
| 前端构建 | TypeScript、Vite 均成功，退出 0 |
| 对比服务重复方法 | 已删除，上一轮 11 个重复定义不再作为未修复问题报告 |
| 候选编号 | 已原子预留且排他创建，顺序/并发覆盖的上一轮路径已有修复 |
| 重试与恢复 | failed 不再被普通领取；增加周期恢复与续租，但新并发边界见 S02–S04 |
| 候选采用 | 内容确实进入新任务，不再只是重新跑原 PDF；完整性仍有 S01 |
| 当前基线与 OCR 侧车 | 三个重跑任务 DB/manifest 的 baseline 一致；EP 两任务的 region-ocr.json 均已纳入发布清单 |
| 来源表格 | table-only 来源通道及前端单元格展示已有实际实现 |

Python 全量测试子进程将 `MINERU_TOKEN` 置空，云端验证另行执行，避免测试偶然使用真实配额。测试全绿不覆盖以下隔离复现。

### 已发布样本复核

| 任务 | 实际制品结果 |
| --- | --- |
| `job-0fde96e378e8`，EP 全文 | 82 声明；complete 7 / partial 19 / missing 54 / conflict 2；36 序列候选、14 活性、34 关系；14 个活性均无结构化 conditions |
| `job-a1e99f6fff97`，EP 30–33 | 10 声明；complete 1 / partial 3 / missing 4 / conflict 1 / declared 1；5 序列候选、5 关系 |
| `job-550d519df867`，论文 | 2 序列候选、50 实体、255 活性、255 关系；255 个活性均无结构化 conditions |

这里的 complete 是系统状态，不能等同于逐字符正确率；82 声明也不是恢复 82 条序列。

## 二、Standards / 可靠性：5 项

### S01 [P1] 校验了候选 txt，实际消费的却是未校验 meta

位置：`src/bio_pdf/application/job_service.py:252–269`；`src/bio_pdf/workflow/handlers.py:1347–1363`。

新任务有效配置只锁定候选正文哈希；worker 从复制的 `candidate.meta.json.lines[].text` 取得实际残基，而未验证这份元数据的哈希和正文一致性。

隔离复现：候选正文为 `Ala Gly Leu Lys`；创建采用任务后只将复制的 meta 行文本改为 `Gly Gly Gly Gly`，txt 字节与哈希不变；真实 worker 仍成功发布 **GGGG / complete**。采用内容和最终结果失去完整性约束。

修改：候选内容、行框、源身份、覆盖策略组成同一个受哈希保护的不可变输入；将 meta 哈希写入有效配置并在消费时校验；正文与有序行文本必须一致。任何不一致必须明确失败，不能退回未验证内容。

验收：创建后分别篡改 txt、meta 文本、meta 框、源 SHA；均在提取前拒绝。合法候选仍通过真实 worker 恢复原序列。

### S02 [P1] 多线程共享 SQLite 连接，心跳提交了其他线程的领取事务

位置：`src/bio_pdf/application/comparison_service.py:489–525、564–574`。

`check_same_thread=False` 允许跨线程访问，不等于事务隔离。恢复器会用同一个 service 启动多个对比线程，心跳与领取共用连接。

隔离复现：领取事务 UPDATE 后暂停；另一线程续租执行 commit；领取线程继续时报 `OperationalError: cannot rollback - no transaction is active`。侧状态已提交为 running，却没有执行解析。

修改：每线程/每操作独立连接，并显式限定事务生命周期；或用统一锁覆盖共享连接的所有事务（不能只锁 claim）。优先把租约状态操作收敛到仓储层。

验收：同一恢复器同时启动至少两个比较；设置屏障交错 claim/renew/publish，不出现跨事务提交、无执行的 running 或事务异常。

### S03 [P1] 执行预算到期后仍阻塞并持续续租

位置：`comparison_service.py:638–650`。

超预算只设置 Event，仍等待同步 `_run_one()` 返回，期间继续续租。预算 0.10 秒，解析阻塞 0.65 秒时仍为 running、有效租约、不能接管；释放解析才标失败。真正挂死时预算失效。

修改：用受监督的可终止执行单元；到期先围栏失败并停止续租，终止本地执行。远端任务保留身份，后续可继续取回，不能通过重新上传“恢复”。旧执行即使迟到也不能发布。

验收：解析永不返回时，仍在预算及容差内进入明确终态；无活跃续租和后台残留；Windows 独立进程场景也必须覆盖。

### S04 [P2] 对比总体状态可被旧快照写回 running

位置：`comparison_service.py:707–723`。

总体状态读取和写入之间没有事务。两个独立连接复现：旧刷新读到 done/running 后暂停；另一侧完成，新刷新写 done；旧刷新随后写回 running。最终两侧均 done，总体仍 running，恢复器不再领取任何侧。

修改：在同一事务内派生并更新总体状态，或 GET 实时从侧状态计算；显式重试代次下的状态变化也应原子处理。

验收：双连接屏障复现上述交错，总体终态不能回退；前端轮询必须停止在实际终态。

### S05 [P1] 对比的跨 attempt 云端身份仍丢失，重试会重新上传

位置：`comparison_service.py:744–758`；`src/bio_pdf/parsers/mineru_adapter.py:1020–1033`。

比较输出目录是 `comparisons/<cid>/<engine>/attempt-N`，ParseRequest.job_id 却是 `<cid>:<engine>`。`_stable_job_dir()` 在祖先目录找不到该名字，退回 attempt 目录。因此普通 workflow 的跨 attempt 修复没有覆盖比较服务。

主审隔离复现：同一比较、同一源 SHA、页集与参数，调用真实 `_run_one` 的 attempt 1、2，使用已有 MockTransport 计数，**`POST /file-urls/batch` 共 2 次**；两个 attempt 分别保存身份文件。

本轮真实任务恰好遭遇“云端 done、下载失败”，这时产品 retry 会重新提交，本应只需继续下载。

修改：显式提供 comparison-side 级 remote-state 根目录，身份按完整请求指纹保存；每 attempt 的输出仍独立，远端身份跨 attempt 共享。原子写入并防止并发重复提交。

验收：attempt 1 远端完成但下载抛错；通过真实比较 retry 启动 attempt 2，上传/提交计数仍为 1，只增加 poll/download；改变源、页集或有效参数时才创建新远端任务。

## 三、Spec / 需求实现：7 项

### P01 [P1] OCR 合并仍抹掉声明编号和长度的数字差异

对应 F05。位置：`handlers.py:847–848、907–914`。

fingerprint 删除全部数字，导致同区 native `<210> 1` / OCR `<210> 2`，以及 `<211> 10` / `<211> 20` 都被判 `merge_equal`；只保留 native，没有来源冲突。

修改：按语法区分声明、残基行和行计数。声明编号/长度是内容，不得删除；仅对已经确认属于残基位置标记的数字做专门处理。

验收：编号、长度不同必须保留双方并阻止无审查确认；同一残基行的真实位置计数差异则按专门规则处理，不能全局去数字。

### P02 [P1] 完整实验条件仍没有进入生产活性提取链

对应 F06。位置：`comparison_service.py:889–891`；`src/bio_pdf/extract/activities/tables.py:262、349`。

比较代码能序列化 conditions，但上游表格提取仍固定 `conditions=[]`。隔离真实 `_domain_pass`：相同表格、正文 incubation 1 h 与 2 h，输出 conditions 均空，随后 paired=1、diffs=[]。现有条件测试直接手工构造非空字段，绕开了生产链。

修改：从表头/表注、方法引用和局部实验上下文提取并链接条件，保留证据及未知状态；无法确定条件时明确“条件未核对”，不能显示为完整实验一致。

验收：从规范化文档开始测试 1h/2h、不同菌株/培养条件/处理剂量；不允许只测试手填条件的配对函数。

### P03 [P2] 单位 lower() 抹平了 SI 大小写语义

对应 F06 及上一轮“单位大小写不能随意抹平”。位置：`comparison_service.py:118–119`。

隔离输入 `5 µM` 与 `5 µm`，输出 paired=1、diffs=[]；浓度与长度差异被消除。

修改：区分 SI 大小写和量纲，明确别名表，只合并等价 Unicode 字形；数量级换算需对数值和单位一起进行，不支持的单位标 unknown。

验收：µM/μM 可等价，µM/µm、M/mM 不可直接等价；1 mM 与 1000 µM 若实现换算应语义一致，否则应明确未换算，不能误报已相等。

### P04 [P1] 表格坐标仍覆盖原点；真实云端归一化坐标尚未转换

对应 F07。位置：`mineru_adapter.py:1541` 及 content-list 投影路径。

隔离声明 bottom-left 的 600×800 页，原框 `[10,20,110,70]`：文本正确转为 `[10,730,110,780]`，表格仍保留原框，因为 `_project_table_item` 强制 TOPLEFT；descriptor 还可能宣称基准可信。

真实 EP 云端返回还出现 `[142,108,605,122]` 等 0–1000 范围坐标，被直接放进 595×842 pt 页面，产生大量越界告警。当前“仅告警、不可信声明”比假装正确好，但尚未完成可定位的适配。

官方源代码 `_build_bbox` 将 pt 坐标乘 1000 再除页面宽高；这是输出归一化的明确依据，应按具体协议/版本反变换，而不依赖响应额外自带自定义 unit 声明。[MinerU 官方源码](https://github.com/opendatalab/MinerU/blob/3e60291846cb7c3bf8fe7f4f16238f4fc6cce491/mineru/backend/pipeline/pipeline_middle_json_mkcontent.py)

修改：对已支持协议建立明确坐标描述符；text/table/figure/cell 共用原点、单位、旋转、裁剪变换。真实 ZIP 的 layout.json 提供 page_size，可与源 PDF 核验；未知协议才降级，不猜测。

验收：固定本次真实 content_list 及源页尺寸；转换后的框全部落入正确页面并经渲染叠框核查；bottom-left 表格与文本一致。

### P05 [P1] 未验证几何的状态没有传到证据与前端

对应 F07“未知制式不承诺精确框”。`coordinate_basis_verified` 的使用仍局限于 adapter；未验证框继续进入 normalized 和证据。隔离 `span_from_block_match` 仍输出 `geometry_precision=block/location_precision=block`，没有标识该框的基准不可信。

修改：规范化块、证据片段、API、前端贯穿 geometry trust；未验证的框保留 raw，仅提供页级导航并明确原因，不能依赖它做可信裁图/高亮。完成 P04 的已知协议转换后才允许可信块级定位。

验收：未知单位/原点的真实页面，从 adapter 到 API/UI 均降级；已验证框正常高亮。不得只检查 manifest 上的一个布尔值。

### P06 [P1，真实云端复现] 页边行号粘连使 ST.25 的 <400> 失效

位置：`src/bio_pdf/extract/sequences/st25.py:132–145`，`_TAG_RE` 在文件 `:38` 锚定行首。

真实 MinerU 返回第 30 页为 `50 <400> 1`，后面有完整的 `Ser Tyr Gly Val His 1 5`。前缀 50 是 PDF 页边行号，但解析器不把它识别为标签，序列 1 未进入残基状态，因此丢失。**不是 MinerU 没识别到残基，也不是多标签同一行不受支持**；当前 feed_line 已支持同一行多个标签。

主审控制复现：只在内存中移除标签前的页边计数，不改任何残基/页框，重放同一真实 normalized，序列数从 7 变成 8，新增 `SEQ ID 1 = SYGVH`。没有修改生产制品或提交修复。

修改：增加保留原文偏移的 ST.25 标签词法层，识别有证据的行号前缀；输出 clean→raw 映射。不得将 P01 的“删除所有数字”当成替代。

验收：固定真实 `50 <400> 1` 样例；同测带行号的 `<210>/<211>`、跨页续行、多标签一行及正文里提及标签的负例。

### P07 [P1，真实云端复现] 药敏表因角色全 unknown 被静默排除

位置：`mineru_adapter.py:1520`；`src/bio_pdf/extract/activities/table_classifier.py:82–92、103–121`。

真实论文第 9 页左侧 TABLE 3 是药敏矩阵。MinerU 返回的表包含药名、菌种、分离株和 70 个 S/R/I 单元格，但使用 HTML td 而非 th；适配后所有 header_role 为 unknown。分类器只统计 role=data 的值，因而 S/R/I 计数实际为 0，表被判 INVENTORY，活性归零。

第 8 页的云端 HTML 本身也有明显结构问题：投影成 12×392、593 个 S/R/I 单元格，`structure_issues=[]`；这不应直接当作可信的正常表继续走无问题的成功路径。这里同时存在上游结构失真和下游质量检查缺口，不能仅改分类优先级后强行生成活性。

修改：为 td-only 和旋转药敏表做有证据的表头/轴语义归一；检查异常跨度、网格规模与行列结构，失真时明确 issue 并触发区域定向重解析或人工核对。评分表仍保持非活性负例。

验收：用本次真实 HTML/原图回放，TABLE 3 不再静默作为库存表；错误结构可见、正确的药物—菌株—S/R/I 关系有证据；TABLE 4 评分不产生活性。不要要求输出数量恰好等于现有 255，必须按人工核对的单元格验收。

## 四、PDF 原文抽查：序列 1–5

按 pdf 技能用 Poppler 140 dpi 渲染并查看完整第 30、31 页。以下是模型目视核对，**不是人工批准的 gold，不据此计算全库准确率**。

| SEQ ID | 原文序列 | 全文任务 | Docling 30–33 生产子任务 |
| --- | --- | --- | --- |
| 1 | `SYGVH`，5 aa | 相符 | 相符 |
| 2 | `VIWSDGTTTYNSALKS`，16 aa | 16 个问号占位 | 未恢复 |
| 3 | `DTHYRLYYYAMDY`，13 aa | `ARRRRRRRRRRRRYR`，不符，系统已标冲突 | 未恢复 |
| 4 | `RSSQSLVHSNGDTYFH`，16 aa | 16 个问号占位 | 未恢复 |
| 5 | `KVSNRFS`，7 aa | 相符 | 未恢复 |

原文说明这组 CDR 同时属于 #134、#136；不能把序列和活性固定建成一对一。相同序列可属于多个构建体，同一构建体在不同条件下也可有多个活性。

抽查记录：`reports/f-round-recheck/EP_visual_spotcheck.json`。原 PDF：`pdf/patent_EP3027649B1_TNFa_IL17_bispecific_antibodies.pdf`，SHA `ffb2e5bb7a63a87ddb6f7d3bd6c5978e5004fece2045d836aeef4288130e9295`。

## 五、真实 MinerU 云端验证

### EP 30–33 页：已取得真实制品

- 对比 `cmp-29d990bf7ec4`；远端 batch `cba3bb95-43b1-4498-a692-df22f1f6e3a8`；实际返回 backend=hybrid、版本 3.4.4；请求 model_version=vlm。
- 凭证认证、上传与远端解析均成功，远端 state=done。第一条标准下载路径因本机 CDN TLS/DNS 异常失败；本次复用相同 batch，没有再次提交。
- 本机 DNS 返回 `198.18.*` 代理映射；从 HTTPS DNS 查询取得 CDN 真实地址后，仅验证进程用 curl `--resolve` 临时连接。**TLS 验证保持开启，未改 hosts/DNS/全局代理。**
- ZIP 146415 字节，SHA `05584407fba54a217ef1243dc073ba959f87d22b5246586779088a51f2f5cec1`。
- 为避免重复网络取件，用已取得的真实 batch entry/ZIP 缓存回放适配器，再运行 Docling；这是云端真实产物，不是构造的 OCR fixture。标准产品链的自动网络恢复仍未通过。

| 当前比较服务的同页共享领域规则 | Docling | MinerU |
| --- | --- | --- |
| 输出源页 | 30–33 | 30–33 |
| 恢复带编号序列 | 0 | 7：2、3、4、5、6、8、9 |
| 目视抽查的 2–5 | 未恢复 | 四条均与上节原文相符 |
| 活性数 | 0 | 0（这组是序列表页） |

此比较的 `_domain_pass` 不包含生产任务完整的 native 融合/区域 OCR/关系发布，不能与上文生产任务候选数混算。7 条长度正确也不等于全部逐字符正确；序列 8、9 尚未完整目视核对。当前没有批准 gold，仍不能给出总体准确率或全领域胜负结论。

本次 content_list 的 page_idx 为 0、1、2、3，对应提交页 30–33；只能确认该连续子集，不能据此确认所有非连续/其他 API 版本的页码语义。

可回放制品：

- `reports/f-round-recheck/mineru_EP_30_33_content_list.json`：实际云端内容，移除了认证头和签名下载地址。
- `reports/f-round-recheck/real_cloud_EP_summary.json`：脱敏的结果与制品 SHA。
- `data/acceptance-f-live-20261016-235443/resumed-summary.json`：隔离比较完整结果。
- `data/acceptance-f-live-20261016-235443/comparisons/cmp-29d990bf7ec4/mineru_http/attempt-1/result-package.zip`：原始包。

### 论文第 8–9 页

对比 `cmp-208bbf91bfc4` 两侧均 done。MinerU 使用真实云端响应，并在下载 transport 中临时采用 TLS 校验开启的 DNS 映射；没有使用构造表格代替结果。

| 项目 | Docling | MinerU |
| --- | --- | --- |
| 第 8 页检测到的表格 | 0（有图） | 1，612 单元格，结构异常 |
| 第 9 页检测到的表格 | 1，评分表 | 2，药敏表与评分表 |
| 当前比较链活性产出 | 0 | 0 |
| 原始 PDF 目视事实 | 第 9 页左侧 TABLE 3 为旋转药敏矩阵，右侧 TABLE 4 为评分表 | 同一原文 |

因此“双方 0”不是有效的效果一致证明：Docling 没得到药敏表结构，MinerU 得到了含 S/R/I 的表但后处理未产出活性。生产任务 255 条还依赖完整流程中的其他通道，不能把不同口径混成精确率或召回率。真实失败原因见 P07。

论文 ZIP 394213 字节，SHA `604f2a1e1426024abcadd1ca4321537054049a216c578921dedc97991017a1de`。可回放文件为 `reports/f-round-recheck/mineru_paper_8_9_content_list.json` 和 `real_cloud_paper_summary.json`；完整隔离结果为 `data/acceptance-f-live-20261016-235443/paper-summary.json`。评分表原文也已目视检查，不能把其数字分数当作生物活性。

## 六、下一轮修改与验收顺序

| 编号 | 范围 | 必须提供的验收证据 |
| --- | --- | --- |
| G01 | S01，采用输入完整性 | 篡改任一实际消费文件均拒绝；合法候选真实发布原序列 |
| G02 | S02，多线程事务 | 同一恢复器多比较并发、claim/renew/publish 屏障测试，无事务串扰 |
| G03 | S03，真正的执行预算 | 挂死解析仍准时终态、停止续租、旧结果不可发布 |
| G04 | S04，总体状态原子性 | 双侧完成与旧刷新交错，终态不回退 |
| G05 | S05，比较级远端身份 | 下载失败后真实 retry 只取旧 batch，提交计数仍 1 |
| G06 | P01+P06，ST.25 词法与融合 | 数字冲突保双方；真实带行号标签恢复 SYGVH，保留证据映射 |
| G07 | P02，生产实验条件 | 从真实文档上下文到记录再到 diff，1h/2h 不被判实验相同 |
| G08 | P03，单位语义 | SI 大小写、量纲、数量级按明确规则处理 |
| G09 | P04+P05，坐标到证据 | 真云端 0–1000 转 pt、原点一致、渲染叠框通过；未知框降级贯穿 UI |
| G10 | 效果回归与标注 | 将本次真实制品与目视抽查变成可回放样本；逐条解释未恢复序列；批准 gold 前保持 not_evaluated |
| G11 | P07，药敏表与结构质量 | td-only/旋转表轴语义正确；异常网格产 issue，药敏正例不静默归零，评分负例仍为零 |

这轮应优先补齐上述真实失败输入，而不是仅增加“有字段/有文件/返回成功”的断言。F10 目前完成了诊断工具和草案，**尚未完成足以证明准确率的人工 gold 和效果修复**。

**两轴汇总：Standards 5 项，首要是候选实际消费内容未受完整性保护；Spec 7 项，首要是声明/残基仍可被静默丢失。现有测试全绿，整体验收仍未通过。**
