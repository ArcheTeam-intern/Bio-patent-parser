# 2026-10-14 解析 Demo 运行结果与代码审查

## 范围与结论

审查对象是本机运行中的 Bio Patent Parser Demo，以及任务 `job-8415a1c6daa0` 上传的论文 PDF。文档为 *The Anticancer Peptide TAT-RasGAP317-326 Exerts Broad Antimicrobial Activity*，共 12 页。只读检查，没有修改业务代码，也没有点击审核决定。

**结论：端到端工作流已跑通，适合作为研发原型；当前解析结果尚不足以直接进入生物数据集或自动导出。** 结果明确处于 `succeeded_with_review`，12 条序列候选、83 条活性候选需要审核。人工审核门槛降低了错误记录污染下游的风险，但候选噪声、表格漏检和证据定位问题仍然明显。

## 对上传 PDF 的抽查

| 原文内容 / 位置 | 当前输出 | 判断 |
|---|---|---|
| 第 1 页作者 `Christian Widmann` | 序列候选 `CHRISTIANWIDMANN`，长度 16、类型 unknown | 明确误提取；作者名不是氨基酸序列。候选校验报错且待审核，保护有效，但仍占用审核队列。 |
| 第 8 页药敏表的 `Benzylpenicillin`、`Cefuroxime axetil` 及多种菌名 | 被抽成序列候选 | 明确误提取；表格文本被序列扫描器当成字母串。 |
| 第 1 页 `Accepted: 17 May 2017` | 活性候选 `17 M`，metric unknown | 明确误提取；日期被当成数值和单位。 |
| 第 2 页图注中的 `20 µM`、`95%` | 生成多个 metric unknown 的泛化候选，证据为整段图注 | 数值在 PDF 中存在，但缺目标、实体/实验语境与字段判别；不能据此认定是合格的活性结果，且有重复。 |
| 第 8 页 Table 2 药物敏感性数据 | 归一化结果该页 `tables=0`，导出没有表格记录 | 关键领域信息未结构化，药物与菌种对应的 S/R/I 结果无法供下游查询。 |
| 全文证据 | 153 个 evidence span 中 141 个有 bbox；抽查的作者名证据没有 bbox | 当前 UI 只能定位到页，无法高亮精确文本；来源核验效率低。 |

目前没有人工标注的 gold 数据，因此这些是对单份文档的逐项抽查，不代表总体准确率或召回率。

## 代码审查发现

### 高优先级

1. **点击证据没有传递 bbox。** `web/src/features/workbench/WorkbenchPage.tsx` 的序列、活性和证据点击均调用 `selectRecord(..., null)`；虽然 PDF Viewer 实现了 bbox overlay，工作台没有从 evidence endpoint 取坐标。表格 evidence ID 的页码解析也不兼容当前 ID 格式。因此当前“证据联动”无法精确定位。
2. **Docling 同页多表的 cell ID 可碰撞。** `src/bio_pdf/parsers/normalize_docling.py` 以页码和单表内 cell 下标生成 ID，没有包含 table index。多个表的同序号 cell 会重名；后续按 ID 建索引时可能覆盖，导致值指向另一张表的证据。
3. **所有表格数值都固定标为 `measured`。** `src/bio_pdf/workflow/handlers.py` 调用表格抽取器时无条件传 `evidence_kind="measured"`。专利权利要求阈值或一般说明表也会被标成实验实测，影响筛选与导出语义。
4. **Resume 实际上没有启用恢复路径。** API 将任务重新排队后，`queue_worker.py` 调用 `run_job(job_id)`，而 `JobService.run_job()` 默认 `resume=False`。注释说会校验并复用缓存阶段，但运行路径没有传入 `resume=True`；重跑还可能重建审核项状态。
5. **导出 revision 存在排队竞态。** 导出 API 在入队时记录 revision，后台构建时却重新读取当时的 revision 和记录快照。若审核发生在这两步之间，导出行元数据与生成文件内容可能对应不同 revision。

### 中优先级

- Worker 租约固定 900 秒且运行时没有续租；任务运行超过租期或被重新排队时，可能被另一个 worker 再次领取，产生并行写同一任务制品的风险。
- ST.25 跨页序列只保存一个残基页作为证据；MinerU rowspan 网格投影没有跳过上一行占用的列，复杂合并表头可能造成列错位。
- 工作台窄视口布局较拥挤；URL 中的 `page` 参数刷新后没有恢复到对应 PDF 页。
- 审核写入的 evidence IDs 缺少同一文档/记录范围绑定校验；建议确保修订值与所选证据来自同一任务、同一记录，并持久化证据引用。

## 验证

- `npm run build`：通过（Vite 5.4.21，96 modules）；有一条动态导入与静态导入并存的 chunk 警告。
- `.venv\Scripts\python.exe -m pytest -o addopts='' -m 'not live' -q`：**298 passed, 4 skipped**；1 条 Starlette TestClient/httpx 弃用警告。
- 运行任务状态：`succeeded_with_review`；页数 12；序列候选 12，活性候选 83，待人工审核 95。

## 建议修复顺序

1. 先补强候选过滤：作者/引用/日期/表格语境过滤；序列必须通过序列 alphabet、长度/边界与上下文联合规则后才进入候选。
2. 修复表格检测与结构化：先确保页 8 Table 2 能抽出列头、药物、菌种、S/R/I，并补多层表头/合并单元格覆盖。
3. 统一 evidence ID、页码和 bbox 契约；前端按 evidence ID 加载坐标并实际渲染高亮。
4. 修复 resume、worker lease/fencing、导出 revision 固定快照及审核证据绑定。
5. 用论文和专利的人工标注样本建立 precision/recall 与字段级评测，再决定自动接受阈值。
