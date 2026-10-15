"""Query download sizes of every Docling model repo (via hf-mirror.com API).

Usage: .venv/Scripts/python.exe scripts/docling_model_inventory.py

One row per repo with total download bytes; the "full-blood" Docling set is the
union of all rows. Sizes are whole-repo because download_hf_model() calls
snapshot_download() without allow_patterns. Huggingface.co is unreachable from
this network; hf-mirror.com is used instead (needs a browser-ish User-Agent).
"""
import json
import urllib.request

REPOS = [
    # layout engines (pick one at runtime)
    ("layout", "docling-project/docling-layout-heron", "默认版面引擎"),
    ("layout", "docling-project/docling-layout-heron-101", "heron 101 类"),
    ("layout", "docling-project/docling-layout-heron-onnx", "heron ONNX 运行时"),
    ("layout", "docling-project/docling-layout-egret-medium", "egret medium"),
    ("layout", "docling-project/docling-layout-egret-large", "egret large"),
    ("layout", "docling-project/docling-layout-egret-xlarge", "egret xlarge"),
    ("layout", "docling-project/docling-layout-old", "旧版版面（legacy）"),
    # tables
    ("table", "docling-project/docling-models", "TableFormer v1 accurate+fast + 旧版面"),
    ("table", "docling-project/TableFormerV2", "TableFormer v2"),
    # pipeline extras (default-on)
    ("extra", "docling-project/CodeFormulaV2", "公式识别 code_formula"),
    ("extra", "docling-project/DocumentFigureClassifier-v2.5", "图片分类器"),
    # VLMs (optional)
    ("vlm", "ibm-granite/granite-docling-258M", "granite-docling 258M VLM"),
    ("vlm", "ibm-granite/granite-docling-258M-mlx", "granite-docling MLX（Mac）"),
    ("vlm", "docling-project/granite-docling-2stage-258m", "granite-docling 两阶段 258M"),
    ("vlm", "docling-project/SmolDocling-256M-preview", "SmolDocling 256M"),
    ("vlm", "docling-project/SmolDocling-256M-preview-mlx-bf16", "SmolDocling MLX（Mac）"),
    ("vlm", "HuggingFaceTB/SmolVLM-256M-Instruct", "smolvlm 图片描述"),
    ("vlm", "ibm-granite/granite-vision-3.2-2b", "granite-vision-3.2-2b（图片描述）"),
    ("vlm", "ibm-granite/granite-vision-3.3-2b-chart2csv-preview", "图表抽取 chart2csv"),
    ("vlm", "ibm-granite/granite-vision-4.1-4b", "granite-vision-4.1-4b（图表抽取 v4）"),
    # OCR (optional)
    ("ocr", "nanonets/Nanonets-OCR2-3B", "Nanonets OCR2 3B"),
    ("ocr", "nvidia/nemotron-ocr-v2", "Nemotron-OCR v2"),
    ("ocr", "opendatalab/MinerU2.5-Pro-2604-1.2B", "MinerU2.5-Pro VLM（docling 引用）"),
]

print(f"{'类别':6} {'仓库':52} {'下载':>10}  说明")
print("-" * 110)
grand = 0
results = []
for kind, repo, note in REPOS:
    url = f"https://hf-mirror.com/api/models/{repo}?blobs=true"
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.4.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.load(r)
        total = 0
        for s in d.get("siblings", []):
            sz = s.get("size") or (s.get("lfs") or {}).get("size") or 0
            total += sz
        results.append((kind, repo, total, note))
        grand_once = total
        print(f"{kind:6} {repo:52} {total/1e6:8.1f} MB  {note}")
    except Exception as e:  # noqa: BLE001
        print(f"{kind:6} {repo:52} {'ERR':>10}  {note} ({type(e).__name__}: {e})")

print("-" * 110)
print(f"全部仓库合计: {sum(r[2] for r in results)/1e6:.1f} MB")
