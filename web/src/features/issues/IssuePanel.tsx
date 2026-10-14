import { useQuery } from "@tanstack/react-query";
import { getIssues } from "../../api/client";

/**
 * Issues 面板（plan B12）：结构化处理问题的可审计视图。
 * 每个 issue 展示 scope/原因/来源（表 ID、证据 ID、页码）与动作提示；
 * 没有 issues 时明确写"无"——不把普通 diagnostic 字符串冒充审核项。
 */

const REASON_LABELS: Record<string, string> = {
  TABLE_INVENTORY: "表格：菌株/目录型",
  TABLE_SCORING: "表格：评分型",
  TABLE_UNCLASSIFIED: "表格：未归类",
  TABLE_SEQUENCE: "表格：序列型",
  TABLE_ASSAY_CONDITIONS: "表格：条件型",
  TABLE_STRUCTURE: "表格：结构问题",
  RESULT_WITHOUT_METRIC: "结果提及但指标未识别",
  ZERO_RESIDUE_LISTING: "清单零残基",
  REPARSE_EXHAUSTED: "复解析预算耗尽",
  REPARSE_NO_REGION: "复解析无法定位区域",
};

export function IssuePanel({ jobId }: { jobId: string }) {
  const issues = useQuery({
    queryKey: ["issues", jobId],
    queryFn: () => getIssues(jobId),
    enabled: !!jobId,
  });

  if (issues.isLoading) return <div className="empty-state">加载中…</div>;
  if (issues.error) return <div className="banner error">读取失败：{String(issues.error)}</div>;
  const items = issues.data?.issues ?? [];
  if (items.length === 0) {
    return <div className="empty-state">该任务没有结构化处理问题（issues）。</div>;
  }

  return (
    <div style={{ padding: 8 }}>
      {items.map((issue) => (
        <div key={issue.issue_id} className="panel" style={{ marginBottom: 8, padding: 8 }}>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <span className="status review">{REASON_LABELS[issue.reason_code] ?? issue.reason_code}</span>
            <span className="dim mono">{issue.scope}</span>
            <span className="dim">state: {issue.state}</span>
            {issue.baseline_id && <span className="dim mono">baseline {issue.baseline_id}</span>}
          </div>
          <div className="dim" style={{ marginTop: 4 }}>{issue.detail}</div>
          {issue.source_ids.length > 0 && (
            <div className="dim mono" style={{ marginTop: 2, fontSize: 11 }}>
              来源：{issue.source_ids.slice(0, 6).join(", ")}
            </div>
          )}
        </div>
      ))}
      <div className="dim" style={{ padding: 4 }}>
        动作（重解析 / 标记非目标 / 保留未解决）随区域复解析执行环境接入；当前为只读审计视图。
      </div>
    </div>
  );
}
