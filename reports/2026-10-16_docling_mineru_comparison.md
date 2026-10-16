# Docling 与 MinerU 云 API 对比（2026-10-16）

> 真实配置、制品引用、页覆盖、耗时、差异、gold 范围和评测结论。
> **本报告的核心事实：协议与分派已按方案实现并经 MockTransport 测试；真实云实测未执行（本环境无 `MINERU_TOKEN`），耗时/差异/评测全部标注"未实测/未评估"——不伪造任何对比结果。**

## 1. 部署选择与版本（冻结于 C01 基线）

- 选择：沿用 **MinerU 云 API**（用户已确认；本轮不建设自建推理）。Token 仅由服务端环境读取（`MINERU_TOKEN`），不写入前端、不提交、不打印。本环境检查结果：**未配置**。
- 本地冻结版本（`reports/baselines/2026-10-16_eval_baseline.json`）：docling 2.138.0、docling-core 2.101.1、RapidOCR 3.10.0、ONNX Runtime 1.31.0、torch 2.14.1+cpu、pypdfium2（见基线 JSON）。
- MinerU 请求参数：`model_version="vlm"`（默认）；`enable_formula/enable_table/is_ocr/language` 由适配器透传；**云端实际模型版本以远端返回为准，未返回即记未知，不声称完全可复现**。

## 2. 真实引擎分派（已实现）

- `engine=docling|docling_sdk` → DoclingAdapter（本地）；`engine=mineru_http` → MinerUAdapter（云）。
- 缺 token 时：任务创建与 stage 运行**都显式失败**（`ENGINE_UNAVAILABLE`），绝不静默替换成 Docling 却把 manifest 标成 MinerU。
- manifest 记录：engine、model_version、协议（`v4-batch`）、`remote_identity`（batch_id/data_id/file_name/base_url）。制品引用：`jobs/<job>/stage_runs/<run>/run-*/{normalized.json, raw_response.json, manifest.json, remote_identity.json}`。

## 3. 云协议修正（已实现，含可审查制品）

旧实现（方案核查指出的缺陷）：把 `file-urls/batch` 的 PUT 签名 URL 当作"公开 PDF URL"再提交解析端点。新实现（v4 协议）：

```
校验本地源 SHA-256（先于上传）
 → POST /api/v4/file-urls/batch   （保存 batch_id / data_id / 上传槽位）
 → PUT 文件（裸客户端：平台 token 不进入签名 URL 请求）
 → 持久化 remote_identity.json（重试/续跑只轮询，不重建远端任务）
 → GET /api/v4/extract-results/batch/{batch_id}（按 file_name 定位本文件条目）
 → 下载 full_zip_url（同样不带 token）并归一化
```

错误面：401/403→`ENGINE_UNAVAILABLE`；远端 `state=failed`→带 `err_msg` 的失败；轮询超时→`EngineTimeoutError`（远端不取消，可续询）；hash 不符→上传前拒绝（数据不出本机）。

**可审查制品（非 mock 报告的替代品）**：`tests/unit/test_mineru_batch_protocol.py`（5 个 MockTransport 场景：全流程+鉴权头边界、resume 免重提交、401、远端 failed、hash 先拒）。这些证明协议实现正确，**不等于云端效果**。

## 4. 页覆盖 / 耗时 / 差异 / 评测（全部未实测）

| 指标 | 状态 |
| --- | --- |
| 页覆盖（请求/实际/遗漏） | 未实测（无云任务） |
| 排队/上传/解析/后处理耗时 | 未实测 |
| 文本/阅读顺序/表格/图表/序列区域差异 | 未实测 |
| 领域差异（序列/活性/实体/关系） | 未实测 |
| 单元格与图区匹配 | 未实测 |
| gold 范围与字段级准确率 | 未评估（无人工金标；口径见方案 §5.6，双方一致不等于正确） |

补充：方案要求"实验任务不向正式审核队列自动注册审核项"——对比任务编排（C11）未实现，因此不存在污染；正式队列当前仅含 docling/native 任务。

## 5. 配置侧参考（来自既有实测，非本次对比结论）

- Docling 主链路：0.55 GB 模型、纯 CPU、~2 s/页（12 页论文 22.5 s）；EP 全文 195 页 native 解析 + 区域 OCR（10 页预算）单任务数分钟级。
- MinerU 云：无本地模型/无 GPU 需求，但**原文出域**（专利场景合规性需按业务确认——正式对测前必须完成该确认）。
- MinerU 本地自建体量（供将来决策参考，2026-10-16 实测）：VLM 路线 ≈2.3 GB + 强 GPU 依赖；pipeline 模型包整包 15.1 GB。

## 6. 结论

- **已达成**：分派真实（失败即失败）、协议正确（v4 批量 + 身份恢复 + 凭据边界）、归一化诚实（markdown-only 拒绝而非虚构单页）。
- **未达成**：任何真实双引擎数字。下一步（环境就绪后）：配置 `MINERU_TOKEN` → 对三样本各跑一次 `engine=mineru_http` 全文任务 → 用 C11 的对比 API/页面（未实现）或直接用两侧 `/records` 制品做字段级对照；无 gold 的范围保持 `not_evaluated`。
