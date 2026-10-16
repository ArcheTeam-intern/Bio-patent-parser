import { useState } from "react";
import type { SequenceInventory, SequenceInventoryEntry } from "../../api/client";

/**
 * 序列清点表（C02）：每个声明 SEQ ID 一行——恢复与否都保留行。
 * 统计口径：声明数/可见数/完整/部分/缺失/冲突/范围外，绝不用候选条数
 * 冒充完整性；部分页范围明确标注覆盖页域，不冒充全文。
 */

const STATUS_LABELS: Record<SequenceInventoryEntry["status"], string> = {
  complete: "完整",
  partial: "部分",
  missing: "缺失",
  conflict: "冲突",
  declared: "仅声明",
  outside_scope: "范围外",
};

const STATUS_CLASS: Record<SequenceInventoryEntry["status"], string> = {
  complete: "muted",
  partial: "review",
  missing: "review",
  conflict: "review",
  declared: "muted",
  outside_scope: "muted",
};

export function SequenceInventoryPanel({
  inventory,
  onJump,
}: {
  inventory: SequenceInventory;
  onJump: (page: number) => void;
}) {
  const [showAll, setShowAll] = useState(false);
  const { summary, entries } = inventory;
  const scope = summary.scope;

  const notable = entries.filter((e) => e.status !== "complete");
  const visible = showAll ? entries : notable;

  const scopeText = scope.covers_full_document
    ? "全文覆盖"
    : scope.requested_pages != null && scope.document_page_count != null
      ? `覆盖第 ${scope.requested_pages[0]}–${scope.requested_pages[scope.requested_pages.length - 1]} 页 · 全文 ${scope.document_page_count} 页`
      : null;

  return (
    <div className="seq-inventory">
      <div className="section-label" style={{ display: "flex", gap: 8, alignItems: "center" }}>
        序列清单
        {summary.declared_total != null && (
          <span className="dim" style={{ fontWeight: 400 }}>
            声明的 SEQ ID 总数（&lt;160&gt;）：{summary.declared_total}
            {summary.declared_total_unseen ? `（另有 ${summary.declared_total_unseen} 个不在本范围）` : ""}
          </span>
        )}
        {scopeText && (
          <span className="dim" style={{ fontWeight: 400 }}>
            {scopeText}
          </span>
        )}
      </div>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", margin: "6px 0" }}>
        <span className="pd-chip muted">声明 {summary.observed_declarations}</span>
        <span className="pd-chip muted">已定位 {summary.located}</span>
        <span className="pd-chip seq">完整 {summary.complete}</span>
        {summary.partial > 0 && <span className="pd-chip meas">部分 {summary.partial}</span>}
        {summary.missing > 0 && <span className="pd-chip meas">缺失 {summary.missing}</span>}
        {summary.conflict > 0 && <span className="pd-chip meas">冲突 {summary.conflict}</span>}
        {summary.declared_only > 0 && <span className="pd-chip muted">仅声明 {summary.declared_only}</span>}
        {summary.outside_scope > 0 && <span className="pd-chip muted">范围外 {summary.outside_scope}</span>}
      </div>

      {visible.length > 0 && (
        <table className="data" style={{ marginBottom: 8 }}>
          <thead>
            <tr>
              <th>SEQ ID</th>
              <th>声明长度</th>
              <th>恢复长度</th>
              <th>状态</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            {visible.slice(0, 200).map((e, i) => (
              <tr
                key={`${e.seq_id_no ?? "?"}-${e.declaration_page}-${i}`}
                className="clickable"
                onClick={() =>
                  onJump(
                    e.residue_pages[0] ?? e.declaration_page,
                  )
                }
                title={`第 ${e.residue_pages[0] ?? e.declaration_page} 页`}
              >
                <td className="mono">{e.seq_id_no ?? "—"}</td>
                <td className="dim">{e.declared_length ?? "—"}</td>
                <td className="dim">{e.observed_length ?? "—"}</td>
                <td>
                  <span className={`status ${STATUS_CLASS[e.status]}`}>{STATUS_LABELS[e.status]}</span>
                </td>
                <td className="dim">{e.reasons.join("；")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {visible.length === 0 && <div className="wb-hint">全部声明均已完整恢复。</div>}
      {(notable.length < entries.length || entries.length > 200) && (
        <button onClick={() => setShowAll((v) => !v)}>
          {showAll ? "只看未完整项" : `显示全部 ${entries.length} 个声明`}
        </button>
      )}
    </div>
  );
}
