# Bio PDF Workflow

面向研发调试与人工审核的生物 PDF 解析 Demo：上传论文或专利 PDF，运行解析，
查看序列与活性候选，点击证据定位原文，人工核对并修正，导出带状态与证据的结果。

完整设计见 `docs/superpowers/specs/2026-10-13-bio-pdf-demo-ui-design.md`，
实施入口见 `docs/BIO_PDF_DEMO_UI_HANDOFF.md`。

## Demo 闭环（D0–D4 已交付）

上传 PDF → 创建任务 → 独立 worker 执行完整流水线（源文件检查 → 页分类 →
解析 → 归一化 → 领域提取 → 校验 → 导出）→ 工作台查看原 PDF 与结果联动 →
点击记录定位证据页 → 人工确认/修正/拒绝（原子提交、版本冲突保护）→
按固定结果版本导出 JSON/CSV/FASTA。

没有人工 gold 时不显示准确率；缺坐标的证据明确降级提示，不伪造高亮；
未知残基、区间/误差/比较符完整保留。

## Windows 启动

```powershell
# 1. 环境（Python 3.12 + uv）
uv venv --python 3.12
uv sync --extra dev --extra api

# 2. 前端构建（Node 18+，一次性）
cd web
npm install
npm run build
cd ..

# 3. 配置后端运行时（K01）：复制模板到本地文件并填入凭据
copy .env.example .env.local
#    编辑 .env.local：MINERU_TOKEN=...（可选；不填则 MinerU 侧会明确返回未配置）

# 4. 启动 API + 静态前端（127.0.0.1:8420），通过 --env-file 加载配置
.venv\Scripts\python.exe -m bio_pdf.cli serve --env-file .env.local --data-dir data\demo --web-dist web\dist

# 5. 另开一个终端，启动持久解析 worker（并发 1），加载同一份配置
.venv\Scripts\python.exe -m bio_pdf.workflow.queue_worker --env-file .env.local --data-dir data\demo

# 6. 打开 http://127.0.0.1:8420/ → 任务 → 添加 PDF
```

配置契约（K01）：进程环境 > `--env-file` 指定文件 > 内置默认值；未知键被拒收，
不静默失败。`.env.local` 已在 `.gitignore` 中，**永远不要提交实际凭据**。
公开接口（如 `/api/v1/capabilities`）只报告 `configured` 状态，不返回密钥值。

开发模式（前端热更新）：

```bash
cd web && npm run dev   # Vite 5173，/api 代理到 8420
```

CLI 直接跑完整流水线（不用浏览器）：

```bash
.venv/Scripts/python.exe -m bio_pdf.cli run --input path/to/paper.pdf --data-dir data/demo
```

## API 概要

统一前缀 `/api/v1`；完整交互见 `http://127.0.0.1:8420/docs`（OpenAPI）。

| 资源 | 说明 |
|---|---|
| `POST /documents` | multipart 上传；PDF 签名校验，HTML 伪装 415 |
| `GET /documents/{id}/source` | 原 PDF；Range + SHA-256 ETag |
| `POST /jobs` | 202 入队；worker 认领执行 |
| `GET /jobs/{id}` | 状态、阶段、待审核数、结果版本 |
| `GET /jobs/{id}/records` | 序列/活性（含人工修正后的当前版本） |
| `GET /jobs/{id}/pages/{n}` | 归一化页块与表格 |
| `GET /reviews` · `POST /reviews/{id}/decisions` | 审核队列与原子决策（409 冲突保护） |
| `POST /jobs/{id}/exports` · `GET /exports/{id}` | 按固定 revision 导出 JSON/CSV/FASTA |

## 布局

```
src/bio_pdf/        # 业务包：contracts, ingest, parsers, extract, validation,
                    # workflow(state machine + handlers + queue_worker),
                    # application(job/review/export services), api(FastAPI)
web/                # React + TypeScript + Vite 前端（构建产物 dist/ 由 API 同源提供）
configs/            # TOML profiles
tests/              # unit/ contracts/ application/ api/ integration/
docs/superpowers/   # 设计文档与实施计划
pdf/                # 原始语料（不入库）
data/               # SQLite + 不可变制品（不入库）
```

## 测试

```bash
.venv/Scripts/python.exe -m pytest -m "not live" -q
```

`live` 标记的测试需要真实外部服务（Docling/MinerU），默认跳过。

## 状态

D0–D4 已交付：完整领域流水线、FastAPI + 持久队列 worker、React 工作台、
原子人工审核与版本持久化、按 revision 导出。D5（同源解析器对比与人工
baseline）在 matching 修正、同源检查与人工标注齐备后交付——当前对比页
明确提示不可比较条件，不显示无依据的准确率。
