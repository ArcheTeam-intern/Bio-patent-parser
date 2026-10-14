import { EvidenceData } from "../../api/client";
import { EvidenceState } from "./useEvidence";

/**
 * Evidence navigator (plan B01): shows which field each evidence belongs
 * to, 1/N navigation across the record's evidence, the source engine, and
 * an HONEST precision label — a block bbox is never presented as a
 * character-precise highlight.
 */

const FIELD_LABELS: Record<string, string> = {
  value: "数值",
  metric: "指标",
  unit_raw: "单位",
  unit: "单位",
  entity_raw: "实体",
  entity_id: "实体",
  target_raw: "靶标",
  target_id: "靶标",
  assay_id: "实验",
  conditions: "条件",
};

export function fieldEvidenceView(
  fieldEvidence: Record<string, string[]> | undefined,
): { field: string; evidenceId: string }[] {
  const out: { field: string; evidenceId: string }[] = [];
  for (const [field, ids] of Object.entries(fieldEvidence ?? {})) {
    for (const id of ids) {
      out.push({ field, evidenceId: id });
    }
  }
  return out;
}

export function EvidenceNavigator({
  state,
  fieldEvidence,
}: {
  state: EvidenceState;
  fieldEvidence: Record<string, string[]> | undefined;
}) {
  const entries = fieldEvidenceView(fieldEvidence);
  const evidence: EvidenceData | null = state.evidence;

  return (
    <div style={{ padding: "8px 12px", borderTop: "1px solid var(--border-soft)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
        <span className="dim">证据</span>
        {state.position.total > 1 && (
          <>
            <button style={{ padding: "0 8px" }} onClick={state.prev} title="上一条证据">
              ◀
            </button>
            <span className="dim">
              {state.position.index}/{state.position.total}
            </span>
            <button style={{ padding: "0 8px" }} onClick={state.next} title="下一条证据">
              ▶
            </button>
          </>
        )}
        {evidence?.source_parser && (
          <span className="dim">来源：{evidence.source_parser}</span>
        )}
      </div>

      {state.loading && <div className="dim">正在加载证据…</div>}
      {state.error && <div className="banner error">{state.error}</div>}

      {evidence && !state.loading && (
        <>
          <div className="dim" style={{ marginBottom: 4 }}>
            {precisionLabel(evidence)}
          </div>
          {evidence.raw_text && (
            <div className="mono" style={{ fontSize: 12, wordBreak: "break-all", marginBottom: 6 }}>
              {evidence.raw_text.length > 160
                ? evidence.raw_text.slice(0, 160) + "…"
                : evidence.raw_text}
            </div>
          )}
          {evidence.source_locator && (
            <div className="dim" style={{ fontSize: 11 }}>
              locator: {evidence.source_locator}
            </div>
          )}
        </>
      )}

      {entries.length > 1 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 6 }}>
          {entries.map((entry, i) => (
            <button
              key={`${entry.field}-${i}`}
              className={state.evidence?.evidence_id === entry.evidenceId ? "primary" : ""}
              style={{ padding: "1px 8px", fontSize: 12 }}
              onClick={() => state.select(entry.evidenceId)}
            >
              {FIELD_LABELS[entry.field] ?? entry.field}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** Honest label from the split precisions (plan B01/B02). */
export function precisionLabel(evidence: EvidenceData): string {
  const text = evidence.text_precision;
  const geometry = evidence.geometry_precision;
  if (geometry === "character") {
    return `字符级高亮${text === "offset" ? "（偏移 " + (evidence.char_start ?? "?") + "–" + (evidence.char_end ?? "?") + "）" : ""}`;
  }
  if (geometry === "cell") return "单元格级定位（表格）";
  if (geometry === "line" || geometry === "block") {
    const offsetNote =
      text === "offset" && evidence.char_start !== null
        ? `，文本偏移 ${evidence.char_start}–${evidence.char_end ?? "?"}`
        : "";
    return `段落区域${offsetNote}（无字符级坐标）`;
  }
  if (geometry === "page") {
    const offsetNote =
      text === "offset" && evidence.char_start !== null
        ? `，文本偏移 ${evidence.char_start}–${evidence.char_end ?? "?"}`
        : "";
    return `页级证据${offsetNote}（无区域坐标）`;
  }
  return "缺少定位证据";
}
