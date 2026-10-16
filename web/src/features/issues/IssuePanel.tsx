import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getIssues } from "../../api/client";

/**
 * Issues 面板（plan B12 / C09）：结构化处理问题的可审计视图。
 * 每条问题回答"为什么 / 影响什么 / 下一步怎么办"，并按
 * 严重程度（提示/警告/错误）、页码、状态过滤；位置只来自真实字段，
 * 不从 id 猜页号。动作（重解析等）随区域复解析执行环境接入——
 * 当前为只读审计视图，不显示不可用的按钮。
 */

const REASON_LABELS: Record<string, string> = {
  TABLE_INVENTORY: "表格：菌株/目录型",
  TABLE_SCORING: "表格：评分型",
  TABLE_UNCLASSIFIED: "表格：未归类",
  TABLE_SEQUENCE: "表格：序列型",
  TABLE_ASSAY_CONDITIONS: "表格：条件型",
  TABLE_STRUCTURE: "表格：结构问题",
  TABLE_GEOMETRY_RECONSTRUCTED: "表格：几何重建",
  RESULT_WITHOUT_METRIC: "结果提及但指标未识别",
  ZERO_RESIDUE_LISTING: "清单零残基",
  SEQ_INVENTORY_MISSING: "序列残基未恢复",
  SEQ_INVENTORY_CONFLICT: "序列声明冲突",
  REPARSE_EXHAUSTED: "复解析预算耗尽",
  REPARSE_NO_REGION: "复解析无法定位区域",
};

const SEVERITY_LABELS: Record<string, string> = {
  info: "提示",
  warning: "警告",
  error: "错误",
};

const SEVERITY_CLASS: Record<string, string> = {
  info: "muted",
  warning: "review",
  error: "error",
};

export function IssuePanel({ jobId }: { jobId: string }) {
  const [severity, setSeverity] = useState<string>("");
  const [pageNumber, setPageNumber] = useState<string>("");

  const pageFilter =
    pageNumber.trim() !== "" && Number.isInteger(Number(pageNumber))
      ? Number(pageNumber)
      : undefined;
  const issues = useQuery({
    queryKey: ["issues", jobId, severity, pageFilter],
    queryFn: () =>
      getIssues(jobId, {
        severity: severity || undefined,
        pageNumber: pageFilter,
      }),
    enabled: !!jobId,
  });

  if (issues.isLoading) return <div className="empty-state">加载中…</div>;
  if (issues.error) return <div className="banner error">读取失败：{String(issues.error)}</div>;
  const items = issues.data?.issues ?? [];
  const counts = issues.data?.severity_counts ?? {};

  return (
    <div style={{ padding: 8 }}>
      <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 8, flexWrap: "wrap" }}>
        <label className="dim">
          严重程度：{" "}
          <select aria-label="严重程度筛选" value={severity} onChange={(e) => setSeverity(e.target.value)}>
            <option value="">全部</option>
            <option value="error">错误</option>
            <option value="warning">警告</option>
            <option value="info">提示</option>
          </select>
        </label>
        <label className="dim">
          页码：{" "}
          <input
            aria-label="页码筛选"
            style={{ width: 64 }}
            value={pageNumber}
            onChange={(e) => setPageNumber(e.target.value)}
            placeholder="如 32"
          />
        </label>
        <span className="dim">
          共 {issues.data?.total ?? 0} 条
          {counts.error !== undefined
            ? `（错误 ${counts.error ?? 0} · 警告 ${counts.warning ?? 0} · 提示 ${counts.info ?? 0}）`
            : ""}
        </span>
      </div>

      {items.length === 0 ? (
        <div className="empty-state">
          {pageFilter !== undefined || severity
            ? "当前筛选下没有问题。"
            : "该任务没有结构化处理问题（issues）。"}
        </div>
      ) : (
        items.map((issue) => (
          <div key={issue.issue_id} className="panel" style={{ marginBottom: 8, padding: 8 }}>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <span className={`status ${SEVERITY_CLASS[issue.severity ?? "warning"] ?? "review"}`}>
                {SEVERITY_LABELS[issue.severity ?? "warning"] ?? "警告"}
              </span>
              <span className="status review">{REASON_LABELS[issue.reason_code] ?? issue.reason_code}</span>
              {issue.page_number != null && <span className="dim">第 {issue.page_number} 页</span>}
              <span className="dim mono">{issue.scope}</span>
              <span className="dim">state: {issue.state}</span>
              {issue.baseline_id && <span className="dim mono">baseline {issue.baseline_id}</span>}
            </div>
            {issue.title && <div style={{ marginTop: 6, fontWeight: 600 }}>{issue.title}</div>}
            <div className="dim" style={{ marginTop: 4 }}>
              {issue.detail}
            </div>
            <dl className="detail-grid" style={{ marginTop: 6 }}>
              <dt>影响</dt>
              <dd>{issue.impact ?? "见详情"}</dd>
              <dt>下一步</dt>
              <dd>{issue.suggestion ?? "按详情人工核对"}</dd>
            </dl>
            {issue.source_ids.length > 0 && (
              <div className="dim mono" style={{ marginTop: 2, fontSize: 11 }}>
                来源：{issue.source_ids.slice(0, 6).join(", ")}
              </div>
            )}
          </div>
        ))
      )}
      <div className="dim" style={{ padding: 4 }}>
        动作（重解析 / 标记非目标 / 保留未解决）随区域复解析执行环境接入；当前为只读审计视图。
      </div>
    </div>
  );
}
