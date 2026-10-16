# G01–G11 独立复核与 H01–H09 修改要求

日期：2026-10-17。固定差异：`git diff c0ddf0e...54528c0`，实现提交 `b237c0d`，执行记录提交 `54528c0`。

输入：`reports/2026-10-17_F01_F10_independent_recheck.md`、`reports/2026-10-17_G01_G11_fixes.md`。

## 结论

**有明确改进，仍不能整体验收通过。** 本轮确认 8 项边界缺陷：可靠性 3 项、需求实现 5 项。其中规则宽表被误拒绝、标签数字冲突被覆盖、实验条件因来源 ID 不同无法配对，会直接影响提取和对比结果。

本次只检查代码、运行现有测试、隔离复现和读取已保存的云端 ZIP；没有再次上传云端，没有使用凭证，也未修改业务代码、生产数据或运行服务。隔离进程复现遗留的进程已清理。

## 一、独立验证与已通过项目

| 项目 | 本次结果 |
| --- | --- |
| `.venv/Scripts/python.exe -m pytest -o addopts='' -m 'not live' -q` | **629 passed、4 skipped**，145.97 秒，退出 0 |
| `web` 下 `npm run test -- --run` | **33 passed / 7 files**，退出 0 |
| `web` 下 `npm run build` | TypeScript 与 Vite 构建成功，退出 0 |
| G02/G04/下载失败复用的 4 个针对回归 | 4 passed |
| ST.25 lexer / 条件 / 几何 / 真实回放的针对回归 | 23 passed |

Python 全量测试子进程将 `MINERU_TOKEN` 置空。完整输出保存于本机临时目录的 `bio-G-review-pytest-20261017.log`。下文的隔离探针是现有测试未覆盖的输入与时序，不能由当前全绿结果替代。

确认已落实：

- 候选 txt 与 meta 哈希、有序行文本、源 SHA 等校验已进入实际消费链；原来的单纯修改 meta 后发布 GGGG 的路径已修。
- 比较服务每次数据库操作使用独立短连接；上一轮共享连接串扰的路径已修。
- 总体比较状态已改为单语句派生；旧刷新覆盖终态的路径已有回归。
- **下载失败后**跨 attempt 复用同一 batch 已有实现与通过的回归；S03 是更早的上传阶段断点。
- `50 <400> 1` 的页边计数词法已实现；µM/µm 的大小写差异已保留。
- 已知 content_list 的 0–1000 坐标转换和 span helper 的 False→页级降级已实现；P04 是生产调用入口仍丢标记。
- td-only 表头推断和严重结构拒绝已有实际实现；P01 是质量判断算法误伤合法输入。

## 二、Standards / 可靠性：3 项

### S01 [P1] 超时终止只覆盖直属子进程，推理孙进程继续运行

位置：`src/bio_pdf/application/comparison_service.py:862`；实际下游进程入口 `src/bio_pdf/parsers/docling_adapter.py:244`。

实际链路为比较线程 → spawn 比较进程 → Docling 推理子进程。当前 `proc.terminate()` 只终止中间进程，不能保证清理推理进程。

隔离复现：通过原监督执行函数启动“子进程再启动长时间运行的孙进程”，预算 2 秒。返回 `outcome=timeout` 后，`grandchild_alive_after_budget=true`。现有 `multiprocessing.active_children()` 检查不包括失去父进程的孙进程，因此会给出错误的无残留结论。

修改要求：将实际解析进程树放入可终止的执行范围。Windows 可采用 Job Object 等进程树约束；超时、失租、异常退出和服务关闭均清理同一范围。不能仅在监督进程里补 finally，因为 terminate 不保证执行 finally。

验收：确保 Docling 推理子进程已经启动后再超时；检查实际后代 PID 已退出、停止消耗资源，迟到结果零发布。API 退出场景也覆盖。隔离测试创建的后代必须在失败时自行清理，不能留下测试污染。

### S02 [P1] 采用页不在任务页集时，静默跳过并发布成功

位置：`src/bio_pdf/api/routes/results.py:1519、1548、1564`；`src/bio_pdf/application/job_service.py:268`；`src/bio_pdf/workflow/handlers.py:1395–1402`。

采用 API 接受独立 `request.pages`；创建任务时没有要求其包含候选页。worker 遇到 out-of-scope 候选，只追加诊断后 continue，跳过校验与消费。API 已记录 adopted 并返回 `consumed_by_new_job=True`。

隔离真实 worker：候选元数据页为 2，实际任务仅解析页 1，最终 `succeeded`、review=0、`extract.adopted_candidates=[]`。新任务并未采用候选，但对外成功。

修改要求：

1. API/创建任务时校验候选页属于请求页集和源文档有效页范围；不符合时在创建任务与写 adopted 事件前拒绝。
2. worker 对未消费的必选 adopted input 结构化失败，不能仅记诊断后继续。
3. 区分“已安排采用”和“实际已消费”；消费结果由最终制品证明，不在入队时断言成功。

验收：候选页不在任务范围、超出文档页数、多个候选仅部分在范围三类都明确拒绝；合法候选的真实 worker 仍恢复原序列且记录消费证据。

### S03 [P2] 上传 PUT 响应丢失，已有 batch 未落盘，重试仍重新建批次

位置：`src/bio_pdf/parsers/mineru_adapter.py:1147、1169`；`request_upload_slots()` 内部 PUT 在 `:409`。

当前 request_upload_slots 不只分配 slot，还执行上传；成功返回后调用方才保存 batch 身份。如果服务器已经收到 PDF，但 PUT 响应丢失，已分配的 batch 不会保存。

隔离 MockTransport：第一次 POST 正常返回 batch/slot，第一次 PUT 模拟服务器可能已接收但客户端 ReadTimeout；随后正常 retry，得到 `file_urls=2、puts=2、zip_downloads=1`。下载失败复用已修，不代表上传阶段同样可恢复。

修改要求：分离“分配 slot”和“上传”。拿到 batch/slot 后立即持久化有指纹的身份与 phase，再执行 PUT；恢复时结合 phase、远端状态和 slot 有效期决定继续上传或轮询。保持同 slot PUT 的幂等语义，避免简单重建批次。

验收：POST 响应已得、PUT 响应丢失后重试，批次分配计数仍为 1；还应测进程在身份保存后/上传中/上传完成后退出。无法恢复的过期状态需要明确原因。

## 三、Spec / 需求符合性：5 项

### P01 [P1] 规则宽表被误判为严重不齐，合法活性被整体拒绝

对应 G11。位置：`src/bio_pdf/parsers/mineru_adapter.py:1706–1709`。

`row_ends` 收集每个单元格结束列，而不是每一行的有效最大宽度。完整矩形只要足够宽，也会得到 1、2、3……的“行宽分布”。

隔离输入：完整 2×6 td-only 表，第一行 Drug + 5 个菌株，第二行 Vancomycin + 5 个 S。规范化后被标记 `RAGGED_ROWS_SEVERE: row widths range 1..6`。同一规范化表直接领域提取有 **5 条**，经比较生产链结构门变成 **0 条**。

修改要求：按行计算有效宽度，正确计入 rowspan/colspan 及占位；结构严重程度判断必须基于行之间的差异。规则矩形不能连温和 RAGGED 告警也产生。

验收：完整 5、6、10、20 列的矩形均无严重/温和不齐告警，合法药敏数按单元格正确产出；真实畸形 12×392 和严重不齐原样仍被拒绝并保留 issue。不能通过关闭质量门修复。

### P02 [P1] 标签数字的字符串包含仍绕过来源冲突

对应 G06。位置：`src/bio_pdf/workflow/handlers.py:945–954`。

数字保真 fingerprint 已实现，但后续 covered/supersedes 仍使用普通 substring，令标签 `<210> 1` 被 `<210> 12` 包含。

隔离同框输入：

- native `<210> 12` + OCR `<210> 1`：只留 12，没有任何事件。
- native `<210> 1` + OCR `<210> 12`：自动替换为 12，仅产生 coverage_supersede，没有声明冲突。
- `<211> 50` 与 `<211> 5` 同样。

修改要求：声明行按标签类型和字段值比较；不同序列编号/长度不能走普通文本覆盖路径。保留双方来源并阻止冲突记录自动 complete。段落包含残基行的正常去重继续支持。

验收：1/12、5/50、2/20、10/100 的双方向，以及多标签同一行；不同编号/长度产生明确冲突，完全相同的声明可正常去重。

### P03 [P1] 实验条件匹配键仍包含引擎局部证据身份

对应 G07。位置：`src/bio_pdf/application/comparison_service.py:135–142`。

conditions_key 把条件的全部字段加入键，包括 evidence/block_id/source。条件内容虽已从生产链提取，但本应等价的条件因两个引擎的证据 ID 不同而无法匹配。

生产入口复现：通过现有测试 `_doc` 构造同一文档、表头 `MIC at 1 h (ug/mL)`，分别运行真实 `_domain_pass_spec` 的 docling 和 mineru_http。条件 kind/value 一致，只有 evidence ID 不同，最终 paired=0、only_a/b 各 1。

修改要求：条件语义和来源附件分开。匹配键只由受定义的 kind/value/unit/operator 等实验语义构成；来源 ID、block ID、引擎名、证据引用只用于回溯。不能删除证据来换取配对成功。

验收：生产提取→序列化→配对全链测试；相同 1h、不同证据仍 paired=1，且保留两侧证据；1h/2h 不相等；未核对状态仍如实呈现。

### P04 [P1] 几何可信度在部分真实提取入口再次丢失

对应 G09。位置：

- `src/bio_pdf/extract/activities/prose.py:165–174`
- `src/bio_pdf/extract/entities.py:103–111`
- `src/bio_pdf/extract/sequences/inline.py:361–370、444–453`

这些入口创建 BlockRef 时没有传 bbox_verified。虽然 `_page_blocks` 已带 False，后续构造把它变成默认 None，span helper 的降级规则没有触发。

隔离真实 prose 提取：文本 `The peptide RRRQRRKKRG showed IC50 of 5 uM.`，bbox `[10,20,300,40]`，bbox_verified=False；产出 1 个测量，证据仍是 geometry_precision=block 且有 bbox。直接测 span helper 为 False 的测试不能覆盖此生产路径。

修改要求：统一由块到 BlockRef 的构造入口，完整传递可信度及来源身份；逐一审计 prose/entity/inline/表格及其他生产调用，保留降级原因。

验收：从真实规范化块分别走各领域提取器，False 始终产页级且无框证据，API/UI 同步降级；True 保持正确框，不只重复 helper 单测。

### P05 [P2] 表格结构 issue 在前端没有区域复解析入口

对应 G11 报告的“issue 可走有界区域复解析”。位置：`web/src/features/issues/IssuePanel.tsx:138、180–194`；后端支持集合见 `src/bio_pdf/api/routes/results.py:1208–1214`。

后端已允许 TABLE_STRUCTURE_NO_EXTRACTION、TABLE_STRUCTURE_SUSPECT；前端 REPARSE_SCOPES 仍只有三个序列 reason。用户查看表格 issue 时，canReparse 始终为 false，按钮不会出现。

该项由确定的条件分支核对，本轮未作独立浏览器点击验收。它与“真实坏 HTML 尚未恢复”的已声明限制分开：这里是支持动作在 UI 不可达。

修改要求：由服务端 issue capability 返回可执行动作，前端按 capability 显示，减少维护两份集合。现有端点仍调用 ocr_residue_pages 的文字候选路径，不应把按钮成功等同于表格网格已恢复；能力/文案应明确产物。

验收：真实 table issue 在问题面板出现支持动作；点击后产生该 issue 的候选与审计记录。不可支持的范围仍有清楚原因，禁止硬显示一个无法执行的按钮。

## 四、云端制品取证核对：实现与提交记录需区分

本轮没有再次调用云端。独立读取：

- `data/acceptance-g-live-ep/remote-result.zip`，SHA `f42bbb7a684d814ea255495a9f196197c2dee2921bb1591310f00c4fe465f930`
- `data/acceptance-g-live-paper/remote-result.zip`，SHA `50703ac6669a2b1696e1f691e7e88af103259ec37b2b62e4d0dd50dfda21d1dd`

使用 ZIP 中实际 `layout.json → pdf_info[].page_size`，与从源 PDF 读取的页面尺寸对照，确实六页都在 2 pt 容差内匹配：

| 页 | ZIP page_size | 源 PDF 尺寸 pt | 本次核对 |
| --- | --- | --- | --- |
| EP 30、31、32、33 | 595×842 | 595.22×842.00 | 四页均通过 |
| 论文 8、9 | 595×779 | 595.26×779.52 | 两页均通过 |

但已提交的 `real_cloud_g_ep_replay.json` 与 `real_cloud_g_paper_replay.json` 中，layout_page_size_check 的六页全为 `layout=[null,null], match=false`，与执行报告的通过描述矛盾。

**本次确认页尺寸本身正确，不把它报告成坐标转换失败。** 需要修正取证脚本读取字段及报告 JSON，使机器可读证据与真实核对一致。禁止只手工把 false 改 true；应从固定 ZIP 重新生成并保存 hash/读取字段/容差。

本轮未重新做图形叠框浏览器验收；尺寸通过也不自动证明所有提取入口的证据可信，P04 仍需修。

## 五、继续保留为未完成的已知限制

以下与用户本轮说明一致，不计入新发现：

1. 普通 CDN HTTPS 下载在本机仍有 TLS 失败；不要求把固定 IP 取件写入产品或改变系统网络。
2. 真实 TABLE 3 的上游 HTML 严重失真；拒绝强提合理，不能把无 gold 的猜测关系当恢复成功。
3. Docling 缺少真实药敏表结构、正文方法引用未自动绑定。
4. 其他 API 版本/非连续页族尚缺坐标样本。
5. 人工批准 gold 尚缺，全文缺失序列的预算与解析层问题尚未闭环。

这些限制意味着整体效果验收仍待补全，但不应把它们包装成新回归，重复要求已经正确完成的修复。

## 六、下一轮 H01–H09

| 编号 | 范围 | 验收重点 |
| --- | --- | --- |
| H01 | S01，进程树预算 | 推理孙进程已启动后超时，实际后代全部退出；失租/服务退出同样覆盖 |
| H02 | S02，采用输入范围 | 请求页集覆盖全部候选；越界拒绝；真实消费后才宣称成功 |
| H03 | S03，上传断点恢复 | POST 已返回但 PUT 回应丢失，仍复用同 batch/slot，不重复分配 |
| H04 | P01，结构门修正 | 宽矩形无误报，真实畸形仍拒绝；按行有效宽度验收 |
| H05 | P02，声明字段比较 | 包含型数字冲突保双方，不进入普通 substring 覆盖 |
| H06 | P03，条件身份 | 匹配只看语义，来源附件完整；从生产链开始测 |
| H07 | P04，几何可信度 | 所有领域入口保留 False；证据/API/UI 真实降级 |
| H08 | P05，问题动作可达 | table issue 在浏览器可执行已支持动作，产物与文案一致 |
| H09 | 取证记录 | 从固定 ZIP 重算六页尺寸，JSON 与报告一致，不手改结论 |

建议先把上述失败输入写成锁定回归，再改实现；本轮验收不需要重新提交相同云端文档。最后运行完整测试并使用真实 issue 验证一次浏览器动作。

**两轴汇总：Standards 3 项，首要是超时进程树未清理；Spec 5 项，首要是合法宽表被整体拒绝。现有测试全绿，整体验收未通过。**
