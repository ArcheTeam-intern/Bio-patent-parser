import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { useEffect, useState } from "react";
import { cancelJob, deleteJob, getSamples, listJobs, resumeJob } from "../../api/client";
import { StatusLabel } from "../../components/StatusLabel";
import { WorkerHint } from "../../components/WorkerHint";
import { UploadDialog } from "./UploadDialog";

/** 任务列表：上传、样本导入、观察状态（design §6）。 */

export function JobsPage() {
  const [uploadOpen, setUploadOpen] = useState(false);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const navigate = useNavigate();

  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: listJobs,
    refetchInterval: (q) => (activeJobs(q.state.data) ? 1500 : false),
  });
  const samples = useQuery({ queryKey: ["samples"], queryFn: getSamples });
  const queryClient = useQueryClient();
  const cancelMutation = useMutation({
    mutationFn: (id: string) => cancelJob(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
    onError: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  });
  const resumeMutation = useMutation({
    mutationFn: (id: string) => resumeJob(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  });
  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteJob(id),
    onSuccess: () => {
      setConfirmingId(null);
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      queryClient.invalidateQueries({ queryKey: ["reviews"] });
    },
  });

  // 确认态自动过期，避免误点后一直悬在"确认删除"。
  useEffect(() => {
    if (!confirmingId) return;
    const t = setTimeout(() => setConfirmingId(null), 4000);
    return () => clearTimeout(t);
  }, [confirmingId]);

  const jobsList = jobs.data ?? [];

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <h2 style={{ margin: 0, fontSize: 16 }}>任务列表</h2>
        <button className="primary" onClick={() => setUploadOpen(true)}>
          添加 PDF
        </button>
      </div>

      <WorkerHint active={activeJobs(jobsList)} />

      {deleteMutation.isError && (
        <div className="banner error" style={{ marginBottom: 8 }}>
          删除失败：{String((deleteMutation.error as Error)?.message ?? deleteMutation.error)}
          （活动中任务需先"停止"）
        </div>
      )}

      {jobsList.length === 0 && !jobs.isLoading ? (        <EmptyJobs samples={samples.data?.samples ?? []} onUpload={() => setUploadOpen(true)} />
      ) : (
        <div className="panel">
          <table className="data">
            <thead>
              <tr>
                <th>任务</th>
                <th>状态</th>
                <th>待审核</th>
                <th>当前阶段</th>
                <th>创建时间</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {jobsList.map((job) => (
                <tr key={job.job_id} className="clickable" onClick={() => navigate(`/jobs/${job.job_id}/workbench`)}>
                  <td>
                    <div className="mono dim">{job.job_id}</div>
                    <div className="dim">{job.document_id.slice(0, 20)}…</div>
                  </td>
                  <td>
                    <StatusLabel value={job.status} kind="job" openReviews={job.open_reviews} />
                  </td>
                  <td>{job.open_reviews > 0 ? <span className="status review">{job.open_reviews} 条</span> : "—"}</td>
                  <td className="dim">{job.current_stage ?? "—"}</td>
                  <td className="dim">{new Date(job.created_at).toLocaleString()}</td>
                  <td>
                    <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
                      {(job.status === "queued" || job.status === "running") && (
                        <button
                          disabled={cancelMutation.isPending}
                          style={{ borderColor: "var(--red)", color: "var(--red)" }}
                          title="取消解析；已完成阶段的缓存会保留，重新解析可复用"
                          onClick={(e) => {
                            e.stopPropagation();
                            cancelMutation.mutate(job.job_id);
                          }}
                        >
                          停止
                        </button>
                      )}
                      {(job.status === "failed" || job.status === "cancelled") && (
                        <button
                          disabled={resumeMutation.isPending}
                          title="从已完成阶段的缓存继续解析"
                          onClick={(e) => {
                            e.stopPropagation();
                            resumeMutation.mutate(job.job_id);
                          }}
                        >
                          重新解析
                        </button>
                      )}
                      {/* 删除只对终止态任务开放（删除结果+审核项+工件；
                          活动中任务须先"停止"）。两段确认避免误删。 */}
                      {job.status !== "queued" && job.status !== "running" && (
                        confirmingId === job.job_id ? (
                          <>
                            <button
                              disabled={deleteMutation.isPending}
                              style={{ borderColor: "var(--red)", color: "var(--red)" }}
                              title="确认删除：结果、审核项与工件一并移除，源文档保留"
                              onClick={(e) => {
                                e.stopPropagation();
                                deleteMutation.mutate(job.job_id);
                              }}
                            >
                              {deleteMutation.isPending ? "删除中…" : "确认删除？"}
                            </button>
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                setConfirmingId(null);
                              }}
                            >
                              取消
                            </button>
                          </>
                        ) : (
                          <button
                            style={{ color: "var(--red)" }}
                            title="删除任务：结果、审核项与工件一并移除（源文档保留）"
                            onClick={(e) => {
                              e.stopPropagation();
                              deleteMutation.reset();
                              setConfirmingId(job.job_id);
                            }}
                          >
                            删除
                          </button>
                        )
                      )}
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          navigate(`/jobs/${job.job_id}/workbench`);
                        }}
                      >
                        打开工作台
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <UploadDialog
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        onCreated={(jobId) => {
          setUploadOpen(false);
          navigate(`/jobs/${jobId}/workbench`);
        }}
      />
    </div>
  );
}

function activeJobs(data: { status: string }[] | undefined): boolean {
  return !!data?.some((j) => j.status === "queued" || j.status === "running");
}

function EmptyJobs({ samples, onUpload }: { samples: { sample_id: string; file_name: string; category: string; available: boolean }[]; onUpload: () => void }) {
  const categoryLabel: Record<string, string> = { paper: "论文", patent: "专利", scan: "扫描件" };
  return (
    <div className="panel">
      <div className="empty-state">
        <div style={{ fontSize: 16 }}>尚未创建任务</div>
        <div>上传一份生物论文或专利 PDF，或选择受控本地样本开始。</div>
        <button className="primary" onClick={onUpload}>
          添加 PDF
        </button>
        <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
          {samples.map((s) => (
            <span key={s.sample_id} className="status muted">
              {categoryLabel[s.category] ?? s.category}
              {s.available ? "" : " · 样本待补充"}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}
