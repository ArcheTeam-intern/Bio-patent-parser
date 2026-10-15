import { useEffect, useRef, useState } from "react";
import { documentSourceUrl, type EvidenceData } from "../../api/client";
import { Modal } from "../../components/Modal";
import { precisionLabel } from "../evidence/EvidenceNavigator";
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
  pageNumber: number;
  onPageChange: (page: number) => void;
  /** Current evidence payload for the magnifier (raw text + precision). */
  evidence?: EvidenceData | null;
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

export function PdfViewer({ documentId, target, pageNumber, onPageChange, evidence }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const [pdfDoc, setPdfDoc] = useState<PdfDocument | null>(null);
  const [scale, setScale] = useState(1.4);
  const [fitWidth, setFitWidth] = useState(true);
  const [containerWidth, setContainerWidth] = useState(0);
  const [pageCount, setPageCount] = useState(0);
  const [geometry, setGeometry] = useState<RenderedGeometry | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingPdf, setLoadingPdf] = useState(true);
  const renderTaskRef = useRef<{ cancel(): void } | null>(null);
  // Magnifier: crop of the evidence region, opened by clicking the box.
  const [magnifier, setMagnifier] = useState<string | null>(null);

  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const resize = () => setContainerWidth(wrap.clientWidth);
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(wrap);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    wrapRef.current?.scrollTo({ top: 0, left: 0 });
  }, [documentId, pageNumber]);

  // The workbench owns the page for the PDF, result lists, and text/tables.
  useEffect(() => {
    if (pageCount > 0 && pageNumber > pageCount) onPageChange(pageCount);
  }, [pageCount, pageNumber, onPageChange]);

  // Load the document; release the previous one.
  useEffect(() => {
    let cancelled = false;
    let loaded: PdfDocument | null = null;
    setLoadingPdf(true);
    setError(null);
    setPdfDoc(null);
    setPageCount(0);
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
    if (!pdfDoc || (fitWidth && containerWidth <= 0)) return;
    if (pageNumber > pdfDoc.numPages) return;
    setError(null);
    setGeometry(null);
    let cancelled = false;
    (async () => {
      try {
        const page = await pdfDoc.getPage(pageNumber);
        const canvas = canvasRef.current;
        if (!canvas || cancelled) return;
        const baseViewport = page.getViewport({ scale: 1 });
        const renderScale = fitWidth
          ? Math.max(0.2, Math.min(4, (containerWidth - 24) / baseViewport.width))
          : scale;
        const viewport = page.getViewport({ scale: renderScale });
        const ctx = canvas.getContext("2d");
        if (!ctx) return;
        const dpr = window.devicePixelRatio || 1;
        // Release the canvas before resizing and starting the next render.
        renderTaskRef.current?.cancel();
        canvas.width = Math.floor(viewport.width * dpr);
        canvas.height = Math.floor(viewport.height * dpr);
        canvas.style.width = `${Math.floor(viewport.width)}px`;
        canvas.style.height = `${Math.floor(viewport.height)}px`;
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
          ptWidth: viewport.width / renderScale,
          ptHeight: viewport.height / renderScale,
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
      renderTaskRef.current?.cancel();
    };
  }, [pdfDoc, pageNumber, scale, fitWidth, containerWidth]);

  const bbox = target?.bbox ?? null;
  // Only draw when the CURRENT rendered page matches the target page.
  const overlayReady =
    bbox !== null && geometry !== null && geometry.pageNumber === pageNumber && target?.page === geometry.pageNumber;
  const displayScale = fitWidth && geometry ? geometry.pxWidth / geometry.ptWidth : scale;

  /** Crop the evidence region out of the rendered canvas, enlarged. */
  const openMagnifier = () => {
    const canvas = canvasRef.current;
    if (!canvas || !bbox || !geometry) return;
    const sx = (bbox[0] / geometry.ptWidth) * canvas.width;
    const sy = (bbox[1] / geometry.ptHeight) * canvas.height;
    const sw = Math.max(8, ((bbox[2] - bbox[0]) / geometry.ptWidth) * canvas.width);
    const sh = Math.max(8, ((bbox[3] - bbox[1]) / geometry.ptHeight) * canvas.height);
    // Enlarge small regions to a readable width; cap huge ones.
    const targetW = Math.min(1100, Math.max(560, sw));
    const zoom = targetW / sw;
    const out = document.createElement("canvas");
    out.width = Math.round(sw * zoom);
    out.height = Math.round(sh * zoom);
    const ctx = out.getContext("2d");
    if (!ctx) return;
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(canvas, sx, sy, sw, sh, 0, 0, out.width, out.height);
    setMagnifier(out.toDataURL("image/png"));
  };

  useEffect(() => {
    const wrap = wrapRef.current;
    const box = boxRef.current;
    if (!overlayReady || !wrap || !box) return;
    const region = box.getBoundingClientRect();
    const view = wrap.getBoundingClientRect();
    wrap.scrollTo({
      top: Math.max(0, wrap.scrollTop + region.top - view.top - (wrap.clientHeight - region.height) / 2),
      left: Math.max(0, wrap.scrollLeft + region.left - view.left - (wrap.clientWidth - region.width) / 2),
    });
  }, [overlayReady, target, geometry]);

  return (
    <>
      <div className="pdf-toolbar">
        <span className="seg">
          <button onClick={() => onPageChange(Math.max(1, pageNumber - 1))} disabled={pageNumber <= 1 || loadingPdf}>
            上一页
          </button>
          <span className="seg-label">
            第{" "}
            <input
              type="number"
              aria-label="PDF 页码"
              value={pageNumber}
              min={1}
              max={pageCount || undefined}
              onChange={(e) => {
                const n = Number(e.target.value);
                if (Number.isInteger(n) && n >= 1) onPageChange(Math.min(pageCount || n, n));
              }}
            />{" "}
            / {pageCount || "…"} 页
          </span>
          <button
            onClick={() => onPageChange(Math.min(pageCount, pageNumber + 1))}
            disabled={pageNumber >= pageCount || loadingPdf}
          >
            下一页
          </button>
        </span>
        <span className="spacer" />
        <span className="seg">
          <button title="缩小" onClick={() => { setFitWidth(false); setScale(Math.max(0.4, displayScale - 0.2)); }}>−</button>
          <span className="seg-label">{Math.round(displayScale * 100)}%</span>
          <button title="放大" onClick={() => { setFitWidth(false); setScale(Math.min(4, displayScale + 0.2)); }}>+</button>
        </span>
        <button aria-pressed={fitWidth} onClick={() => setFitWidth(true)}>适合宽度</button>
        <a href={documentSourceUrl(documentId)} target="_blank" rel="noreferrer">
          查看源文件
        </a>
      </div>
      <div className="pdf-canvas-wrap" ref={wrapRef}>
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
          <canvas ref={canvasRef} style={{ visibility: geometry?.pageNumber === pageNumber ? "visible" : "hidden" }} />
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
              ref={boxRef}
              title="点击放大查看该证据区域"
              onClick={openMagnifier}
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

      <Modal open={magnifier !== null} onClose={() => setMagnifier(null)} title="证据区域放大" width={860}>
        {magnifier && (
          <img
            src={magnifier}
            alt="证据区域放大图"
            style={{ maxWidth: "100%", display: "block", margin: "0 auto", border: "1px solid var(--border)", borderRadius: 6 }}
          />
        )}
        {evidence && (
          <div style={{ marginTop: 12 }}>
            <div className="dim">{precisionLabel(evidence)} · 第 {target?.page} 页</div>
            {evidence.raw_text && (
              <div className="mono" style={{ fontSize: 12, lineHeight: 1.7, marginTop: 6, wordBreak: "break-all" }}>
                {evidence.raw_text.length > 600 ? evidence.raw_text.slice(0, 600) + "…" : evidence.raw_text}
              </div>
            )}
          </div>
        )}
      </Modal>
    </>
  );
}
