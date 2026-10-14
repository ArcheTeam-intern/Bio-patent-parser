import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError, createJob, uploadDocument } from "../../api/client";

interface Props {
  open: boolean;
  onClose: () => void;
  onCreated: (jobId: string) => void;
}

export function UploadDialog({ open, onClose, onCreated }: Props) {
  const [file, setFile] = useState<File | null>(null);
  const [parser, setParser] = useState("native");
  const [pages, setPages] = useState("");
  const [docType, setDocType] = useState("auto");
  const [ocrPolicy, setOcrPolicy] = useState("auto");
  const [error, setError] = useState<ApiError | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const queryClient = useQueryClient();

  const uploadAndCreate = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error("请选择一个 PDF 文件");
      setError(null);
      const header = new TextDecoder().decode(await file.slice(0, 1024).arrayBuffer()).trimStart();
      if (!/^%PDF-\d\.\d/.test(header)) {
        const html = /<!doctype html|<html[\s>]/i.test(header);
        throw new ApiError(415, "INPUT_NOT_PDF", html
          ? "文件内容是 HTML 网页，不是真正的 PDF。请在来源网站点击“下载 PDF”，不要将专利详情网页保存为 .pdf。"
          : file.size === 0 ? "文件为空，请重新选择 PDF。" : "文件缺少 PDF 文件头，请重新下载原始 PDF。",
          { file_name: file.name, detected_type: html ? "html" : "unknown" });
      }
      const doc = await uploadDocument(file);
      const pageNumbers = parsePages(pages);
      return createJob({
        document_id: doc.document_id,
        parser,
        page_numbers: pageNumbers,
        targets: ["sequences", "activities"],
        ocr_policy: ocrPolicy,
        document_type_hint: docType,
      });
    },
    onSuccess: (job) => {
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      onCreated(job.job_id);
    },
    onError: (err) => setError(err instanceof ApiError ? err : new ApiError(0, "CLIENT_ERROR", String(err), {})),
  });

  if (!open) return null;

  return (
    <dialog open style={{ display: "block" }}>
      <div className="dialog-title">添加 PDF 并创建解析任务</div>
      <div className="dialog-body">
        {error && (
          <div className="banner error">
            上传失败：{String(error.details.file_name ?? file?.name ?? "")} — {error.message}
            {error.code !== "HTTP_ERROR" && <span className="dim"> ({error.code})</span>}
          </div>
        )}

        <div style={{ display: "grid", gap: 10 }}>
          <label>
            <div className="dim">PDF 文件</div>
            <input
              ref={fileInput}
              type="file"
              accept="application/pdf,.pdf"
              disabled={uploadAndCreate.isPending}
              onChange={(e) => { setFile(e.target.files?.[0] ?? null); setError(null); }}
            />
          </label>

          <label>
            <div className="dim">解析引擎</div>
            <select value={parser} onChange={(e) => setParser(e.target.value)}>
              <option value="native">原生快速（仅文本）</option>
              <option value="docling_sdk">结构增强（Docling，含表格与坐标）</option>
            </select>
          </label>

          <label>
            <div className="dim">页范围（留空为全部，如 1-3,7）</div>
            <input type="text" value={pages} onChange={(e) => setPages(e.target.value)} placeholder="全部" />
          </label>

          <label>
            <div className="dim">文档类型提示</div>
            <select value={docType} onChange={(e) => setDocType(e.target.value)}>
              <option value="auto">自动</option>
              <option value="paper">论文</option>
              <option value="patent">专利</option>
            </select>
          </label>

          <label>
            <div className="dim">OCR 策略</div>
            <select value={ocrPolicy} onChange={(e) => setOcrPolicy(e.target.value)}>
              <option value="auto">自动</option>
              <option value="force">强制 OCR</option>
              <option value="disabled">禁用 OCR</option>
            </select>
          </label>
        </div>
      </div>
      <div className="dialog-actions">
        <button onClick={onClose}>取消</button>
        <button className="primary" disabled={!file || uploadAndCreate.isPending} onClick={() => uploadAndCreate.mutate()}>
          {uploadAndCreate.isPending ? "创建中…" : "创建任务"}
        </button>
      </div>
    </dialog>
  );
}

function parsePages(text: string): number[] | undefined {
  const trimmed = text.trim();
  if (!trimmed) return undefined;
  const out: number[] = [];
  for (const token of trimmed.split(",")) {
    const range = token.match(/^(\d+)-(\d+)$/);
    if (range) {
      for (let i = Number(range[1]); i <= Number(range[2]); i++) out.push(i);
    } else if (/^\d+$/.test(token.trim())) {
      out.push(Number(token.trim()));
    }
  }
  return out.length ? out : undefined;
}
