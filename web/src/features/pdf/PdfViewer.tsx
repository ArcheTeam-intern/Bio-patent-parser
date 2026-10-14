import { useEffect, useRef, useState } from "react";
import { documentSourceUrl } from "../../api/client";
import type { EvidenceTarget } from "../evidence/useEvidence";

/**
 * PDF viewer with the evidence overlay (plan B01).
 *
 * Location geometry comes exclusively from the evidence API payload: the
 * target's page/bbox (backend points, top-left origin on the visible page).
 * Overlay geometry maps pt → px through the *rendered* viewport, so zoom
 * and DPR never offset the box.
 *
 * Race handling: a new page render cancels the previous renderTask; the
 * overlay only appears after the render for the target page has completed,
 * so a stale bbox is never briefly drawn on a freshly rendered page. The
 * source page's own /Rotate is applied by getViewport — never re-applied.
 */

interface Props {
  documentId: string;
  target: EvidenceTarget | null;
}

interface RenderedGeometry {
  pxWidth: number;
  pxHeight: number;
  ptWidth: number;
  ptHeight: number;
  pageNumber: number;
}

type PdfDocument = {
  numPages: number;
  destroy(): Promise<void>;
  getPage(n: number): Promise<{
    getViewport(opts: { scale: number }): { width: number; height: number };
    render(opts: Record<string, unknown>): { promise: Promise<void>; cancel(): void };
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
  const [loadingPdf, setLoadingPdf] = useState(true);
  const renderTaskRef = useRef<{ cancel(): void } | null>(null);

  // Follow the evidence target's page (from the API — never id-parsed).
  useEffect(() => {
    if (target?.page && target.page >= 1) {
      setPageNumber((current) => (current === target.page ? current : target.page!));
    }
  }, [target?.page]);

  // Load the document; release the previous one.
  useEffect(() => {
    let cancelled = false;
    let loaded: PdfDocument | null = null;
    setLoadingPdf(true);
    setError(null);
    setPdfDoc(null);
    setGeometry(null);
    (async () => {
      try {
        const pdfjs = await import("pdfjs-dist");
        pdfjs.GlobalWorkerOptions.workerSrc =
          new URL("pdfjs-dist/build/pdf.worker.min.mjs", import.meta.url).toString() + "?v=2";
        loaded = (await pdfjs.getDocument({ url: documentSourceUrl(documentId) })
          .promise) as unknown as PdfDocument;
        if (cancelled) {
          void loaded.destroy();
          return;
        }
        setPdfDoc(loaded);
        setPageCount(loaded.numPages);
      } catch (e) {
        if (!cancelled) setError(`PDF 加载失败：${String(e)}`);
      } finally {
        if (!cancelled) setLoadingPdf(false);
      }
    })();
    return () => {
      cancelled = true;
      if (loaded) void loaded.destroy().catch(() => undefined);
    };
  }, [documentId]);

  useEffect(() => {
    if (!pdfDoc) return;
    let cancelled = false;
    (async () => {
      try {
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
        // Cancel any in-flight render before starting a new one.
        renderTaskRef.current?.cancel();
        const task = page.render({
          canvasContext: ctx,
          viewport,
          transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined,
        });
        renderTaskRef.current = task;
        await task.promise;
        if (cancelled) return;
        setGeometry({
          pxWidth: Math.floor(viewport.width),
          pxHeight: Math.floor(viewport.height),
          ptWidth: viewport.width / scale,
          ptHeight: viewport.height / scale,
          pageNumber,
        });
      } catch (e) {
        // RenderingCancelledException is expected on fast navigation.
        const name = (e as { name?: string })?.name ?? "";
        if (!cancelled && name !== "RenderingCancelledException") {
          setError(`页面渲染失败：${String(e)}`);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [pdfDoc, pageNumber, scale]);

  const bbox = target?.bbox ?? null;
  // Only draw when the CURRENT rendered page matches the target page.
  const overlayReady =
    bbox !== null && geometry !== null && target?.page === geometry.pageNumber;

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
          / {pageCount || "…"} 页
        </span>
        <button
          onClick={() => setPageNumber((p) => Math.min(pageCount, p + 1))}
          disabled={pageNumber >= pageCount}
        >
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
        {error && (
          <div className="banner error" style={{ margin: 12 }}>
            {error}
          </div>
        )}
        {loadingPdf && !error && (
          <div className="banner info" style={{ margin: 12 }}>
            正在加载源 PDF…
          </div>
        )}
        <div className="pdf-canvas-inner">
          <canvas ref={canvasRef} />
          {target && target.page === null && (
            <div className="banner warn" style={{ position: "absolute", top: 8, left: 8, zIndex: 2 }}>
              缺少位置证据，无法定位到原文
            </div>
          )}
          {target && target.page !== null && !bbox && (
            <div className="banner warn" style={{ position: "absolute", top: 8, left: 8, zIndex: 2 }}>
              页级证据：只有页码，无区域坐标
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
