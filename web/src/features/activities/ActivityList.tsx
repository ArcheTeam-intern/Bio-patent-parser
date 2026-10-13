import { MeasurementData } from "../../api/client";
import { formatValueBase } from "./ValueFormatter";

export { formatValue } from "../sequences/SequenceList";


/** 活性值展示（design §7.5）：区间/误差/比较符完整呈现，绝不截成标量。 */

export function MeasurementDetail({
  record,
  onEvidenceClick,
}: {
  record: MeasurementData;
  onEvidenceClick?: (evidenceId: string) => void;
}) {
  return (
    <div>
      <dl className="detail-grid">
        <dt>指标</dt>
        <dd>{record.metric}</dd>
        <dt>数值</dt>
        <dd>
          <span className="mono">{formatValueBase(record.value)}</span>{" "}
          {record.field_evidence["value"]?.map((eid) => (
            <button key={eid} className="dim" style={{ padding: "0 6px" }} onClick={() => onEvidenceClick?.(eid)}>
              证据
            </button>
          ))}
        </dd>
        <dt>单位</dt>
        <dd>
          {record.unit_raw ?? "—"}
          {record.unit_normalized && record.unit_normalized !== record.unit_raw && (
            <span className="dim">（规范 {record.unit_normalized}）</span>
          )}
        </dd>
        <dt>实体</dt>
        <dd>{record.entity_id ?? "未关联"}</dd>
        <dt>靶标</dt>
        <dd>{record.target_raw ?? "未关联"}</dd>
        <dt>证据类型</dt>
        <dd>{evidenceKindLabel(record.evidence_kind)}</dd>
        <dt>数值类型</dt>
        <dd>{valueKindLabel(record.value.kind)}</dd>
      </dl>
      {record.value.kind === "unparsed" && (
        <div className="banner warn" style={{ margin: "0 12px 12px" }}>
          原文数值未能解析（{record.value.raw}）。请人工选择解析类型后修正。
        </div>
      )}
      <CheckList checks={record.checks} reasons={record.review_reasons ?? []} />
    </div>
  );
}

export function CheckList({ checks, reasons }: { checks: { check_id: string; outcome: string; details: Record<string, string> }[]; reasons: string[] }) {
  if (checks.length === 0 && reasons.length === 0) return null;
  return (
    <div style={{ padding: "0 12px 12px" }}>
      <div className="dim" style={{ marginBottom: 4 }}>检查结果</div>
      {checks.map((c, i) => (
        <div key={i} style={{ fontSize: 12, marginBottom: 2 }}>
          <span className={`status ${c.outcome === "pass" ? "ok" : c.outcome === "fail" ? "conflict" : "muted"}`}>
            {c.outcome === "pass" ? "通过" : c.outcome === "fail" ? "未通过" : "未知"}
          </span>{" "}
          <span className="mono dim">{c.check_id}</span>{" "}
          <span className="dim">{c.details["message"] ?? ""}</span>
        </div>
      ))}
      {reasons.map((r, i) => (
        <div key={`r${i}`} className="dim">· {r}</div>
      ))}
    </div>
  );
}

export function evidenceKindLabel(kind: string): string {
  switch (kind) {
    case "measured": return "实测";
    case "claim_threshold": return "权利要求阈值";
    case "general_statement": return "一般陈述";
    case "cited_result": return "引用结果";
    default: return "未确定";
  }
}

export function valueKindLabel(kind: string): string {
  switch (kind) {
    case "scalar": return "标量";
    case "range": return "区间";
    case "plus_minus": return "均值±误差";
    case "categorical": return "分类";
    default: return "待解析";
  }
}
