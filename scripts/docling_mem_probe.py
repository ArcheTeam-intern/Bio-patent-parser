"""Measure RSS of the docling parse rig (mirrors docling_adapter.py options).

Usage:
  .venv/Scripts/python.exe scripts/docling_mem_probe.py <pdf> [pages] [ocr]

Reports RSS after imports, after model load, after converting N pages, and the
peak sampled during conversion. `ocr` switches to do_ocr=True with RapidOCR
(onnxruntime), matching a scanned document run.
"""

import sys
import threading
import time

import psutil

proc = psutil.Process()


def rss_mb() -> float:
    return proc.memory_info().rss / 1048576


def report(tag: str) -> None:
    print(f"{tag:42} RSS {rss_mb():7.1f} MB", flush=True)


def main() -> None:
    pdf = sys.argv[1]
    pages = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    do_ocr = len(sys.argv) > 3 and sys.argv[3] == "ocr"
    report("baseline (python)")

    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        PdfPipelineOptions,
        TableStructureOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption

    report("after docling imports")

    options = PdfPipelineOptions()
    options.do_ocr = do_ocr
    options.do_table_structure = True
    options.force_backend_text = not do_ocr
    options.table_structure_options = TableStructureOptions(do_cell_matching=True)
    if do_ocr:
        from docling.datamodel.pipeline_options import OcrMode, RapidOcrOptions

        options.ocr_options = RapidOcrOptions(
            backend="onnxruntime", mode=OcrMode.FULL_PAGE, lang=["ch"], scale=300 / 72
        )

    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )
    converter.initialize_pipeline(InputFormat.PDF)
    report("after model load (no conversion)")

    peak = rss_mb()
    stop = threading.Event()

    def sampler() -> None:
        nonlocal peak
        while not stop.is_set():
            peak = max(peak, rss_mb())
            time.sleep(0.2)

    t = threading.Thread(target=sampler, daemon=True)
    t.start()
    started = time.time()
    result = converter.convert(pdf, page_range=(1, pages))
    stop.set()
    t.join(timeout=2)
    elapsed = time.time() - started
    report(f"after conversion ({len(result.document.pages)} pages, {elapsed:.1f}s)")
    print(f"{'PEAK during conversion':42} RSS {peak:7.1f} MB")


if __name__ == "__main__":
    main()
