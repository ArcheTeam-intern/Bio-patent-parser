import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, getReviews, submitDecision } from "../../api/client";

/**
 * 审核面板（design §8.1）：确认 / 修正 / 拒绝，服务端原子提交。
 * 409 冲突时显示服务端当前值，要求人工重新核对，绝不覆盖。
 */

interface Props {
  jobId: string;
  recordId: string;
  currentRecord: Record<string, unknown>;
}

export function ReviewPanel({ jobId, recordId, currentRecord }: Props) {
  const [actor] = useState("local-reviewer");
  const [mode, setMode] = useState<"idle" | "rejecting" | "correcting">("idle");
  const [reason, setReason] = useState("");
  const [patchDraft, setPatchDraft] = useState("");
  const [conflict, setConflict] = useState<{ live: number; base: number } | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const reviewItemQuery = useOpenReview(jobId, recordId);

  const decide = useMutation({
    mutationFn: async (args: {
      action: "confirm" | "correct" | "reject";
      baseRevision: number;
      patch?: Record<string, unknown>;
      reason?: string;
    }) =>
      submitDecision(reviewItemQuery.reviewId!, {
        action: args.action,
        base_revision: args.baseRevision,
        actor,
        patch: args.patch,
        reason: args.reason,
        evidence_ids: (currentRecord["evidence_ids"] as string[]) ?? [],
      }),
    onSuccess: (res) => {
      setConflict(null);
      setMode("idle");
      setMessage(
        res.action === "reject"
          ? "已拒绝，记录保留历史。"
          : res.accepted
            ? "已确认并接受（新版本 r" + res.record_revision + "）。"
            : "决定已应用。",
      );
      queryClient.invalidateQueries({ queryKey: ["records", jobId] });
      queryClient.invalidateQueries({ queryKey: ["job", jobId] });
      queryClient.invalidateQueries({ queryKey: ["reviews"] });
      reviewItemQuery.refresh();
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        const live = Number(err.details["live_revision"]);
        setConflict({ live, base: Number(err.details["base_revision"]) });
        setMessage("版本冲突：其他窗口已修改此记录。请核对新值后重试。");
      } else if (err instanceof ApiError) {
        setMessage(`提交失败：${err.message}`);
      } else {
        setMessage(`提交失败：${String(err)}`);
      }
    },
  });

  const blockers = (currentRecord["blockers"] as string[] | undefined) ?? [];
  const item = reviewItemQuery.reviewId;
  // B07: no automatic version bumping. While a conflict is unresolved the
  // submit path stays disabled; the reviewer reloads (fresh queries) and
  // confirms against the NEW revision explicitly. The record's own
  // _revision (server-provided) is the fallback when no review item exists.
  const baseRevision = conflict
    ? conflict.live
    : Math.max(reviewItemQuery.recordRevision, Number(currentRecord["_revision"] ?? 0));

  if (!item) {
    return (
      <div style={{ padding: 12, borderTop: "1px solid var(--border-soft)" }}>
        <div className="dim">
          此记录没有待审核项
          {String(currentRecord["acceptance_state"]) === "accepted" ? "（已接受）。" : "。"}
        </div>
        {message && <div className="banner info" style={{ marginTop: 8 }}>{message}</div>}
      </div>
    );
  }

  return (
    <div style={{ padding: 12, borderTop: "1px solid var(--border-soft)" }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
        <span className="dim">审核（基线 r{baseRevision || reviewItemQuery.recordRevision}）</span>
        {conflict && (
          <span className="status conflict">
            冲突：服务端已到 r{conflict.live}
          </span>
        )}
      </div>

      {message && <div className={`banner ${conflict ? "warn" : "info"}`}>{message}</div>}

      {mode === "idle" && conflict && (
        <div style={{ display: "flex", gap: 8 }}>
          <button
            className="primary"
            onClick={() => {
              // Explicit re-verification: reload both the record and the
              // review item, then the reviewer decides again.
              setConflict(null);
              setMessage("已重新加载最新记录与审核状态，请再次核对后提交。");
              queryClient.invalidateQueries({ queryKey: ["records", jobId] });
              reviewItemQuery.refresh();
            }}
          >
            重新核对最新版本
          </button>
          <button onClick={() => setConflict(null)}>取消</button>
        </div>
      )}

      {mode === "idle" && !conflict && (
        <div style={{ display: "flex", gap: 8 }}>
          <button
            className="primary"
            disabled={decide.isPending || blockers.length > 0}
            title={blockers.join("；")}
            onClick={() => decide.mutate({ action: "confirm", baseRevision })}
          >
            确认
          </button>
          <button disabled={decide.isPending} onClick={() => setMode("correcting")}>
            修正并确认
          </button>
          <button
            disabled={decide.isPending}
            style={{ borderColor: "var(--red)", color: "var(--red)" }}
            onClick={() => setMode("rejecting")}
          >
            拒绝
          </button>
        </div>
      )}

      {mode === "rejecting" && !conflict && (
        <div style={{ display: "grid", gap: 8 }}>
          <label>
            <div className="dim">拒绝原因（必填）</div>
            <input type="text" value={reason} onChange={(e) => setReason(e.target.value)} style={{ width: "100%" }} />
          </label>
          <div style={{ display: "flex", gap: 8 }}>
            <button
              disabled={!reason.trim() || decide.isPending}
              onClick={() => decide.mutate({ action: "reject", baseRevision, reason })}
            >
              提交拒绝
            </button>
            <button onClick={() => setMode("idle")}>取消</button>
          </div>
        </div>
      )}

      {mode === "correcting" && !conflict && (
        <div style={{ display: "grid", gap: 8 }}>
          <div className="dim">
            结构化修正（typed patch）。数值保持十进制字符串；raw 只能写证据中实际存在的表达。
          </div>
          <PatchFields
            record={currentRecord}
            onChange={(patch) => setPatchDraft(JSON.stringify(patch))}
          />
          <div style={{ display: "flex", gap: 8 }}>
            <button
              className="primary"
              disabled={decide.isPending || !patchDraft}
              onClick={() => {
                try {
                  const patch = JSON.parse(patchDraft) as Record<string, unknown>;
                  decide.mutate({ action: "correct", baseRevision, patch });
                } catch {
                  setMessage("修正内容不是有效的结构化 patch。");
                }
              }}
            >
              提交修正
            </button>
            <button onClick={() => setMode("idle")}>取消</button>
          </div>
        </div>
      )}
    </div>
  );
}

function PatchFields({
  record,
  onChange,
}: {
  record: Record<string, unknown>;
  onChange: (patch: Record<string, unknown> | null) => void;
}) {
  const value = record["value"] as Record<string, unknown> | undefined;
  const [kind, setKind] = useState<string>(String(value?.["kind"] ?? "range"));
  const [lower, setLower] = useState(String(value?.["lower"] ?? ""));
  const [upper, setUpper] = useState(String(value?.["upper"] ?? ""));
  const [number, setNumber] = useState(String(value?.["number"] ?? ""));
  const [uncertainty, setUncertainty] = useState(String(value?.["uncertainty"] ?? ""));
  const [qualifier, setQualifier] = useState(String(value?.["qualifier"] ?? ""));
  const [raw, setRaw] = useState(String(value?.["raw"] ?? ""));

  const build = () => {
    const patchValue: Record<string, unknown> = { kind, raw };
    if (kind === "range") {
      patchValue["lower"] = lower;
      patchValue["upper"] = upper;
    } else if (kind === "scalar") {
      patchValue["number"] = number;
    } else if (kind === "plus_minus") {
      patchValue["number"] = number;
      patchValue["uncertainty"] = uncertainty;
    } else if (kind === "categorical") {
      patchValue["qualifier"] = qualifier;
    }
    const unchanged =
      value &&
      value["kind"] === kind &&
      String(value["raw"] ?? "") === raw &&
      (kind !== "range" || (String(value["lower"]) === lower && String(value["upper"]) === upper));
    onChange(unchanged ? null : { value: patchValue });
  };

  return (
    <div style={{ display: "grid", gap: 6 }}>
      <label>
        <div className="dim">数值类型</div>
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="range">区间</option>
          <option value="scalar">标量</option>
          <option value="plus_minus">均值±误差</option>
          <option value="categorical">分类</option>
        </select>
      </label>
      {kind === "range" && (
        <>
          <label>下界 <input type="text" value={lower} onChange={(e) => setLower(e.target.value)} /></label>
          <label>上界 <input type="text" value={upper} onChange={(e) => setUpper(e.target.value)} /></label>
        </>
      )}
      {kind === "scalar" && (
        <label>数值 <input type="text" value={number} onChange={(e) => setNumber(e.target.value)} /></label>
      )}
      {kind === "plus_minus" && (
        <>
          <label>均值 <input type="text" value={number} onChange={(e) => setNumber(e.target.value)} /></label>
          <label>误差 <input type="text" value={uncertainty} onChange={(e) => setUncertainty(e.target.value)} /></label>
        </>
      )}
      {kind === "categorical" && (
        <label>限定词 <input type="text" value={qualifier} onChange={(e) => setQualifier(e.target.value)} /></label>
      )}
      <label>
        <div className="dim">原文表达（必须与证据一致）</div>
        <input type="text" value={raw} onChange={(e) => setRaw(e.target.value)} style={{ width: "100%" }} />
      </label>
      <button onClick={build}>生成 patch</button>
    </div>
  );
}

function useOpenReview(jobId: string, recordId: string) {
  const query = useQuery({
    queryKey: ["reviews", jobId],
    queryFn: () => getReviews(jobId),
  });
  const [version, setVersion] = useState<{ reviewId: string | null; recordRevision: number }>({
    reviewId: null,
    recordRevision: 1,
  });
  useEffect(() => {
    const items = query.data?.reviews ?? [];
    const match = items.find((r) => r.record_id === recordId);
    setVersion({
      reviewId: match?.review_id ?? null,
      recordRevision: match?.record_revision ?? 1,
    });
  }, [query.data, recordId]);
  return { ...version, refresh: () => query.refetch() };
}
