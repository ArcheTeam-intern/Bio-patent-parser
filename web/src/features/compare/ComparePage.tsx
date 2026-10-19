import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  ComparisonInfo,
  ComparisonSideErrorInfo,
  EngineCapability,
  createComparison,
  getCapabilities,
  getComparison,
  getComparisonDiff,
  listComparisons,
  listJobs,
  retryComparison,
} from "../../api/client";
import { ApiError } from "../../api/client";

/**
 * 解析器对比（C11/design §9）：同源同页 + 同一批领域规则才可比；
 * 实验与正式任务完全隔离（不写审核队列、不发基线）。没有人工 gold 时
 * 只显示覆盖与差异，绝不显示准确率或"最优引擎"结论。
 *
 * K02: capability is read first and gates create/retry buttons by
 * ``configured``; ``error_info`` is the structured failure payload and
 * drives the side-level banner. K03: details are fetched by ID, not
 * snapshotted into local state — the list, the detail and the diff all
 * read from the same query so a successful retry refreshes the panel
 * without a second click.
 */

const SIDE_LABELS: Record<string, string> = {
  docling: "Docling（本地）",
  mineru_http: "MinerU 云 API",
};

function describeSideError(info: ComparisonSideErrorInfo | null, fallback: string | null): string {
  if (!info) return fallback ?? "";
  const head = `[${info.code}] 阶段=${info.stage}` + (info.retryable ? "（可重试）" : "（不可重试）");
  return info.message ? `${head}\n${info.message}` : head;
}

function DiffView({ comparisonId }: { comparisonId: string }) {
  const diff = useQuery({
    queryKey: ["comparison-diff", comparisonId],
    queryFn: () => getComparisonDiff(comparisonId),
    retry: false,
  });
  if (diff.isLoading) return <div className="dim">计算差异…</div>;
  if (diff.error) return <div className="banner warn">差异暂不可用：{String(diff.error)}</div>;
  const data = diff.data as {
    structure?: Record<string, { blocks?: number; tables?: number; figures?: number; words?: number }>;
    figures?: {
      count_a?: number;
      count_b?: number;
      captions_a?: number;
      captions_b?: number;
      shared_captions?: string[];
      only_a_captions?: string[];
      only_b_captions?: string[];
      per_page?: Record<string, Record<string, number>>;
    };
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
      pairs_with_unverified_conditions?: number;
      conditions_note?: string;
    };
    evaluation?: { status?: string; note?: string };
  };
  const seq = data.sequences ?? {};
  const meas = data.measurements ?? {};
  const fig = data.figures ?? {};
  // Side names are engine names; derive the two labels present in the map.
  const structureNames = Object.keys(data.structure ?? {});
  const [nameA, nameB] = structureNames.length === 2 ? structureNames : ["", ""];
  const structA = data.structure?.[nameA] ?? {};
  const structB = data.structure?.[nameB] ?? {};
  const num = (v: number | undefined) => (v == null ? "—" : v);
  const pageKeys = Object.keys(fig.per_page?.[nameB] ?? fig.per_page?.[nameA] ?? {}).sort(
    (x, y) => Number(x) - Number(y),
  );
  return (
    <div style={{ marginTop: 8 }}>
      <table className="data">
        <thead>
          <tr>
            <th>层面</th>
            <th>{nameA || "A"}</th>
            <th>{nameB || "B"}</th>
            <th>判定</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>文本块</td>
            <td>{num(structA.blocks)}</td>
            <td>{num(structB.blocks)}</td>
            <td className="dim">结构量，不比较优劣</td>
          </tr>
          <tr>
            <td>表格</td>
            <td>{num(structA.tables)}</td>
            <td>{num(structB.tables)}</td>
            <td className="dim">检出数量；结构错误另见结构闸门</td>
          </tr>
          <tr>
            <td>图形</td>
            <td>{num(structA.figures)}</td>
            <td>{num(structB.figures)}</td>
            <td className="dim">
              caption 一致 {fig.shared_captions?.length ?? 0} · 仅 A {fig.only_a_captions?.length ?? 0} · 仅 B{" "}
              {fig.only_b_captions?.length ?? 0}
            </td>
          </tr>
          <tr>
            <td>字数</td>
            <td>{num(structA.words)}</td>
            <td>{num(structB.words)}</td>
            <td className="dim">两侧各有丢失时需对照原文</td>
          </tr>
          <tr>
            <td>序列（按 SEQ ID）</td>
            <td colSpan={2} className="dim">
              一致 {seq.matched?.length ?? 0} · 仅 A {seq.only_a?.length ?? 0} · 仅 B{" "}
              {seq.only_b?.length ?? 0} · 字段不同 {seq.field_diffs?.length ?? 0}
            </td>
            <td className="dim">{seq.matched?.length ? "按序列号配对" : "两侧未解析出序列"}</td>
          </tr>
          <tr>
            <td>活性（按 实体/指标/靶标）</td>
            <td colSpan={2} className="dim">
              配对 {meas.paired ?? 0} · 歧义 {meas.ambiguous ?? 0} · 仅 A {meas.keys_only_a?.length ?? 0} · 仅 B{" "}
              {meas.keys_only_b?.length ?? 0} · 值不同 {meas.value_diffs?.length ?? 0}
              {(meas.pairs_with_unverified_conditions ?? 0) > 0 && (
                <> · 条件未核对 {meas.pairs_with_unverified_conditions}</>
              )}
            </td>
            <td className="dim">
              {(meas.paired ?? 0) === 0 && (meas.keys_only_a?.length ?? 0) === 0 && (meas.keys_only_b?.length ?? 0) === 0
                ? "两侧均未提取到活性测量"
                : "条件未核对的配对不算同一实验"}
            </td>
          </tr>
        </tbody>
      </table>

      {pageKeys.length > 0 && (
        <div className="dim" style={{ marginTop: 8 }}>
          每页图形数：
          {pageKeys.map((page) => {
            const a = fig.per_page?.[nameA]?.[page] ?? 0;
            const b = fig.per_page?.[nameB]?.[page] ?? 0;
            return (
              <span key={page} className="pd-chip meas" style={{ marginLeft: 4 }}>
                p{page}: {a} / {b}
              </span>
            );
          })}
        </div>
      )}

      {((fig.only_a_captions?.length ?? 0) > 0 || (fig.only_b_captions?.length ?? 0) > 0) && (
        <div className="dim" style={{ marginTop: 6 }}>
          单侧独有 caption：
          {(fig.only_a_captions ?? []).map((c) => (
            <span key={`a-${c}`} className="pd-chip meas" style={{ marginLeft: 4 }} title="仅 A">
              A: {c}
            </span>
          ))}
          {(fig.only_b_captions ?? []).map((c) => (
            <span key={`b-${c}`} className="pd-chip meas" style={{ marginLeft: 4 }} title="仅 B">
              B: {c}
            </span>
          ))}
        </div>
      )}

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
  // K03: selection is an ID; the detail is fetched from the same cache
  // the list uses, so a retry refreshes the panel automatically.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const jobs = useQuery({ queryKey: ["jobs"], queryFn: listJobs });
  const capabilities = useQuery({
    queryKey: ["capabilities"],
    queryFn: getCapabilities,
  });
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

  // K02: every mining status (configured / unavailable reason) comes
  // from the capability payload — a single round-trip replaces ad-hoc
  // string matching on the side error text.
  const mineruCapability: EngineCapability | undefined = capabilities.data?.engines.find(
    (e) => e.name === "mineru_http",
  );
  const mineruConfigured = mineruCapability?.configured ?? false;
  const mineruReason = mineruCapability?.unavailable_reason ?? null;

  // K03: details come from a per-id query that polls while queued/running.
  const selected = useQuery({
    queryKey: ["comparison", selectedId],
    queryFn: () => (selectedId ? getComparison(selectedId) : Promise.reject()),
    enabled: Boolean(selectedId),
    refetchInterval: (q) => {
      const data = q.state.data as ComparisonInfo | undefined;
      if (!data) return false;
      if (data.status === "running" || data.status === "queued") return 2000;
      return false;
    },
  }).data;

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
      setSelectedId(info.comparison_id);
      queryClient.invalidateQueries({ queryKey: ["comparisons"] });
    },
  });
  const retry = useMutation({
    mutationFn: (id: string) => retryComparison(id),
    onSuccess: (info) => {
      // K03: refresh BOTH the list and the per-id detail so the panel
      // updates without a second "查看" click.
      queryClient.invalidateQueries({ queryKey: ["comparisons"] });
      queryClient.invalidateQueries({ queryKey: ["comparison", info.comparison_id] });
      queryClient.invalidateQueries({ queryKey: ["comparison-diff", info.comparison_id] });
    },
  });

  const documents = [...new Map((jobs.data ?? []).map((j) => [j.document_id, j])).values()];

  // K02: render the structured failure reason instead of the raw string.
  const renderCreateError = () => {
    if (!create.isError) return null;
    const err = create.error;
    if (err instanceof ApiError && err.code === "ENGINE_NOT_CONFIGURED") {
      const detail = (err.details || {}) as { missing?: string[]; engine?: string };
      return (
        <div className="banner warn" style={{ marginTop: 8 }}>
          MinerU 未配置：缺少
          {(detail.missing ?? ["MINERU_TOKEN"]).join(", ")}；服务启动时需通过
          <code>--env-file</code> 提供。已拒绝本次创建，未发起任何云端调用。
        </div>
      );
    }
    return (
      <div className="banner error" style={{ marginTop: 8 }}>
        创建失败：{err instanceof ApiError ? `[${err.code}] ${err.message}` : String(err)}
      </div>
    );
  };

  return (
    <div>
      <h2 style={{ fontSize: 16, margin: "0 0 12px" }}>解析器对比</h2>
      <div className="banner info">
        同源同页 + 同一批领域规则；实验与正式任务隔离（不写审核队列、不发基线）。
        MinerU 需要服务端 MINERU_TOKEN——未配置时该侧会如实标记为失败，不影响 Docling 侧。
      </div>
      {/* K02: capability panel — explains why buttons are disabled. */}
      <div
        className={`panel ${mineruConfigured ? "ok" : "warn"}`}
        style={{ padding: 12, marginBottom: 12 }}
      >
        <strong>引擎能力</strong>
        <ul style={{ margin: "6px 0 0 18px" }}>
          <li>
            Docling（本地）：{capabilities.data?.engines.find((e) => e.name === "docling_sdk")?.available ? "可用" : "不可用"}
          </li>
          <li>
            MinerU 云 API：
            {mineruConfigured ? (
              <span>已配置（凭据已加载；尚未向云端验证）</span>
            ) : (
              <span className="dim">
                未配置 — {mineruReason?.message ?? "缺少 MINERU_TOKEN"}
              </span>
            )}
          </li>
        </ul>
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
            // K02: pre-check at the UI layer mirrors the server's
            // ENGINE_NOT_CONFIGURED guard — no spinner, no error toast,
            // just a disabled button + the reason in the banner.
            disabled={
              !documentId ||
              create.isPending ||
              !mineruConfigured
            }
            title={!mineruConfigured ? "MinerU 未配置，无法创建对比" : ""}
            onClick={() => create.mutate()}
          >
            {create.isPending ? "创建中…" : "创建对比（Docling vs MinerU）"}
          </button>
        </div>
        {renderCreateError()}
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
                className={selectedId === c.comparison_id ? "selected" : ""}
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
                  {c.sides.map((s: { side: string; status: string; error: string | null }) => (
                    <span key={s.side} style={{ marginRight: 6 }} title={s.error ?? ""}>
                      {SIDE_LABELS[s.side] ?? s.side}：
                      {s.status === "done" ? "✓" : s.status === "failed" ? "✗" : "…"}
                    </span>
                  ))}
                </td>
                <td>
                  <button onClick={() => setSelectedId(c.comparison_id)}>查看</button>
                  {c.sides.some((s) => s.status === "failed") && (
                    <button
                      style={{ marginLeft: 6 }}
                      // K02: a retry that would push a not-configured
                      // engine is disabled; the per-side error_info tells
                      // the user which one is non-retryable.
                      disabled={retry.isPending || !mineruConfigured}
                      onClick={() => retry.mutate(c.comparison_id)}
                    >
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
              title={describeSideError(s.error_info, s.error)}
            >
              {SIDE_LABELS[s.side] ?? s.side}：{s.status}
              {s.error_info ? (
                <span className="dim">
                  {" "}
                  — [{s.error_info.code}] 阶段 {s.error_info.stage}
                  {s.error_info.retryable ? "（可重试）" : "（不可重试）"}
                  {s.error_info.batch_id ? ` · batch=${s.error_info.batch_id}` : ""}
                  {s.error_info.generation != null
                    ? ` · generation=${s.error_info.generation}`
                    : ""}
                </span>
              ) : s.error ? (
                <span className="dim"> — {s.error}</span>
              ) : null}
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