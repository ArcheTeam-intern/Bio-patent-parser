# 第七轮 I01–I08 独立复核与下一轮修改要求

日期：2026-10-17  
复核范围：`271e059..dc7409c`，实际 HEAD：`dc7409c`。  
用途：交给下一位实现者执行；本次只做复核，没有修改业务代码、重启服务或发起新的云端解析。

## 一、结论

本轮修复解决了上一轮的主要复现，独立回归结果与提交报告一致：**676 后端通过、4 跳过；37 前端通过；构建成功**。

但不能据此判定 I01–I08 所有边界均已闭环。本次确认 **6 项残留缺陷**，按 Standards 与 Spec 分别列出。它们来自新边界复现，没有把已披露的 TABLE 3、Docling 药敏为 0、CDN 环境等限制重复算成新缺陷。

| 轴 | 编号 | 优先级 | 已确认的问题 | 影响范围 |
|---|---|---|---|---|
| Standards / 可靠性 | J01 | P2 | 远端终态失败后，显式重试仍永久复用失败批次 | MinerU 比较侧重试 |
| Standards / 可靠性 | J02 | P2 | 测试用全部 `active_children()` 认领根进程 | 同一个 pytest 进程内的其他 multiprocessing 执行单元 |
| Standards / 可靠性 | J03 | P2 | 退出场景脚本缺少 spawn 入口防护 | 退出测试重入、PID 证据归属 |
| Spec / 需求实现 | J04 | P2 | 互补声明仅双行保留，没有真正合并 | 声明字段丢失、序列 inventory 伪冲突 |
| Spec / 需求实现 | J05 | P2 | 原始列上下文仍区分等价的 µ/μ 字形 | 引擎对比中的活性漏配对 |
| Spec / 需求实现 | J06 | P2 | 源页尺寸不匹配时，回放仍将投影坐标标为可信 | 取证脚本；不等同于已证明线上坐标错误 |

P2 表示应安排修复，不表示已观察到生产数据大面积损坏。各项影响边界见下文，尤其不要将 J02 描述为此前的全机进程误杀，也不要将 J04 描述为已绕过完整性审核自动发布。

## 二、本次验证证据

### 2.1 独立执行

后端使用 UTF-8 模式执行：

```powershell
$env:PYTHONUTF8 = '1'
.venv/Scripts/python.exe -m pytest -o addopts= -m 'not live' -q
npm --prefix web run test -- --run
npm --prefix web run build
```

| 项目 | 本次结果 |
|---|---|
| 完整后端，包含进程测试文件 | `676 passed, 4 skipped, 1 warning in 304.61s` |
| 前端 | `37 passed / 8 files` |
| TypeScript + Vite 构建 | 成功，退出 0 |
| Standards 定向回归 | 46 passed |
| Spec 定向回归 | 26 passed |
| 新边界 | J01–J06 均有受控复现；主审重新执行了六项关键探针 |

定向回归是完整回归的子集，不与 676 相加。本次没有核对 4 项跳过用例各自的运行条件，因此不将“676 通过”写成全部 680 项均已实跑通过。

完整后端的工具调用在 300 秒达到 RPC 等待上限，但本地命令继续执行并写出完整日志；随后读取到了上述 304.61 秒的最终 pytest 汇总，也确认对应 pytest 进程已结束。没有将工具层超时当作测试失败，也没有仅凭执行方的历史数字认定通过。

本机临时日志：

- `%TEMP%/bio-I-review-pytest-20261017.log`
- `%TEMP%/bio-I-review-web-test-20261017.log`
- `%TEMP%/bio-I-review-web-build-20261017.log`

### 2.2 已认可的改善

- I02：`slot_expired` 分配失败后不再落入旧批次轮询；未知阶段也有拒绝分支。
- I03：PUT 500/429 保持同一个 allocated slot 重试，原先错误地重分配的问题已修。
- I04：空列表、非正页、候选或文档范围越界的 adopt 输入已有拒绝与零写入回归。
- I05：相同声明的简单字段超集能够进入解析；真实共享字段冲突继续保留双方。
- I06：dose 的 SI 大小写语义、µM/µm 的量纲区分已经改善。
- I07/I08：提交子集顺序到源页号的映射已经用于正常回放，缺失 layout 尺寸不再默认可信 A4；交付的两份回放 JSON 已更新。
- I01：发现器按 ParentProcessId 链向下定位，查询命令不再用引擎入口作全机模糊匹配。J02/J03 是这条测试链剩下的另外两处问题。

## 三、Standards / 可靠性

### J01：远端终态失败后，显式重试无法产生新的解析尝试

**位置**

- `src/bio_pdf/parsers/mineru_adapter.py:530`：读取到远端 failed 状态时直接抛出异常。
- 同文件 `:1283`：调用轮询前把持久化阶段置为 `polling`，终态异常没有另行持久化。
- `src/bio_pdf/application/comparison_service.py:472`、`:772`：显式 retry 重新排队，但后续 attempt 继续共享同一侧的远端 identity。

**复现**

使用现有 `test_mineru_batch_protocol.py` 的 `_make_request` 和 `httpx.MockTransport`：分配一次 b1、PUT 成功，然后 b1 始终返回 `state=failed`。对同一请求连续尝试三次，实测为：

```text
POST /file-urls/batch                  1 次
PUT 同一个 slot                       1 次
GET /extract-results/batch/b1          3 次
异常                                  EngineUnavailableError × 3
持久化 phase                          polling
```

重试路径复用 identity 的代码已核对。探针直接重复调用 adapter，未将它冒充完整 HTTP retry API 的端到端实测。

**影响**

下载超时后复用已完成批次是正确行为；但远端批次已经终态失败时，继续轮询这个不可恢复批次不会重新执行解析。即使用户明确点击重试，当前也没有新的云端尝试。

**修改要求**

1. 将远端执行阶段与本地下载阶段区分：至少识别 `terminal_failed`、可恢复的 polling、已完成待下载。
2. 持久化远端失败状态、错误原因、旧 batch_id 和 attempt 关联，不覆盖历史。
3. 对显式 retry 制定明确策略：可重新执行的远端失败允许在新 generation 分配新批次；确定不可重试的输入错误则明确禁用重试并展示原因。
4. 保持下载失败、GET 临时网络错误、PUT 429/5xx 的现有复用行为，不能把所有错误一律改成重新上传。
5. 新 generation 的创建须受当前 lease/token 围栏保护，防止两个 retry 各自重复提交。

**验收**

- b1 远端 failed → 用户显式 retry → b2 成功；结果与审计记录均能追溯两个批次。
- b1 已完成但下载 503 → retry 仍使用 b1，不新增分配/上传。
- PUT 429/500、slot_expired 原有计数断言继续通过。
- 并发 retry 对同一侧最多产生一个新的有效远端 generation。

### J02：测试根进程的归属仍由全部 active_children() 决定

**位置**：`tests/api/test_process_tree_h01.py:65`、`:105`、`:142`。

`_wait_children()` 收集当前 pytest 进程下全部 multiprocessing 子进程；调用方选第一个作为中继，并在 finally 中清理返回列表中的所有根。

**复现**

在独立探针进程里，只启动一个与 comparison 无关、由探针自有的 `multiprocessing.Process(target=time.sleep)`，不启动 comparison，再调用 `_wait_children(0.5)`：

```text
only_unrelated_mp_child = 41556
returned_roots          = [41556]
unrelated_still_alive   = true
```

探针最后仅通过自己持有的 Process handle 终止并 join，没有调用原测试的清理分支。PID 是本次运行值，不可硬编码为后续验收身份。

**影响边界**

已经修好的 ParentProcessId 后代遍历可以保证“选定根之后”的范围，但不能证明这个根属于本测试。这里影响的是同父的其他 multiprocessing 执行单元或 fixture，不是全机任意生产进程。现有无关 `subprocess.Popen` 哨兵不能覆盖此问题。

**修改要求**

- 在本次 comparison 的启动处捕获准确的 Process handle/PID，绑定 comparison_id、side 和 lease/attempt 身份。
- 可以用测试局部的进程工厂包装或受控启动观察点；不要通过“当前所有孩子”或“第一个孩子”推断身份。
- 等待、取证和清理必须消费同一个已绑定的自有根；清理前核对存活和身份，执行后 join。

**验收**

- 预先启动一个无关 multiprocessing 子进程和一个无关 subprocess；真实预算测试运行后两者都仍存活。
- 只有无关 mp 子进程时，本次 comparison 根发现必须超时/失败，不能返回它。
- 测试真实捕获自有中继及其推理后代，超时后确认该树退出；失败路径也只清理该树。

### J03：退出测试的独立脚本在 Windows spawn 导入时重入

**位置**：`tests/api/test_process_tree_h01.py:194`，`_SCENARIO` 字符串末尾无条件调用 `main()`。

**复现方法与结果**

保留 `_SCENARIO` 原文本，只替换外部的 Repository、ComparisonService 和进程发现依赖；假的 start 使用真实 Windows spawn 启动一个无推理子进程。没有启动真实 Docling 或操作生产服务。

主审复跑得到：

```text
__main__     : repository → service → create → start
__mp_main__  : repository → service → create → start
child target : 已看到子进程写入的 pids.json
父进程继续   : 再次覆盖同一个 pids.json
```

受控探针退出 0，父子进程均已退出。它证明了入口重入和共享取证文件覆盖；**没有证明真实退出测试每次都会产生假阳性**。

**修改要求**

```python
if __name__ == "__main__":
    main()
```

同时把 PID 制品绑定外层 scenario owner PID、随机 run token 和 comparison_id，只允许外层场景写入；外层测试读取时验证身份。子进程 bootstrap 不得创建比较任务或覆盖制品。

**验收**

- 对场景执行真实 spawn，记录 create/start 调用者：必须仅外层各一次。
- child target 执行前没有 bootstrap 额外创建的 comparison 或 PID 文件。
- 恢复真实退出链路，确认记录的自有中继和推理后代由退出清扫实际终止；无关进程存活。
- 不用“清扫函数已被调用”替代真实 PID 退出断言。

## 四、Spec / 需求实现

### J04：互补声明双行保留后，ST.25 解析器丢失前一行字段

**位置**：`src/bio_pdf/workflow/handlers.py:1037–1057`。

`addendum` 分支保留两条声明行，注释认为 parser 会消费全部字段。但 ST.25 的第二个 `<210>` 会结束/重置前一个声明状态。

**最小输入**

同一位置、同一序列号，两个通道分别识别到：

```text
native: <210> 1 <211> 5
OCR:    <210> 1 <212> PRT
随后:   <400> 1
        Ala Gly Leu Lys
```

用现有 `_native` 测试辅助构造块，调用真实 `_merge_ocr_page_lines`，再将融合块送入 `_domain_pass_spec`。主审实测：

```json
{
  "events": ["declaration_addendum"],
  "sequence": {
    "seq_id_no": "1",
    "molecule_type": "protein",
    "declared_length": null,
    "observed_length": 4,
    "normalized_sequence": "AGLK"
  },
  "inventory": {
    "observed_declarations": 2,
    "complete": 0,
    "conflict": 2
  }
}
```

**准确影响**

兼容新增信息没有真正组合成一个声明：长度 5 丢失，后续无法按这个声明长度核对 4 个残基；inventory 又把重复声明记成两项冲突。因此本例不是成功补全，也不是已经证明整项任务错误自动确认为完整。

**修改要求**

1. 按序列号和声明边界合并互补字段，产出一个逻辑声明，而非简单把第二个 `<210>` 再喂给 parser。
2. 逻辑字段保留各自的 native/OCR 证据及原始文本；原始制品保持不可变。
3. 共有字段值冲突继续保留双方并阻止完整性确认；不能用合并掩盖冲突。
4. inventory 应统计逻辑声明，兼容字段补充不产生重复声明伪冲突。
5. 如果选择在 parser 层支持多通道声明补充，也必须限定同一逻辑声明身份，避免吞掉文档中真正重复/冲突的序列号。

**验收必须穿过完整领域链**

- 上例输出 declared_length=5、molecule_type=protein、observed_length=4；不因兼容声明产生两项冲突，但必须保留真实长度不匹配问题。
- 把残基改为 5 个后，一个逻辑声明可通过完整性检查，两通道证据可追溯。
- `<211>5` vs `<211>50`、`<210>1` vs `<210>12` 等原冲突矩阵继续阻断。
- 双通道互换、字段顺序互换、多序列块、跨行字段都要覆盖。
- 不能仅断言“融合文本里两行都还在”。

### J05：等价微符号仍通过 column_context 阻止配对

**位置**：`src/bio_pdf/application/comparison_service.py:192–193`。

单位与条件已做等价字形处理，但配对身份键还包含经 `norm_text` 处理的原始 `row_context/column_context`。该入口仅 lower，没有统一 µ/μ。

**复现**

使用 `tests/unit/test_table_conditions_g07.py::_doc` 生成两个表，caption 都是 `MIC at 10 µM`，实体都为 Peptide A、值都为 4，仅改变表头：

```text
A: MIC (µg/mL)
B: MIC (μg/mL)
```

分别通过 `_domain_pass_spec` 后：

- 两边 `unit_normalized` 都为 `ug/mL`。
- dose 条件都为 `10 µM`；table_anchor 相同。
- `pair_measurements` 却返回 `paired=0`，两侧各 1 条 only；不同键仅是列上下文中的微符号。

**修改要求**

- 给配对身份中的所有语义文本入口统一等价字形规则，或把表头拆为稳定指标/目标/条件等结构身份，原始上下文只用于溯源和消歧。
- 不能为消除本例而粗暴删除全部行列上下文，否则重复菌株、重复指标的不同单元格可能错误合并。
- 不能把 SI 单位全转小写；保留 µM/µm、mM/M 的既有区别。
- 原始文本和两侧证据继续保留。

**验收**

- 上述真实领域链输入变为 paired=1、only_a=0、only_b=0。
- µ/μ 出现在表头、条件和其他身份上下文时规则一致。
- 原大小写量纲反例继续通过；不同实验条件不强配。
- 同实体重复指标的多单元格不被误合并，歧义仍显式报告。

### J06：尺寸核对失败未阻止回放坐标获得可信标记

**位置**：`scripts/regen_cloud_replay.py:128–146`。

脚本计算了 `size_check[page].match`，但 `projection_page_sizes` 仍纳入所有 layout 尺寸。投影和后续 descriptor 又使用同一份 layout 尺寸，导致它们之间自洽却未通过源 PDF 尺寸核验。

**复现**

在临时目录创建：

```text
源 PDF：500 × 700 pt
layout.json：page_idx=0，page_size=[595,842]
content_list：page_idx=0，bbox=[100,100,900,900]
调用 replay_zip(zip_path, source_pdf, [1])
```

主审实测：

```json
{
  "all_sizes_match": false,
  "projection_page_sizes": {"1": [595.0, 842.0, 0.0]},
  "coordinate_basis_verified": true,
  "page_size_verified": true,
  "blocks_without_verified_frame": 0,
  "max_abs_bbox_extent": 757.8
}
```

本例实际投影 y1=757.8，已经超过真实源页高度 700，仍没有未验证块。这里已确认的是回放取证脚本的信任门缺失，不推导为全部线上 MinerU 坐标错误。

**修改要求**

1. 将“协议坐标单位已知”和“相对于源 PDF 的页几何已核验”分开处理。
2. 源 PDF/layout 超出容差时，严格模式直接失败；诊断模式可以继续输出数据，但对应页/块的源坐标可信度必须为 false。
3. 只有匹配通过的源帧才能进入可信映射；不要把 layout 尺寸既作为待核对对象，又作为其自身已核验的依据。
4. 宽高、旋转、源页映射和越界检查分别记录。不要用简单裁剪坐标掩盖尺寸错误。
5. 保留原始 ZIP、layout、源 PDF 哈希及诊断原因，方便人工核对。

**验收**

- 上例严格模式拒绝，或诊断模式明确降级：该页不能显示为源坐标已验证，其块计入未验证数量。
- 尺寸一致的非 A4 非连续页保持可信；缺尺寸页保持未验证。
- 两份固定真实 ZIP 仍正确回放，源页映射不退回子集编号。
- 增加旋转和容差边界测试；不能只比较新输出 JSON 与自己生成的快照。

## 五、建议执行与交付方式

### Standards 任务

先修 J02/J03，让进程测试证据绑定准确身份，再执行真实预算/正常退出用例；J01 单独实现远端终态恢复，并保留已完成批次下载恢复的回归。

### Spec 任务

J04 从融合到 parser、inventory 连续验收；J05 从文档表格到 measurement 配对连续验收；J06 从真实源 PDF 尺寸到投影信任标记连续验收。三者分别提交，避免只修改辅助函数断言而遗漏下游消费。

### 每项交付必须包含

1. 对应问题编号、修改文件与提交号。
2. 本文最小反例修复前后输出。
3. 新增边界回归及原回归结果。
4. 仍未覆盖的范围；工具输出、真实运行和 mock 结果分开说明。

不需要为了完成这六项扩大为新一轮前端重做或更换解析引擎。完成后再统一运行当前完整回归命令。

## 六、保留的效果限制与本轮范围

以下事项仍沿用执行报告中的已披露状态，没有在本轮获得新的充分证据：

- 本机普通 CDN 下载 TLS 环境问题。
- 真实 TABLE 3 的上游网格失真与人工/区域复解析核验。
- Docling 论文药敏仍为 0，不能据此声称两引擎效果相同。
- 其他 API 版本的坐标基准。
- 人工 gold 尚未核准；本次没有得出序列/活性召回率、精准率或“已提取齐全”的结论。

本次没有重新验证旧凭据，也没有用 F 轮的历史序列条数或测试数量作为验收依据。没有新增云端调用，不给出新的 MinerU/Docling 全面胜负结论。

工作区原有 `progress.md` 改动及其他未跟踪文件保持不动；本次交付新增本复核文档。
