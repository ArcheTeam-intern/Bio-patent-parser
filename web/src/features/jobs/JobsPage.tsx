import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { useState } from "react";
import { getSamples, listJobs } from "../../api/client";
import { StatusLabel } from "../../components/StatusLabel";
import { UploadDialog } from "./UploadDialog";

/** 任务列表：上传、样本导入、观察状态（design §6）。 */

export function JobsPage() {
  const [uploadOpen, setUploadOpen] = useState(false);
  const navigate = useNavigate();

  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: listJobs,
    refetchInterval: (q) => (activeJobs(q.state.data) ? 1500 : false),
  });
  const samples = useQuery({ queryKey: ["samples"], queryFn: getSamples });

  const jobsList = jobs.data ?? [];

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <h2 style={{ margin: 0, fontSize: 16 }}>任务列表</h2>
        <button className="primary" onClick={() => setUploadOpen(true)}>
          添加 PDF
        </button>
      </div>

      {jobsList.length === 0 && !jobs.isLoading ? (
        <EmptyJobs samples={samples.data?.samples ?? []} onUpload={() => setUploadOpen(true)} />
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
                    <StatusLabel value={job.status} kind="job" />
                  </td>
                  <td>{job.open_reviews > 0 ? <span className="status review">{job.open_reviews} 条</span> : "—"}</td>
                  <td className="dim">{job.current_stage ?? "—"}</td>
                  <td className="dim">{new Date(job.created_at).toLocaleString()}</td>
                  <td>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        navigate(`/jobs/${job.job_id}/workbench`);
                      }}
                    >
                      打开工作台
                    </button>
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
