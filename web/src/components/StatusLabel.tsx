/** Status label: colour + text. Never colour alone (design §10). */

const JOB_STATUS: Record<string, { cls: string; label: string }> = {
  queued: { cls: "muted", label: "等待解析" },
  running: { cls: "running", label: "解析中" },
  succeeded: { cls: "ok", label: "处理完成" },
  succeeded_with_review: { cls: "review", label: "处理完成 · 待审核" },
  failed: { cls: "conflict", label: "处理失败" },
  cancelled: { cls: "muted", label: "已取消" },
};

const RECORD_STATUS: Record<string, { cls: string; label: string }> = {
  checks_passed: { cls: "ok", label: "规则检查通过" },
  verified_reference: { cls: "ok", label: "已核对参考" },
  verified_human: { cls: "ok", label: "人工核对通过" },
  candidate: { cls: "muted", label: "候选" },
  needs_review: { cls: "review", label: "待审核" },
  accepted: { cls: "ok", label: "已接受" },
  rejected: { cls: "conflict", label: "已拒绝" },
  conflict: { cls: "conflict", label: "存在冲突" },
  unchecked: { cls: "muted", label: "未检查" },
};

export function StatusLabel({
  value,
  kind = "job",
  openReviews,
}: {
  value: string | null | undefined;
  kind?: "job" | "record";
  /** When provided for jobs, a review-complete status stops claiming 待审核. */
  openReviews?: number;
}) {
  if (!value) {
    return <span className="status muted">未提取</span>;
  }
  const table = kind === "job" ? JOB_STATUS : RECORD_STATUS;
  let entry = table[value] ?? { cls: "muted", label: value };
  if (kind === "job" && value === "succeeded_with_review" && openReviews === 0) {
    entry = { cls: "ok", label: "处理完成（审核已全部处理）" };
  }
  return <span className={`status ${entry.cls}`}>{entry.label}</span>;
}
