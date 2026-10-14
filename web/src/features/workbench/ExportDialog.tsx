import { useEffect, useState } from "react";
import { createExport, exportDownloadUrl, getExport, getJob, getRecords } from "../../api/client";
import { useQuery } from "@tanstack/react-query";

/**
 * 导出弹窗（design §14）：显示当前版本、格式与预计记录数。
 * 空结果不假装成功——服务端失败时原样展示失败原因。
 */

interface Props {
  jobId: string;
  open: boolean;
  onClose: () => void;
}

export function ExportDialog({ jobId, open, onClose }: Props) {
  const [format, setFormat] = useState<"json" | "csv" | "fasta">("json");
  const [acceptedOnly, setAcceptedOnly] = useState(false);
  const [exportId, setExportId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const job = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => getJob(jobId),
    enabled: open,
  });
  // Same key shape as the workbench so both share one cached response.
  const records = useQuery({
    queryKey: ["records", jobId, job.data?.result_revision, job.data?.status],
    queryFn: () => getRecords(jobId),
    enabled: open,
  });
  const exportInfo = useQuery({
    queryKey: ["export", exportId],
    queryFn: () => getExport(exportId!),
    enabled: !!exportId,
  });

  // Poll until terminal; a plain interval is predictable across versions.
  const [tick, setTick] = useState(0);
  const status = exportInfo.data?.status;
  useEffect(() => {
    if (!exportId || status === "completed" || status === "failed") return;
    const timer = setInterval(() => setTick((t) => t + 1), 600);
    return () => clearInterval(timer);
  }, [exportId, status]);
  useEffect(() => {
    if (tick > 0) exportInfo.refetch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick]);

  useEffect(() => {
    if (!open) {
      setExportId(null);
      setError(null);
    }
  }, [open]);

  if (!open) return null;

  const seqCount = records.data?.sequences.length ?? 0;
  const measCount = records.data?.measurements.length ?? 0;
  const revision = job.data?.result_revision ?? 0;
  const done = exportInfo.data?.status === "completed";
  const failed = exportInfo.data?.status === "failed";

  const start = async () => {
    setError(null);
    try {
      const res = await createExport(jobId, { format, accepted_only: acceptedOnly });
      setExportId(res.export_id);
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <dialog open style={{ display: "block" }}>
      <div className="dialog-title">导出结果</div>
      <div className="dialog-body">
        {error && <div className="banner error">{error}</div>}

        <dl className="detail-grid">
          <dt>结果版本</dt>
          <dd>r{revision}（快照固定，不受后续审核影响）</dd>
          <dt>预计记录</dt>
          <dd>
            序列 {seqCount} 条 · 活性 {measCount} 条
            {acceptedOnly && "（仅已接受）"}
          </dd>
        </dl>

        <label>
          <div className="dim">格式</div>
          <select value={format} onChange={(e) => setFormat(e.target.value as typeof format)}>
            <option value="json">JSON（完整记录 + 检查 + 证据引用）</option>
            <option value="csv">CSV（区间/误差/比较符独立列）</option>
            <option value="fasta">FASTA（仅已接受且完整、可确定的序列）</option>
          </select>
        </label>
        <label style={{ display: "flex", gap: 6, marginTop: 8, alignItems: "center" }}>
          <input type="checkbox" checked={acceptedOnly} onChange={(e) => setAcceptedOnly(e.target.checked)} />
          <span className="dim">仅导出已接受的记录</span>
        </label>

        {exportInfo.data && (
          <div className={`banner ${failed ? "error" : done ? "ok" : "info"}`} style={{ marginTop: 12 }}>
            {failed && `导出失败：${exportInfo.data.error ?? "未知原因"}`}
            {done && `导出完成，共 ${exportInfo.data.record_count} 条记录（r${exportInfo.data.result_revision}）。`}
            {!done && !failed && "导出中…"}
          </div>
        )}
      </div>
      <div className="dialog-actions">
        {done && (
          <a href={exportDownloadUrl(exportInfo.data!.export_id)} target="_blank" rel="noreferrer">
            <button className="primary">下载</button>
          </a>
        )}
        {!exportId && (
          <button className="primary" onClick={start}>
            开始导出
          </button>
        )}
        <button onClick={onClose}>关闭</button>
      </div>
    </dialog>
  );
}
