# K01–K08 执行报告 + 两次 MinerU 真实云端验证

日期：2026-10-19
分支：`codex/scan-ocr-recovery`
基线：`4893aaa`（J01–J06 复核）
最终完整回归：**741 passed, 4 skipped, 0 failed**（含 process tree 慢测试）

本次执行包含三部分：

1. K01–K08 修改方案的全部八项（8 个提交）；
2. 12 页论文的两次真实 MinerU 云端对比（修复前 / 修复后），发现并修复 MinerU 投影层丢弃 figures/captions；
3. 68 页专利的真实对比 + 完整 job，发现并修复三个此前未被测试覆盖的缺陷（K05 在生产中是死路径、K06 sidecar 混入噪声、Docling sparse 页范围 identity 碰撞）。

## 1. K01–K08 提交清单

| 提交 | 任务 | 内容 |
|---|---|---|
| `762bdbb` | K01 | 统一运行时配置入口：`runtime_config.py`（严格 `KEY=VALUE`、未知键拒收、相对路径转绝对、秘密字段屏蔽）、CLI/worker `--env-file`、`.env.example`、子进程继承 |
| `cbb3a16` | K02+K03 | 能力/预检/错误契约（`mineru_http` 规范名 + `mineru` 别名、`configured`/`unavailable_reason`、创建与重试预检 422 零 attempt、HTTP 状态语义映射、`error_info` 持久化）；详情改由 ID 查询，retry 同时失效列表/详情/diff |
| `4512986` | K04 | `remote_identity_store.transition`：对 (fingerprint, generation, owner_token, revision) 做 CAS，短锁 + 唯一临时文件原子替换，历史追加（上限 64） |
| `13d7c7a` | K05 | `declaration_merge.py`：同一 native mention 的 N 条 OCR 行合并为一个逻辑声明，跨行冲突逐条记录 |
| `0a84d00` | K06 | 每字段来源（origin / source_artifact_id / bbox / bbox_verified / location_precision）写入 `region-ocr.json.declaration_merges` |
| `37271bf` | K07 | `norm_identity` 拆分：普通词折叠大小写，SI 单位保留大小写（µM ≠ µm） |
| `56f18f3` | K08 | 预算测试确定性：ready/release 握手（`BIO_PDF_K08_HOLD_DIR`）+ 时钟注入 |
| `1c23e68` | K08 | H01 预算测试改用同一确定性握手 |

## 2. 12 页论文的两次真实对比

文档：`pdf/paper_TAT-RasGAP_broad_antimicrobial_2017.pdf`（12 页，全文）

| 对比 | 说明 | Docling | MinerU |
|---|---|---|---|
| `cmp-be41e1492fda` | 投影修复前 | 467 blocks / 2 tables / 9 figures / 6550 words | 170 blocks / 4 tables / **0 figures** / 5749 words |
| `cmp-d5721ca9a543` | 投影修复后 | 同上 | 184 blocks / 4 tables / **20 figures** / 7258 words |

两次两侧都 `done`，页覆盖 12/12，0 解析错误。MinerU 云端约 50 秒，Docling 本地约 100 秒。

### 2.1 修复：MinerU image/chart 投影（`882fb2e`）

MinerU `content_list` 返回 5 images + 37 charts，标题在 `image_caption` / `chart_caption`，`content` 为空。旧投影走 `if not text: continue`，**figure 与 caption 全部丢失**。

修复后：
- 每个 `image`/`chart` 投影为 Docling 形状的 `pictures`（bbox 从 0–1000 归一化帧换算到源 PDF pt）；
- caption 同时作为**页级**文本块（`bbox=None`、`bbox_verified=False`）回填，不伪造精细坐标，也不会被 `filter_exportable_figures` 当成文字覆盖而反噬 figure；
- 无 bbox 的区域保留 caption、不生成 figure；
- 无视觉区域的 payload 行为不变。

验证：figures 0 → 20，blocks 170 → 184，words 5749 → 7258，FIGURE 1–7 caption 全部回归。

### 2.2 修复：H01 冷启动 flake（`d514905`）

冷启动 Docling 到 READY 实测 **64.6 秒**，而等待上限是 60 秒；完整回归把权重挤出 OS 缓存后必挂。改为 180 秒墙钟上限，执行根死亡时立即失败。H08 happy-path 完成等待同步放宽到 240 秒。

### 2.3 论文上的解析质量评估

| 维度 | Docling | MinerU |
|---|---|---|
| 表格检测 | 2/4（TABLE 2 拆成 196 个文本块，TABLE 3 漏检） | 4/4（TABLE 2 压成 12×392、TABLE 3 表头混入 S/R/I，被结构闸门正确拒绝） |
| 图形 | 9 | 20（修复后，含子图） |
| 跨页段落 | 丢页 4 顶部一段 | 保留 |
| 公式 | 纯文本 | LaTeX（更忠实，但干扰实体匹配） |

序列与活性均为 0 是**正确结果**：论文无 ST.25 声明、无 MIC/IC50 表。

## 3. 68 页专利的验证（`cmp-edb52a8a1e5d` + `job-ba9a9e828e4c`）

文档：`pdf/patent_EP3423477B1_antimicrobial_peptides.pdf`，页 {23,24,26,29,30,31,32}。

### 3.1 修复：Docling sparse 页范围 identity 碰撞（`21088a2`）

请求拆成 3 个连续段，每段 Docling 2.x 导出的 `self_ref` 都从 `#/texts/0` 开始，直接拼接后整侧失败：
`NORMALIZATION_ID_COLLISION block cmp-...:docling:docling:#/texts/0`。

`_merge_run_documents` 现在为每个 item 分配全局唯一 `self_ref`，并在**同一段内**重写 `captions`/`footnotes` 引用。新测试 `tests/unit/test_docling_child_merge.py`（5 条，含同段多 item 场景）。

修复后对比结果（两侧 done，页覆盖 7/7）：

| 指标 | Docling | MinerU |
|---|---|---|
| 序列 | 0 | 1（seq 1，293/549，partial） |
| 观测声明 | 4（located 0 / missing 3） | 4（located 1 / missing 2） |
| 活性测量 | 0 | 0 |

MinerU 在序列恢复上更好。活性为 0 是正确结果：专利 MIC 表是"化合物 × 菌株"网格，列头是菌株名/肽链名，`classify_table` 归为 UNCLASSIFIED。

### 3.2 修复：K05 在生产中是死代码路径（`21088a2`）

`region_ocr.py` 的 `_TAG_LINE_RE` 把所有 ST.25 标签行从 OCR 结果里丢弃（理由"native 已有"）。后果：`_merge_ocr_page_lines` 的声明合并在生产里**永远不会触发**——只有直接注入 OCR 行的单元测试能触发。

已解除过滤。去重仍在 downstream：fingerprint 相等丢副本、互补字段合并为一个声明、值冲突记录 `content_conflict`。

### 3.3 修复：K06 sidecar 混入噪声（`21088a2`）

修复前 `declaration_merges` 有 49 条，全部是 `verdict=incomparable` 的零字段条目（普通 OCR 文本冒充声明）。现在只写非 `incomparable` 的组。

### 3.4 修复后的端到端证据（`job-ba9a9e828e4c`）

native engine + 真实 RapidOCR，页 29–32，`succeeded_with_review`：

- `region-ocr.json.declaration_merges`：**8 条**，全部 `verdict=equal`（`<110>`/`<120>`/`<130>`/`<160>`/`<170>`/`<210> 1`/`<211> 549`/`<212> PRT`）；
- 每条字段带 `origin`、`source_parser`、`source_artifact_id`（`blocks`）、`bbox`、`bbox_verified=True`、`location_precision=block`；
- **seq 2 / seq 3 从 `missing` 恢复为 `complete`**（observed 35 / 37，与 declared 一致）；
- `conflict=0`，无重复 SEQ ID。

### 3.5 K07 的端到端限制

K07 需要"实体 × 指标"指标表（列头 `MIC (µg/mL)`）。这批 9 份 PDF 的活性表都是"化合物 × 菌株"网格，列头不含指标名，无法触发配对。K07 的证据仅为单元测试（`test_table_conditions_k07.py` 5 条 + `test_table_conditions_g07.py` 14 条），它们跑真实生产链路 `_domain_pass_spec → pair_measurements`，只是表结构为注入。

## 4. 环境问题记录

**Meta VPN 劫持 CDN DNS**：开启 Meta Tunnel 时 `cdn-mineru.openxlab.org.cn` 被解析到 `198.18.1.147`（Meta 私有网段 `198.18.0.0/15`），TCP/80 通但 TLS 握手 EOF，表现为
`NetworkTransientError ... SSL: UNEXPECTED_EOF_WHILE_READING`。
MinerU API 本身正常（`state=done`，有 `full_zip_url`）。关闭 VPN 后 DNS 返回真实 CDN，一次通过。此类错误应先查 VPN/DNS，不要怀疑 token 或代码。

**配置加载**：token 通过 `--env-file .env.local` 加载，`.env.local` 已被 `.gitignore` 忽略；公开接口只报告 `configured` 布尔值。

## 5. 测试与交付

| 项目 | 结果 |
|---|---|
| 后端完整回归（含慢测试） | **741 passed, 4 skipped, 0 failed** |
| 4 个跳过 | existing live 开关未开启（3 Docling live + 1 MinerU live） |
| 前端测试 | 37 passed / 8 files |
| 前端 build | 通过（tsc + vite） |
| 新增测试 | `test_runtime_config.py`(17)、`test_table_conditions_k07.py`(5)、`test_process_tree_h08.py`(2)、`test_mineru_figure_projection.py`(7)、`test_docling_child_merge.py`(5) |

### 5.1 本次明确未解决的限制

1. **K07 缺真实语料凭据**：需要一份列头为 `MIC (µg/mL)` 的生物专利。
2. **专利 TABLE 2/3 结构崩坏**：MinerU 把 12 行宽表压成 12×392，表头混入 S/R/I；结构闸门已正确拒绝，但表格内容本身仍不可用，需要人工或区域重解析。
3. **LaTeX 干扰**：MinerU 的公式输出（`$\mathrm{...}$`）会进入文本块，影响下游实体匹配；本次未做 LaTeX 归一化。
4. **无人工 gold**：两次对比都只报告覆盖与一致性，不报告准确率。
