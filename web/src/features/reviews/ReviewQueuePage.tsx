import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { getReviews } from "../../api/client";
import { StatusLabel } from "../../components/StatusLabel";

/** 审核队列（design §8）：按原因排列，点击进入工作台审核聚焦。 */

const REASON_LABELS: Record<string, string> = {
  VALIDATION_CONFLICT: "存在冲突",
  SEQUENCE_CONFLICT: "序列冲突",
  SEQ_DECLARED_LENGTH_MATCH: "长度异常",
  ACT_UNIT_PRESENT: "缺少单位",
  ACT_ENTITY_LINK: "实体未关联",
  ACT_TARGET_LINK: "靶标未关联",
  LINK_UNRESOLVED: "关系歧义",
  REQUIRES_HUMAN_OR_REFERENCE_VERIFICATION: "需人工核对",
};

export function ReviewQueuePage() {
  const reviews = useQuery({ queryKey: ["reviews"], queryFn: () => getReviews() });
  const navigate = useNavigate();

  const items = reviews.data?.reviews ?? [];

  return (
    <div>
      <h2 style={{ fontSize: 16, margin: "0 0 12px" }}>审核队列</h2>
      <div className="banner info">
        队列只包含需要人工处理的记录。确认、修正、拒绝随审核服务启用（D3）；
        当前可浏览原因与证据并跳转到工作台。
      </div>
      {items.length === 0 ? (
        <div className="panel">
          <div className="empty-state">
            {reviews.isLoading ? "加载中…" : "队列为空：没有待审核记录。"}
          </div>
        </div>
      ) : (
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
      )}
    </div>
  );
}
