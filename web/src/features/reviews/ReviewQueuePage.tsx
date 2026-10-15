import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import { getReviews, listJobs } from "../../api/client";
import { StatusLabel } from "../../components/StatusLabel";

/**
 * 审核队列（design §8）：跨任务的全部待审核记录，按任务可筛选、
 * 分页加载。“总数”是真实匹配数——一个几何重建的药敏矩阵可能产生
 * 上百条单元格审核项，截断的页面不允许冒充整个队列。
 */

const REASON_LABELS: Record<string, string> = {
  VALIDATION_CONFLICT: "存在冲突",
  SEQUENCE_CONFLICT: "序列冲突",
  SEQ_DECLARED_LENGTH_MATCH: "长度异常",
  "declared length does not match observed length": "长度不符",
  "sequence is incomplete": "序列不完整",
  ACT_UNIT_PRESENT: "缺少单位",
  "measurement has no unit": "缺少单位",
  ACT_ENTITY_LINK: "实体未关联",
  "measurement has no linked entity": "实体未关联",
  ACT_TARGET_LINK: "靶标未关联",
  LINK_UNRESOLVED: "关系歧义",
  REQUIRES_HUMAN_OR_REFERENCE_VERIFICATION: "需人工核对",
};

const PAGE_SIZE = 100;

export function ReviewQueuePage() {
  const [params, setParams] = useSearchParams();
  const jobFilter = params.get("job_id") ?? "";
  const [limit, setLimit] = useState(PAGE_SIZE);
  const navigate = useNavigate();

  const reviews = useQuery({
    queryKey: ["reviews", "queue", jobFilter, limit],
    queryFn: () => getReviews(jobFilter || undefined, { limit }),
  });
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: listJobs });

  const items = reviews.data?.reviews ?? [];
  const total = reviews.data?.total ?? 0;
  const jobsWithReviews = (jobs.data ?? []).filter((j) => j.status !== "queued" || j.open_reviews > 0);

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12, flexWrap: "wrap" }}>
        <h2 style={{ fontSize: 16, margin: 0 }}>审核队列</h2>
        <label className="dim">
          任务：{" "}
          <select
            aria-label="任务筛选"
            value={jobFilter}
            onChange={(e) => {
              const next = new URLSearchParams(params);
              if (e.target.value) next.set("job_id", e.target.value);
              else next.delete("job_id");
              setParams(next, { replace: true });
              setLimit(PAGE_SIZE);
            }}
          >
            <option value="">全部任务</option>
            {jobsWithReviews.map((j) => (
              <option key={j.job_id} value={j.job_id}>
                {j.job_id}
              </option>
            ))}
          </select>
        </label>
        <span className="wb-count">共 {total} 条待审核</span>
      </div>
      <div className="banner info">
        队列只包含需要人工处理的记录（序列与活性）。点击一行跳到工作台对应记录，在右侧面板确认 / 修正 / 拒绝。
      </div>
      {items.length === 0 ? (
        <div className="panel">
          <div className="empty-state">
            {reviews.isLoading ? "加载中…" : "队列为空：没有待审核记录。"}
          </div>
        </div>
      ) : (
        <>
          <div className="panel">
            <table className="data">
              <thead>
                <tr>
                  <th>原因</th>
                  <th>记录类型</th>
                  <th>任务</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {items.map((r) => (
                  <tr
                    key={r.review_id}
                    className="clickable"
                    onClick={() => navigate(`/jobs/${r.job_id}/workbench?record=${encodeURIComponent(r.record_id)}`)}
                  >
                    <td>
                      {r.reason_codes.map((code, i) => (
                        <span key={i} className="status review" style={{ marginRight: 4 }}>
                          {REASON_LABELS[code] ?? code}
                        </span>
                      ))}
                    </td>
                    <td>{r.record_kind === "sequence" ? "序列" : r.record_kind === "measurement" ? "活性" : "未知"}</td>
                    <td className="mono dim">{r.job_id}</td>
                    <td>
                      <StatusLabel value={r.status === "open" ? "needs_review" : r.status} kind="record" />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {items.length < total && (
            <div style={{ marginTop: 10, textAlign: "center" }}>
              <button onClick={() => setLimit((n) => n + PAGE_SIZE)}>
                已显示 {items.length} / {total} 条 · 加载更多
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
