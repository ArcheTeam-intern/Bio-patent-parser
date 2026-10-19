import json, httpx
from pathlib import Path
from bio_pdf.parsers.base import ParseRequest
from bio_pdf.parsers.mineru_adapter import MinerUAdapter
import hashlib, tempfile

tmp = Path(tempfile.mkdtemp())
source = tmp / "source.pdf"
source.write_bytes(b"%PDF-1.4 fake")
sha = hashlib.sha256(source.read_bytes()).hexdigest()

calls = {"post": 0, "put": 0}
def handler(request):
    url = str(request.url)
    if request.method == "POST" and url.endswith("/file-urls/batch"):
        calls["post"] += 1
        return httpx.Response(200, json={"data": {"batch_id": "b1", "file_urls": ["https://oss.example/put/s"]}})
    if request.method == "PUT":
        calls["put"] += 1
        return httpx.Response(500)
    raise AssertionError(url)

out = tmp / "jobs" / "job-x" / "stage_runs" / "attempt-1" / "run-0"
out.mkdir(parents=True)
req = ParseRequest(job_id="job-x", document_id=f"sha256:{sha}", source_path=source,
                   page_numbers=(30, 31, 32), profile="layout_ocr", options_ref="x",
                   output_dir=out, parser_options={})
adapter = MinerUAdapter(token="t", transport=httpx.MockTransport(handler), poll_interval=0.0)
try:
    adapter.parse(req, local_file=source)
except BaseException as exc:
    print("raised:", type(exc).__name__, "| code:", getattr(exc, "code", None), "| details:", getattr(exc, "details", None))
print("calls:", calls)
ident = tmp / "jobs" / "job-x" / "mineru_remote_identity.json"
print("identity:", ident.read_text(encoding="utf-8")[:400] if ident.is_file() else "MISSING")
