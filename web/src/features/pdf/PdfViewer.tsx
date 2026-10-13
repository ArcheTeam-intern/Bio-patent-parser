import { useEffect, useRef, useState } from "react";
import { documentSourceUrl } from "../../api/client";

/**
 * PDF viewer with the evidence overlay.
 *
 * Coordinates: the backend's bboxes are PDF points on the *visible* page
 * with a top-left origin (see bio_pdf.pdf.geometry). PDF.js viewports use
 * the same top-left visible-page space, and at scale 1 one CSS px equals
 * one PDF point. Overlay geometry therefore maps pt → px as
 * px = pt * (viewport_px / viewport_pt) using the *real* viewport the page
 * was rendered with — never a guessed 612×792 default. The source page's
 * own /Rotate is already applied by getViewport, so we must not re-apply
 * it here.
 */

export interface EvidenceTarget {
  /** Source page number (1-based). Null → no location evidence at all. */
  page: number | null;
  bbox: [number, number, number, number] | null; // points, top-left origin
}

interface Props {
  documentId: string;
  target: EvidenceTarget | null;
}

interface RenderedGeometry {
  /** CSS px size of the rendered canvas. */
  pxWidth: number;
  pxHeight: number;
  /** Page size in PDF points (viewport at scale 1). */
  ptWidth: number;
  ptHeight: number;
}

type PdfDocument = {
  numPages: number;
  getPage(n: number): Promise<{
    getViewport(opts: { scale: number }): { width: number; height: number };
    render(opts: Record<string, unknown>): { promise: Promise<void> };
  }>;
};

export function PdfViewer({ documentId, target }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [pdfDoc, setPdfDoc] = useState<PdfDocument | null>(null);
  const [pageNumber, setPageNumber] = useState(1);
  const [scale, setScale] = useState(1.4);
  const [pageCount, setPageCount] = useState(0);
  const [geometry, setGeometry] = useState<RenderedGeometry | null>(null);
  const [error, setError] = useState<string | null>(null);

  const effectivePage = target?.page
    ? Math.min(Math.max(target.page, 1), Math.max(pageCount, 1))
    : pageNumber;
  const showTargetPage = target?.page !== null && target !== null && effectivePage !== pageNumber;

  useEffect(() => {
    if (showTargetPage) {
      setPageNumber(effectivePage);
    }
  }, [showTargetPage, effectivePage]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const pdfjs = await import("pdfjs-dist");
        // The query busts a browser cache poisoned by an earlier
        // wrong-MIME response of the same asset path.
        pdfjs.GlobalWorkerOptions.workerSrc =
          new URL("pdfjs-dist/build/pdf.worker.min.mjs", import.meta.url).toString() + "?v=2";
        const doc = await pdfjs.getDocument({ url: documentSourceUrl(documentId) }).promise;
        if (!cancelled) {
          setPdfDoc(doc);
          setPageCount(doc.numPages);
        }
      } catch (e) {
        if (!cancelled) setError(`PDF 加载失败：${String(e)}`);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [documentId]);

  useEffect(() => {
    if (!pdfDoc) return;
    let cancelled = false;
    (async () => {
      const page = await pdfDoc.getPage(pageNumber);
      const canvas = canvasRef.current;
      if (!canvas || cancelled) return;
      const viewport = page.getViewport({ scale });
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.floor(viewport.width * dpr);
      canvas.height = Math.floor(viewport.height * dpr);
      canvas.style.width = `${Math.floor(viewport.width)}px`;
      canvas.style.height = `${Math.floor(viewport.height)}px`;
      const renderTask = page.render({
        canvasContext: ctx,
        viewport,
        transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined,
      });
      await renderTask.promise;
      if (!cancelled) {
        setGeometry({
          pxWidth: Math.floor(viewport.width),
          pxHeight: Math.floor(viewport.height),
          ptWidth: viewport.width / scale,
          ptHeight: viewport.height / scale,
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [pdfDoc, pageNumber, scale]);

  const bbox = target?.bbox ?? null;
  const overlayReady = bbox !== null && geometry !== null && target !== null;

  return (
    <>
      <div className="pdf-toolbar">
        <button onClick={() => setPageNumber((p) => Math.max(1, p - 1))} disabled={pageNumber <= 1}>
          上一页
        </button>
        <span>
          第{" "}
          <input
            type="number"
            value={pageNumber}
            min={1}
            max={pageCount || undefined}
            onChange={(e) => setPageNumber(Number(e.target.value) || 1)}
            style={{ width: 56 }}
          />{" "}
          / {pageCount} 页
        </span>
        <button onClick={() => setPageNumber((p) => Math.min(pageCount, p + 1))} disabled={pageNumber >= pageCount}>
          下一页
        </button>
        <button onClick={() => setScale((s) => Math.max(0.4, s - 0.2))}>−</button>
        <span>{Math.round(scale * 100)}%</span>
        <button onClick={() => setScale((s) => Math.min(4, s + 0.2))}>+</button>
        <a href={documentSourceUrl(documentId)} target="_blank" rel="noreferrer">
          查看源文件
        </a>
      </div>
      <div className="pdf-canvas-wrap">
        {error && <div className="banner error" style={{ margin: 12 }}>{error}</div>}
        {!pdfDoc && !error && <div className="banner info" style={{ margin: 12 }}>正在加载 PDF…</div>}
        <div className="pdf-canvas-inner">
          <canvas ref={canvasRef} />
          {target && target.page === null && (
            <div className="banner warn" style={{ position: "absolute", top: 8, left: 8, zIndex: 2 }}>
              缺少位置证据，无法定位到原文
            </div>
          )}
          {target && target.page !== null && !bbox && (
            <div className="banner warn" style={{ position: "absolute", top: 8, left: 8, zIndex: 2 }}>
              只有页码，无法精确定位（证据缺少坐标）
            </div>
          )}
          {overlayReady && geometry && (
            <div
              className="evidence-box"
              style={{
                left: `${(bbox![0] / geometry.ptWidth) * 100}%`,
                top: `${(bbox![1] / geometry.ptHeight) * 100}%`,
                width: `${((bbox![2] - bbox![0]) / geometry.ptWidth) * 100}%`,
                height: `${((bbox![3] - bbox![1]) / geometry.ptHeight) * 100}%`,
              }}
            />
          )}
        </div>
      </div>
    </>
  );
}
