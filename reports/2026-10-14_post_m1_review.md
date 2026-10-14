# M0/M1 执行后独立核查

核查基线：HEAD `cab949a`。核查两份执行报告、现有源码、新任务 `job-95b61e00997d` 当前发布制品，并复跑非 live 测试。本轮未修改业务代码或审核决定。

## 结论

作者/药物/日期误提已改善，两段真实短序列已找到；工程基础有进展。但当前 M0/M1 是部分完成，尚有前后端断联、重复提取、混合文档未实际 OCR、假字符精度、表格指标串列及不完整 fencing。不能只用 95→25 条审核数量下降作为精度提升依据。

## 当前制品与报告差异

- 5 条序列、20 条活性与报告一致；当前 extract 中是 **47 条 evidence span**，不是报告的70条。extract SHA-256：`8784eae6eb3999f6c65675f353d684ae4678b17cbc033455b0b18e84b41e2a4e`。
- RRRQRRKKRG 与 DTRLNTVWMW各有两个候选，**全部在第9页**。来源是同一位置的Docling方法段落与native文本行，不是摘要与正文各一处。
- 两个Docling候选的bbox都覆盖同一段695字符方法描述；有精确字符offset，但没有对应的字符级bbox。`location_precision=character`表述过强。
- 当前 normalized 中有229个跨页重复block ID，最高重复12次，主要为native:rN。
- 20条活性metric全部unknown，每条仅value字段有证据；缺实体/靶标/assay链接。
- TABLE_UNCLASSIFIED目前仅写diagnostics，ValidateStage只给sequence/measurement建审核项。报告所说已建表级语义审核项尚未实现。

## 确定缺陷与复现

| 优先级 | 文件/位置 | 现象 |
|---|---|---|
| P1 | web/src/features/workbench/WorkbenchPage.tsx：pageOf/onSelect | 前端仍从`:pN:` ID取页码并传bbox=null；新ev-hash无法取页，证据定位回归 |
| P1 | extract/evidence.py：span_from_block_match；pdf/native.py：_textpage_lines | block bbox被标character；native ID没有页号；证据hash没有页号，跨页存在碰撞风险 |
| P1 | parsers/fusion.py：fuse_documents/_aligned_native | 一个layout段落最多匹配一个native行，剩余重叠行追加进主文；产生重复提取，并未真正保留完整字符冲突集合 |
| P1 | workflow/handlers.py：LayoutParseStage.run | 部分页面需OCR时仍执行layout_native；parser=docling_serve/mineru_http也调用DoclingAdapter，配置与执行引擎不一致 |
| P1 | config.py / parsers/docling_adapter.py | tableformer_mode/ocr_lang/timeout等入配置，却没有传入真正converter；缓存hash变化不代表参数生效 |
| P1 | workflow/queue_worker.py / runner.py / handlers.py | lease_guard只看延迟更新lost标志；阶段完成、review写入、status更新和文件发布不在带token的事务中，失租后仍可能写共享状态 |
| P1 | workflow/runner.py：_cache_is_intact | 核验输出hash，但没有核验stage manifest本身hash；可篡改manifest同时改输出hash后绕过 |
| P1 | storage/result_reader.py：read_published_manifest/read_json | 损坏或缺entry的published manifest可静默回退legacy，没有固定baseline ID |
| P1 | api/routes/exports.py / application/export_service.py | 入队记录revision，后台再次读current；原来的导出revision竞态仍存在，固定快照已完成的声明不成立 |
| P1 | extract/activities/tables.py / metrics.py | 混合MIC/MBC表被全表同一canonical metric覆盖；record evidence ID与span_from_cell的ev-hash不同 |
| P1 | extract/activities/mentions.py / prose.py | 整句优先级角色分类误删真实结果；undetermined仍生成unknown Measurement |
| P2 | extract/sequences/st25.py：parse_st25_blocks | 只在含210/400 tag的block记页，纯续页不记录；每页证据使用同一raw[:400]，不是该页真实片段 |
| P2 | contracts/compat.py | v1旧schema仍被拒；对任意非ent:前缀ID作legacy迁移，无版本边界；compat没有进入统一读取路径 |

### 在内存中直接复现的领域错误

输入 `Peptide | MIC (uM) | MBC (uM)`，数据 `A | 8 | 16`：classifier选择MBC，得到 **MBC=8、MBC=16**，正确应为MIC=8、MBC=16。

表格record引用 `doc:cell:c11`，实际span_from_cell生成 `ev-e2882a3ea9a10e33a5d9`，并非同一ID。

- `Figure 1 shows an IC50 of 5 nM.` → 0条结果。
- `Compound A had an IC50 of 1.2 ± 0.3 nM (mean ± SD).` → 0条结果。
- `Cells were treated with 20 µM peptide A, reducing viability to 5%.` → 0条结果，处理浓度和真实结果一起被排除。
- `The MIC of peptide A was less than 10 µM.` → unknown/claim_threshold。论文中的实验上界不应仅因less than就变成专利权利要求。

以上调用不需要外部引擎、人工gold或应用写入，现有实现即可稳定复现。

## 验证边界与延期判断

- 本轮实测：**351 passed、4 skipped、1条Starlette弃用警告**。测试通过，但没有覆盖上述跨模块情况。
- 没有人工gold只会阻止正式准确率报告，不阻止完成T15匹配器、覆盖范围、负例与评测CLI。
- 没有当前论文的真实药敏结构不阻止T09多层表头算法和确定性结构夹具；T09与T16不应相互等待。
- 项目已存在多份PDF签名有效的专利。抽查首3页及末12页未检出ST25文本标签，不能据此称无专利样本，也不能声称其清单已识别；需页发现与渲染核验。
- 后续实施顺序见 `docs/superpowers/plans/2026-10-14-bio-pdf-post-m1-remediation.md`。
