# 给下一位执行模型的入口

项目根目录：`E:\GoLandCode\Bio-patent-parser`。

请依次阅读：

1. [架构与结果合同](superpowers/specs/2026-10-13-bio-pdf-workflow-design.md)
2. [分阶段执行计划](superpowers/plans/2026-10-13-bio-pdf-workflow-implementation.md)
3. [前期解析器调研](../research/pdf_parser_comparison_2026-10-13.md)
4. [当前样本签名与哈希](../research/corpus_inventory_2026-10-13.json)

用户希望先用 Python 做生物论文/专利 PDF 解析 Demo，重点是序列、SEQ ID、活性数值、单位、实验条件与实体关联，并不断提高效果。后期我们开发的业务层可迁移到 Go/TS；通过 HTTP 调用 Docling/MinerU 时，官方引擎仍有 Python 运行环境。

明确选择：首版普通 Python 状态机 + SQLite + 不可变文件证据，不用 LangChain。LangGraph 只作为以后复杂分支/人工暂停的可选外壳。Docling 和 MinerU通过独立适配器接入，以同样本对测决定主引擎，不预设 Docling 的识别率更高。

当前只有方案和下载样本。根目录没有应用代码，也不是业务 Git 仓库；docling/ 是已经克隆的独立上游源码。不要把业务项目写到上游目录内，不修改上游，不提交模型、PDF、运行数据库或密钥。

首轮请执行计划 Tasks 0-11（M1A-M1C），按里程碑交付。先文件检查、原生证据、真实 Docling 小页解析，再序列/活性规则、审核报告和人工标注 baseline。MinerU、官方序列附件、局部第二路OCR与LLM属于第二期，API界面属于第三期。

重要约束：

- pdf/ 中两份 EP 文件与 probe.pdf 实际是 HTML，必须拒绝并保留原文件；其余七份只有签名检查记录，仍需实际完整性检查。
- 规则检查通过不等于序列正确，首版 auto_accept=false；需要证据、明确来源比对或人工审核。
- 保存源页码/坐标、原生/OCR原文、表格行列、比较符、修饰、未知值，不以最终 Markdown 替代证据。
- 不静默改残基、补数值、串配抗体链、混淆 claim阈值与实测结果。
- 所有接口与模型版本锁定并写入 manifest；实际运行与未运行情况分开报告。
- 不需要为框架选择或常规可逆实现反复确认，按方案推进第一期；若与本地现状冲突，记录并解释调整。

可直接发送给下一位模型的任务：

> 请在 E:\GoLandCode\Bio-patent-parser 实施 docs/BIO_PDF_HANDOFF.md 指向的方案。先完整阅读设计和执行计划，再按 Tasks 0-11 实现首个 Python Demo。采用普通 Python 状态机、SQLite 和文件证据，支持原生 PDF 与 Docling 适配器，完成序列/活性候选、规则校验、待审核导出和评测基线。保留后续 MinerU 对测与 Go/TS 迁移边界。按每个里程碑运行必要检查，并用真实样本制品报告结果；没有人工标注就不声称准确率，没有运行模型就不声称解析完成。
