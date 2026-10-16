import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  ComparisonInfo,
  createComparison,
  getComparisonDiff,
  listComparisons,
  listJobs,
  retryComparison,
} from "../../api/client";

/**
 * 解析器对比（C11/design §9）：同源同页 + 同一批领域规则才可比；
 * 实验与正式任务完全隔离（不写审核队列、不发基线）。没有人工 gold 时
 * 只显示覆盖与差异，绝不显示准确率或"最优引擎"结论。
 */

const SIDE_LABELS: Record<string, string> = {
  docling: "Docling（本地）",
  mineru_http: "MinerU 云 API",
};

function DiffView({ comparisonId }: { comparisonId: string }) {
  const diff = useQuery({
    queryKey: ["comparison-diff", comparisonId],
    queryFn: () => getComparisonDiff(comparisonId),
    retry: false,
  });
  if (diff.isLoading) return <div className="dim">计算差异…</div>;
  if (diff.error) return <div className="banner warn">差异暂不可用：{String(diff.error)}</div>;
  const data = diff.data as {
    sequences?: {
      matched?: string[];
      only_a?: string[];
      only_b?: string[];
      field_diffs?: { seq_id_no: string; length_a: number; length_b: number }[];
    };
    measurements?: {
      paired?: number;
      ambiguous?: number;
      keys_only_a?: string[][];
      keys_only_b?: string[][];
      value_diffs?: { seq_id_no?: string; entity?: string | null }[];
    };
    evaluation?: { status?: string; note?: string };
  };
  const seq = data.sequences ?? {};
  const meas = data.measurements ?? {};
  return (
    <div style={{ marginTop: 8 }}>
      <table className="data">
        <thead>
          <tr>
            <th>层面</th>
            <th>一致</th>
            <th>仅 A / 仅 B</th>
            <th>字段不同</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>序列（按 SEQ ID）</td>
            <td>{seq.matched?.length ?? 0}</td>
            <td>
              {seq.only_a?.length ?? 0} / {seq.only_b?.length ?? 0}
            </td>
            <td>{seq.field_diffs?.length ?? 0}</td>
          </tr>
          <tr>
            <td>活性（按 实体/指标/靶标）</td>
            <td>
              {meas.paired ?? 0}（歧义 {meas.ambiguous ?? 0}）
            </td>
            <td>
              {meas.keys_only_a?.length ?? 0} / {meas.keys_only_b?.length ?? 0}
            </td>
            <td>{meas.value_diffs?.length ?? 0}</td>
          </tr>
        </tbody>
      </table>
      {(seq.field_diffs?.length ?? 0) > 0 && (
        <div className="dim" style={{ marginTop: 6 }}>
          字段不同的序列：
          {seq.field_diffs!.slice(0, 8).map((d) => (
            <span key={d.seq_id_no} className="pd-chip meas" style={{ marginLeft: 4 }}>
              SEQ {d.seq_id_no}（{d.length_a} vs {d.length_b}）
            </span>
          ))}
        </div>
      )}
      {data.evaluation && (
        <div className="dim" style={{ marginTop: 6 }}>
          评测口径：{data.evaluation.status === "not_evaluated" ? "未评估" : data.evaluation.status}
          {data.evaluation.note ? `（${data.evaluation.note}）` : ""}
        </div>
      )}
    </div>
  );
}

export function ComparePage() {
  const [documentId, setDocumentId] = useState("");
  const [pagesText, setPagesText] = useState("");
  const [selected, setSelected] = useState<ComparisonInfo | null>(null);
  const queryClient = useQueryClient();

  const jobs = useQuery({ queryKey: ["jobs"], queryFn: listJobs });
  const comparisons = useQuery({
    queryKey: ["comparisons"],
    queryFn: listComparisons,
    refetchInterval: (q) =>
      (q.state.data?.comparisons ?? []).some(
        (c) => c.status === "running" || c.status === "queued",
      )
        ? 2000
        : false,
  });

  const create = useMutation({
    mutationFn: () =>
      createComparison({
        document_id: documentId.trim(),
        pages: pagesText
          .split(",")
          .map((p) => Number(p.trim()))
          .filter((n) => Number.isInteger(n) && n > 0),
        engines: ["docling", "mineru_http"],
      }),
    onSuccess: (info) => {
      setSelected(info);
      queryClient.invalidateQueries({ queryKey: ["comparisons"] });
    },
  });
  const retry = useMutation({
    mutationFn: (id: string) => retryComparison(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["comparisons"] }),
  });

  const documents = [...new Map((jobs.data ?? []).map((j) => [j.document_id, j])).values()];

  return (
    <div>
      <h2 style={{ fontSize: 16, margin: "0 0 12px" }}>解析器对比</h2>
      <div className="banner info">
        同源同页 + 同一批领域规则；实验与正式任务隔离（不写审核队列、不发基线）。
        MinerU 需要服务端 MINERU_TOKEN——未配置时该侧会如实标记为失败，不影响 Docling 侧。
      </div>

      <div className="panel" style={{ padding: 12, marginBottom: 12 }}>
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <label className="dim">
            文档：{" "}
            <select
              aria-label="对比文档"
              value={documentId}
              onChange={(e) => setDocumentId(e.target.value)}
            >
              <option value="">选择已登记文档</option>
              {documents.map((j) => (
                <option key={j.document_id} value={j.document_id}>
                  {j.document_id.slice(7, 27)}…（{j.job_id}）
                </option>
              ))}
            </select>
          </label>
          <label className="dim">
            页（逗号分隔，留空=全文）：{" "}
            <input
              aria-label="对比页集合"
              value={pagesText}
              onChange={(e) => setPagesText(e.target.value)}
              placeholder="30,31,32"
              style={{ width: 120 }}
            />
          </label>
          <button
            className="primary"
            disabled={!documentId || create.isPending}
            onClick={() => create.mutate()}
          >
            {create.isPending ? "创建中…" : "创建对比（Docling vs MinerU）"}
          </button>
        </div>
        {create.isError && (
          <div className="banner error" style={{ marginTop: 8 }}>
            创建失败：{String(create.error)}
          </div>
        )}
      </div>

      <div className="panel">
        <table className="data">
          <thead>
            <tr>
              <th>对比</th>
              <th>文档</th>
              <th>页</th>
              <th>状态</th>
              <th>两侧</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {(comparisons.data?.comparisons ?? []).map((c) => (
              <tr
                key={c.comparison_id}
                className={selected?.comparison_id === c.comparison_id ? "selected" : ""}
              >
                <td className="mono dim">{c.comparison_id}</td>
                <td className="mono dim">{c.document_id.slice(7, 19)}…</td>
                <td className="dim">{c.pages.length ? c.pages.join(",") : "全文"}</td>
                <td>
                  <span
                    className={`status ${c.status === "done" ? "muted" : c.status === "failed" ? "error" : "review"}`}
                  >
                    {c.status === "done" ? "完成" : c.status === "failed" ? "失败" : "运行中"}
                  </span>
                </td>
                <td className="dim">
                  {c.sides.map((s) => (
                    <span key={s.side} style={{ marginRight: 6 }} title={s.error ?? ""}>
                      {SIDE_LABELS[s.side] ?? s.side}：
                      {s.status === "done" ? "✓" : s.status === "failed" ? "✗" : "…"}
                    </span>
                  ))}
                </td>
                <td>
                  <button onClick={() => setSelected(c)}>查看</button>
                  {c.sides.some((s) => s.status === "failed") && (
                    <button style={{ marginLeft: 6 }} onClick={() => retry.mutate(c.comparison_id)}>
                      重试失败侧
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {(comparisons.data?.comparisons ?? []).length === 0 && (
              <tr>
                <td colSpan={6} className="dim">
                  还没有对比实验。选择文档后可创建 Docling vs MinerU 的同源对比。
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {selected && (
        <div className="panel" style={{ padding: 12, marginTop: 12 }}>
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <strong>{selected.comparison_id}</strong>
            <span className="dim">
              页：{selected.pages.length ? selected.pages.join(",") : "全文"} · 状态：
              {selected.status}
            </span>
          </div>
          {selected.sides.map((s) => (
            <div
              key={s.side}
              className={s.status === "failed" ? "banner warn" : "dim"}
              style={{ marginTop: 6 }}
            >
              {SIDE_LABELS[s.side] ?? s.side}：{s.status}
              {s.error ? ` — ${s.error}` : ""}
            </div>
          ))}
          {selected.status === "done" ? (
            <DiffView comparisonId={selected.comparison_id} />
          ) : (
            <div className="dim" style={{ marginTop: 6 }}>
              两侧完成后显示差异（当前两侧未全部完成/失败——失败侧可重试）。
            </div>
          )}
        </div>
      )}
    </div>
  );
}