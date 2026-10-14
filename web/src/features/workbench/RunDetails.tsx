import { useQuery } from "@tanstack/react-query";
import { artifactContentUrl, Job, listArtifacts } from "../../api/client";

/**
 * 运行详情（design §7.7）：只展示服务端真实数据 —— 引擎、配置哈希、
 * 阶段运行记录与制品清单。不渲染任何未被接口返回的字段。
 */

function formatBytes(bytes: number | null): string {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

function duration(started: string | null, ended: string | null): string {
  if (!started || !ended) return "—";
  const ms = new Date(ended).getTime() - new Date(started).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "—";
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export function RunDetails({ jobId, job }: { jobId: string; job: Job | undefined }) {
  const artifacts = useQuery({
    queryKey: ["artifacts", jobId],
    queryFn: () => listArtifacts(jobId),
    enabled: !!jobId,
  });

  const stages = job?.stages ?? [];

  return (
    <div style={{ padding: 12, display: "grid", gap: 14 }}>
      <div>
        <div className="dim" style={{ marginBottom: 6 }}>解析配置</div>
        <table className="data">
          <tbody>
            <tr>
              <td style={{ width: 120, whiteSpace: "nowrap" }}>解析引擎</td>
              <td className="mono">{job?.engine || "—"}</td>
            </tr>
            <tr>
              <td style={{ whiteSpace: "nowrap" }}>配置哈希</td>
              <td className="mono" style={{ wordBreak: "break-all" }}>{job?.config_hash || "—"}</td>
            </tr>
            <tr>
              <td style={{ whiteSpace: "nowrap" }}>结果版本</td>
              <td>r{job?.result_revision ?? 0}（当前修订）</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div>
        <div className="dim" style={{ marginBottom: 6 }}>阶段运行记录</div>
        {stages.length === 0 ? (
          <div className="empty-state">没有阶段记录。</div>
        ) : (
          <table className="data">
            <thead>
              <tr>
                <th>阶段</th>
                <th>尝试</th>
                <th>状态</th>
                <th>开始</th>
                <th>耗时</th>
                <th>错误</th>
              </tr>
            </thead>
            <tbody>
              {stages.map((s) => (
                <tr key={`${s.stage_name}-${s.attempt}`}>
                  <td className="mono">{s.stage_name}</td>
                  <td>{s.attempt}</td>
                  <td>{s.status}</td>
                  <td className="dim">
                    {s.started_at ? new Date(s.started_at).toLocaleString() : "—"}
                  </td>
                  <td className="dim">{duration(s.started_at, s.ended_at)}</td>
                  <td className="dim">
                    {Object.values(s.error ?? {}).join("；") || "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div>
        <div className="dim" style={{ marginBottom: 6 }}>制品清单</div>
        {artifacts.isLoading && <div className="dim">正在加载制品…</div>}
        {artifacts.error && (
          <div className="banner error">制品读取失败：{String(artifacts.error)}</div>
        )}
        {artifacts.data && (
          <table className="data">
            <thead>
              <tr>
                <th>路径</th>
                <th>类型</th>
                <th>大小</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {artifacts.data.artifacts.map((a) => (
                <tr key={a.artifact_id}>
                  <td className="mono dim" style={{ fontSize: 11 }}>
                    {a.relative_path.replace(`jobs/${jobId}/`, "")}
                  </td>
                  <td className="dim">{a.media_type}</td>
                  <td className="dim">{formatBytes(a.bytes)}</td>
                  <td>
                    <a
                      href={artifactContentUrl(jobId, a.artifact_id)}
                      target="_blank"
                      rel="noreferrer"
                    >
                      下载
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}