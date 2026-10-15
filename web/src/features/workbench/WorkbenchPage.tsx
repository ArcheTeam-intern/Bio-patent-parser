import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams, useSearchParams, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { cancelJob, getJob, getRecords, MeasurementData, resumeJob, SequenceRecordData } from "../../api/client";
import { StatusLabel } from "../../components/StatusLabel";
import { WorkerHint } from "../../components/WorkerHint";
import { StageStrip, STAGE_GROUP_ORDER, stageGroup } from "./StageStrip";
import { PageDistribution } from "./PageDistribution";
import { PdfViewer } from "../pdf/PdfViewer";
import { SequenceDetail } from "../sequences/SequenceList";
import { MeasurementDetail, CheckList } from "../activities/ActivityList";
import { ReviewPanel } from "../reviews/ReviewPanel";
import { ExportDialog } from "./ExportDialog";
import { useEvidence } from "../evidence/useEvidence";
import { EvidenceNavigator } from "../evidence/EvidenceNavigator";
import { RunDetails } from "./RunDetails";
import { TablesTab, TextTab } from "../tables/TableView";
import { FiguresTab } from "../figures/FiguresTab";
import { IssuePanel } from "../issues/IssuePanel";

type Tab = "sequences" | "activities" | "tables" | "figures" | "text" | "issues" | "run";

/** 解析工作台（design §7）：PDF 与领域结果联动，URL 保存视图状态。 */

export function WorkbenchPage() {
  const { jobId } = useParams<{ jobId: string }>();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const tab = (params.get("tab") as Tab) ?? "sequences";
  const recordId = params.get("record");
  const rawPage = Number(params.get("page") ?? 1);
  const currentPage = Number.isInteger(rawPage) && rawPage >= 1 ? rawPage : 1;
  const scope = params.get("scope") === "all" ? "all" : "page";

  const job = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => getJob(jobId!),
    enabled: !!jobId,
    refetchInterval: (q) => (isActive(q.state.data?.status) ? 1500 : false),
  });
  const records = useQuery({
    queryKey: ["records", jobId, job.data?.result_revision, job.data?.status],
    queryFn: () => getRecords(jobId!),
    // Records exist only once a run published results; querying earlier
    // (queued/running/cancelled/failed-before-publish) is a guaranteed 400.
    enabled:
      !!jobId &&
      (job.data?.status === "succeeded" ||
        job.data?.status === "succeeded_with_review"),
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
  const queryClient = useQueryClient();
  const cancelMutation = useMutation({
    mutationFn: (id: string) => cancelJob(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["job", jobId] }),
    onError: () => queryClient.invalidateQueries({ queryKey: ["job", jobId] }),
  });
  const resumeMutation = useMutation({
    mutationFn: (id: string) => resumeJob(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["job", jobId] });
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
  });

  // B01: location comes from the evidence API, never from id parsing.
  const selectedEvidenceIds = useMemo(() => {
    if (!selected) return [] as string[];
    const record = selected.record;
    const fields = "field_evidence" in record ? Object.values(record.field_evidence ?? {}).flat() : [];
    return [...new Set([...(record.evidence_ids ?? []), ...fields])];
  }, [selected]);
  const evidenceState = useEvidence(jobId ?? "", selectedEvidenceIds, recordId);

  const changePage = useCallback((page: number) => {
    evidenceState.clear();
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      next.set("page", String(page));
      next.delete("record");
      return next;
    }, { replace: true });
  }, [evidenceState.clear, setParams]);

  useEffect(() => {
    const page = evidenceState.target?.page;
    if (!page || !selected || !evidenceState.evidence ||
        !selectedEvidenceIds.includes(evidenceState.evidence.evidence_id)) return;
    setParams((previous) => {
      if (Number(previous.get("page")) === page) return previous;
      const next = new URLSearchParams(previous);
      next.set("page", String(page));
      return next;
    }, { replace: true });
  }, [evidenceState.target, evidenceState.evidence, selected, selectedEvidenceIds, setParams]);

  const pagesFor = (id: string) => records.data?.record_pages?.[id] ?? [];
  const kindsById = useMemo(() => {
    const out: Record<string, "sequence" | "measurement"> = {};
    for (const s of records.data?.sequences ?? []) out[s.sequence_id] = "sequence";
    for (const m of records.data?.measurements ?? []) out[m.measurement_id] = "measurement";
    return out;
  }, [records.data]);
  const allSequences = records.data?.sequences ?? [];
  const allMeasurements = records.data?.measurements ?? [];
  const visibleSequences = scope === "all" ? allSequences : allSequences.filter(s => pagesFor(s.sequence_id).includes(currentPage));
  const visibleMeasurements = scope === "all" ? allMeasurements : allMeasurements.filter(m => pagesFor(m.measurement_id).includes(currentPage));
  const unlocated = [...allSequences.map(s => s.sequence_id), ...allMeasurements.map(m => m.measurement_id)]
    .filter(id => pagesFor(id).length === 0).length;

  const selectRecord = (id: string) => {
    if (id === recordId) {
      if (selectedEvidenceIds[0]) evidenceState.select(selectedEvidenceIds[0]);
      return;
    }
    evidenceState.clear();
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
            <StatusLabel value={job.data?.status} kind="job" openReviews={job.data?.open_reviews} />
            {job.data && job.data.open_reviews > 0 && (
              <a
                href={`/reviews?job_id=${encodeURIComponent(jobId)}`}
                onClick={(e) => {
                  e.preventDefault();
                  navigate(`/reviews?job_id=${encodeURIComponent(jobId)}`);
                }}
                title="在审核队列中查看本任务的全部待审核记录"
              >
                <span className="status review" style={{ cursor: "pointer" }}>
                  {job.data.open_reviews} 条待审核 ›
                </span>
              </a>
            )}
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <span className="dim">
              尚无人工准确率基线；本页展示真实解析结果与审核状态。
            </span>
            {isActive(job.data?.status) && (
              <button
                disabled={cancelMutation.isPending}
                style={{ borderColor: "var(--red)", color: "var(--red)" }}
                title="取消本次解析（已完成阶段的结果保留在缓存中，重新解析会复用）"
                onClick={() => cancelMutation.mutate(jobId)}
              >
                {cancelMutation.isPending ? "停止中…" : "停止解析"}
              </button>
            )}
            {(job.data?.status === "failed" || job.data?.status === "cancelled") && (
              <button
                disabled={resumeMutation.isPending}
                title="从已完成的阶段缓存继续解析"
                onClick={() => resumeMutation.mutate(jobId)}
              >
                {resumeMutation.isPending ? "排队中…" : "重新解析"}
              </button>
            )}
            <button onClick={() => setExportOpen(true)}>导出</button>
          </div>
        </div>
        {job.data && <StageStrip stages={job.data.stages} />}
      </div>

      <ExportDialog jobId={jobId} open={exportOpen} onClose={() => setExportOpen(false)} />

      <WorkerHint active={isActive(job.data?.status)} />

      <div className="workbench">
        <section className="panel">
          <div className="panel-header">原始 PDF</div>
          {job.data && (
            <PdfViewer documentId={job.data.document_id} target={evidenceState.target}
              pageNumber={currentPage} onPageChange={changePage}
              evidence={evidenceState.evidence} />
          )}
        </section>

        <section className="panel">
          <div className="panel-header">
            <div className="wb-tabs">
              {(["sequences", "activities", "tables", "figures", "text", "issues", "run"] as Tab[]).map((t) => (
                <button
                  key={t}
                  className={`wb-tab${tab === t ? " active" : ""}`}
                  onClick={() => {
                    const next = new URLSearchParams(params);
                    next.set("tab", t);
                    setParams(next, { replace: true });
                  }}
                >
                  {{ sequences: "序列", activities: "活性", tables: "表格", figures: "图表", text: "原文", issues: "问题", run: "运行详情" }[t]}
                </button>
              ))}
            </div>
          </div>
          <div className="wb-scope">
            {(tab === "sequences" || tab === "activities") ? (
              <>
                <div className="wb-scope-row">
                  <label>
                    结果范围：{" "}
                    <select aria-label="结果范围" value={scope} onChange={e => {
                      const next = new URLSearchParams(params);
                      next.set("scope", e.target.value);
                      setParams(next, { replace: true });
                    }}>
                      <option value="page">当前页</option>
                      <option value="all">全部文档</option>
                    </select>
                  </label>
                  <span className="wb-count">
                    {scope === "page" ? `第 ${currentPage} 页 · ` : "全文 · "}
                    {tab === "sequences" ? `${visibleSequences.length}/${allSequences.length} 条序列` : `${visibleMeasurements.length}/${allMeasurements.length} 条活性`}
                  </span>
                </div>
                {scope === "page" && <div className="wb-hint">随左侧 PDF 翻页；点击记录可定位证据。跨页记录会在各证据页显示。</div>}
                {unlocated > 0 && <div className="wb-hint">另有 {unlocated} 条记录缺少可定位页码，可在“全部文档”查看。</div>}
              </>
            ) : <span className="wb-count">{tab === "tables" || tab === "text" ? `第 ${currentPage} 页 · 与左侧 PDF 同步` : "全文视图"}</span>}
          </div>
          <div className="panel-scroll">
            {records.error && (tab === "sequences" || tab === "activities") && <div className="banner error">记录读取失败：{String(records.error)}</div>}
            {isActive(job.data?.status) && (
              <div className="banner info">
                {job.data?.status === "running" && job.data?.current_stage ? (
                  <>
                    解析中：
                    {stageGroup(job.data.current_stage)?.label ?? job.data.current_stage}
                    （第{" "}
                    {STAGE_GROUP_ORDER.indexOf(stageGroup(job.data.current_stage)?.key ?? "") + 1}
                    /{STAGE_GROUP_ORDER.length} 步）—— 完成后结果自动更新；点右上“停止解析”可取消。
                  </>
                ) : job.data?.status === "running" ? (
                  "解析进行中，结果将在完成后更新。"
                ) : (
                  "排队中，等待解析进程（worker）领取。"
                )}
              </div>
            )}
            {(tab === "sequences" || tab === "activities" || tab === "tables") &&
              records.data &&
              !isActive(job.data?.status) && (
                <div style={{ padding: "12px 14px 6px", borderBottom: "1px solid var(--border-soft)" }}>
                  <PageDistribution
                    pageCount={0}
                    recordPages={records.data.record_pages ?? {}}
                    kinds={kindsById}
                    currentPage={currentPage}
                    onJump={changePage}
                    tableCounts={tab === "tables" ? records.data.table_pages : undefined}
                  />
                </div>
              )}
            {(tab === "sequences" || tab === "activities") && !records.data && (
              <div className="empty-state">
                <div>
                  该任务
                  {job.data?.status === "cancelled"
                    ? "已取消"
                    : job.data?.status === "failed"
                      ? "解析失败"
                      : "尚未完成"}
                  ，还没有可展示的结果。
                </div>
                {(job.data?.status === "cancelled" || job.data?.status === "failed") && (
                  <div className="dim">点右上“重新解析”可从已完成的阶段缓存继续。</div>
                )}
              </div>
            )}
            {tab === "sequences" && records.data && (
              <SequenceTab
                records={visibleSequences}
                emptyMessage={scope === "page" ? `第 ${currentPage} 页没有序列候选。可切换“全部文档”查看其他页。` : "全文尚未提取到序列候选。"}
                selectedId={recordId}
                onSelect={(s: SequenceRecordData) => selectRecord(s.sequence_id)}
                loading={records.isLoading}
              />
            )}
            {tab === "activities" && records.data && (
              <ActivityTab
                records={visibleMeasurements}
                emptyMessage={scope === "page" ? `第 ${currentPage} 页没有活性候选。可切换“全部文档”查看其他页。` : "全文尚未提取到活性候选。"}
                selectedId={recordId}
                onSelect={(m: MeasurementData) => selectRecord(m.measurement_id)}
                loading={records.isLoading}
              />
            )}
            {tab === "tables" && (
              <TablesTab jobId={jobId} pageNumber={currentPage} />
            )}
            {tab === "text" && <TextTab jobId={jobId} pageNumber={currentPage} />}
            {tab === "figures" && <FiguresTab jobId={jobId} onJump={changePage} />}
            {tab === "issues" && <IssuePanel jobId={jobId} />}
            {tab === "run" && <RunDetails jobId={jobId} job={job.data} />}
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
  emptyMessage,
}: {
  records: SequenceRecordData[];
  selectedId: string | null;
  onSelect: (r: SequenceRecordData) => void;
  loading: boolean;
  emptyMessage: string;
}) {
  if (loading) return <div className="empty-state">加载中…</div>;
  if (records.length === 0) return <div className="empty-state">{emptyMessage}</div>;
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
  emptyMessage,
}: {
  records: MeasurementData[];
  selectedId: string | null;
  onSelect: (r: MeasurementData) => void;
  loading: boolean;
  emptyMessage: string;
}) {
  if (loading) return <div className="empty-state">加载中…</div>;
  if (records.length === 0) return <div className="empty-state">{emptyMessage}</div>;
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
