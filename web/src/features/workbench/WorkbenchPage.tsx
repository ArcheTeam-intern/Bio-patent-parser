import { useEffect, useMemo, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { getJob, getRecords, MeasurementData, SequenceRecordData } from "../../api/client";
import { StatusLabel } from "../../components/StatusLabel";
import { StageStrip } from "./StageStrip";
import { PdfViewer } from "../pdf/PdfViewer";
import { SequenceDetail } from "../sequences/SequenceList";
import { MeasurementDetail, CheckList } from "../activities/ActivityList";
import { ReviewPanel } from "../reviews/ReviewPanel";
import { ExportDialog } from "./ExportDialog";
import { useEvidence } from "../evidence/useEvidence";
import { EvidenceNavigator } from "../evidence/EvidenceNavigator";

type Tab = "sequences" | "activities" | "tables" | "text" | "run";

/** 解析工作台（design §7）：PDF 与领域结果联动，URL 保存视图状态。 */

export function WorkbenchPage() {
  const { jobId } = useParams<{ jobId: string }>();
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as Tab) ?? "sequences";
  const recordId = params.get("record");

  const job = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => getJob(jobId!),
    enabled: !!jobId,
    refetchInterval: (q) => (isActive(q.state.data?.status) ? 1500 : false),
  });
  const records = useQuery({
    queryKey: ["records", jobId],
    queryFn: () => getRecords(jobId!),
    enabled: !!jobId,
  });

  const selected = useMemo(() => {
    if (!records.data || !recordId) return null;
    const seq = records.data.sequences.find((s) => s.sequence_id === recordId);
    if (seq) return { kind: "sequence" as const, record: seq };
    const meas = records.data.measurements.find((m) => m.measurement_id === recordId);
    if (meas) return { kind: "measurement" as const, record: meas };
    return null;
  }, [records.data, recordId]);

  const [exportOpen, setExportOpen] = useState(false);

  // B01: location comes from the evidence API, never from id parsing.
  const selectedEvidenceIds = useMemo(() => {
    if (!selected) return [] as string[];
    const record = selected.record as { evidence_ids?: string[] };
    return record.evidence_ids ?? [];
  }, [selected]);
  const evidenceState = useEvidence(jobId ?? "", selectedEvidenceIds);

  const selectRecord = (id: string) => {
    const next = new URLSearchParams(params);
    next.set("record", id);
    setParams(next, { replace: true });
  };

  // URL state restore: a record in the URL is selected; the evidence hook
  // then resolves the real page/bbox. The PDF page follows the evidence.
  useEffect(() => {
    if (recordId && !selected && (records.data !== undefined)) {
      // Record id no longer exists in the current revision — clear it.
      const next = new URLSearchParams(params);
      next.delete("record");
      setParams(next, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recordId, selected, records.data]);

  if (!jobId) return <div className="banner error">缺少任务 ID</div>;

  return (
    <div>
      <div className="panel" style={{ marginBottom: 8 }}>
        <div className="panel-header">
          <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
            <span className="mono dim">{jobId}</span>
            <StatusLabel value={job.data?.status} kind="job" />
            {job.data && job.data.open_reviews > 0 && (
              <span className="status review">{job.data.open_reviews} 条待审核</span>
            )}
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <span className="dim">
              尚无人工准确率基线；本页展示真实解析结果与审核状态。
            </span>
            <button onClick={() => setExportOpen(true)}>导出</button>
          </div>
        </div>
        {job.data && <StageStrip stages={job.data.stages} />}
      </div>

      <ExportDialog jobId={jobId} open={exportOpen} onClose={() => setExportOpen(false)} />

      <div className="workbench">
        <section className="panel">
          <div className="panel-header">原始 PDF</div>
          {job.data && (
            <PdfViewer documentId={job.data.document_id} target={evidenceState.target} />
          )}
        </section>

        <section className="panel">
          <div className="panel-header">
            <div style={{ display: "flex", gap: 8 }}>
              {(["sequences", "activities", "tables", "text", "run"] as Tab[]).map((t) => (
                <button
                  key={t}
                  style={{ border: "none", background: tab === t ? "var(--accent-soft)" : "none", color: tab === t ? "var(--accent)" : "inherit", fontWeight: tab === t ? 600 : 400 }}
                  onClick={() => {
                    const next = new URLSearchParams(params);
                    next.set("tab", t);
                    setParams(next, { replace: true });
                  }}
                >
                  {{ sequences: "序列", activities: "活性", tables: "表格", text: "原文", run: "运行详情" }[t]}
                </button>
              ))}
            </div>
          </div>
          <div className="panel-scroll">
            {tab === "sequences" && (
              <SequenceTab
                records={records.data?.sequences ?? []}
                selectedId={recordId}
                onSelect={(s: SequenceRecordData) => selectRecord(s.sequence_id)}
                loading={records.isLoading}
              />
            )}
            {tab === "activities" && (
              <ActivityTab
                records={records.data?.measurements ?? []}
                selectedId={recordId}
                onSelect={(m: MeasurementData) => selectRecord(m.measurement_id)}
                loading={records.isLoading}
              />
            )}
            {tab === "tables" && <div className="empty-state">表格视图：结构解析产生表格后在此展示（含行列与合并表头）。</div>}
            {tab === "text" && <div className="empty-state">原文视图：按页展示原生文本与结构解析文本（切换对比）。</div>}
            {tab === "run" && <RunDetails jobId={jobId} />}
          </div>
        </section>

        <section className="panel">
          <div className="panel-header">记录详情与审核</div>
          <div className="panel-scroll">
            {!selected && <div className="empty-state">从中间列选择一条记录查看详情、证据与审核操作。</div>}
            {selected?.kind === "sequence" && (
              <>
                <SequenceDetail record={selected.record} />
                <CheckList checks={selected.record.checks} reasons={selected.record.review_reasons ?? []} />
                <EvidenceNavigator
                  state={evidenceState}
                  fieldEvidence={{ value: selected.record.evidence_ids }}
                />
                <ReviewPanel
                  jobId={jobId}
                  recordId={selected.record.sequence_id}
                  currentRecord={selected.record as unknown as Record<string, unknown>}
                />
              </>
            )}
            {selected?.kind === "measurement" && (
              <>
                <MeasurementDetail
                  record={selected.record}
                  onEvidenceClick={(eid) => evidenceState.select(eid)}
                />
                <EvidenceNavigator
                  state={evidenceState}
                  fieldEvidence={selected.record.field_evidence}
                />
                <ReviewPanel
                  jobId={jobId}
                  recordId={selected.record.measurement_id}
                  currentRecord={selected.record as unknown as Record<string, unknown>}
                />
              </>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}

function isActive(status: string | undefined): boolean {
  return status === "queued" || status === "running";
}

function SequenceTab({
  records,
  selectedId,
  onSelect,
  loading,
}: {
  records: SequenceRecordData[];
  selectedId: string | null;
  onSelect: (r: SequenceRecordData) => void;
  loading: boolean;
}) {
  if (loading) return <div className="empty-state">加载中…</div>;
  if (records.length === 0) return <div className="empty-state">尚未提取到序列候选。</div>;
  return (
    <table className="data">
      <thead>
        <tr>
          <th>SEQ ID</th>
          <th>类型</th>
          <th>声明/观察</th>
          <th>状态</th>
        </tr>
      </thead>
      <tbody>
        {records.map((s) => (
          <tr
            key={s.sequence_id}
            className={`clickable ${selectedId === s.sequence_id ? "selected" : ""}`}
            onClick={() => onSelect(s)}
          >
            <td className="mono">{s.seq_id_no ?? s.sequence_id.slice(-12)}</td>
            <td>{s.molecule_type}</td>
            <td className="dim">
              {s.declared_length ?? "—"}/{s.observed_length}
              {s.declared_length !== null && s.observed_length !== s.declared_length && " ⚠"}
            </td>
            <td>
              <StatusLabel value={s.acceptance_state} kind="record" />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ActivityTab({
  records,
  selectedId,
  onSelect,
  loading,
}: {
  records: MeasurementData[];
  selectedId: string | null;
  onSelect: (r: MeasurementData) => void;
  loading: boolean;
}) {
  if (loading) return <div className="empty-state">加载中…</div>;
  if (records.length === 0) return <div className="empty-state">尚未提取到活性候选。</div>;
  return (
    <table className="data">
      <thead>
        <tr>
          <th>指标</th>
          <th>数值</th>
          <th>靶标</th>
          <th>证据</th>
          <th>状态</th>
        </tr>
      </thead>
      <tbody>
        {records.map((m) => (
          <tr
            key={m.measurement_id}
            className={`clickable ${selectedId === m.measurement_id ? "selected" : ""}`}
            onClick={() => onSelect(m)}
          >
            <td>{m.metric}</td>
            <td className="mono">
              {m.value.kind === "range"
                ? "区间"
                : m.value.raw}{" "}
              {m.unit_raw ?? ""}
            </td>
            <td className="dim">{m.target_raw ?? "—"}</td>
            <td className="dim">{m.evidence_kind}</td>
            <td>
              <StatusLabel value={m.acceptance_state} kind="record" />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function RunDetails({ jobId }: { jobId: string }) {
  return (
    <div style={{ padding: 12 }}>
      <div className="dim">任务 {jobId}</div>
      <div className="dim" style={{ marginTop: 8 }}>
        运行详情包含：引擎、规则版本、配置哈希、请求/实际页集合、阶段耗时、警告与制品清单。
        制品可在任务面板通过 /artifacts 接口按 ID 下载。
      </div>
    </div>
  );
}
