import { useMemo, useState } from "react";
import { ActivityValue, SequenceRecordData } from "../../api/client";

/**
 * Sequence display (design §7.4): raw + normalized side by side, every
 * unknown residue visually flagged at its position — never filtered out.
 */

// CLSI/EUCAST interpretive-category display words for disk-diffusion
// calls. Display-only gloss: the stored qualifier stays the exact
// evidence letter (raw must agree with the cell text per B07).
const CALL_WORDS: Record<string, string> = {
  S: "敏感 (Susceptible)",
  I: "中介 (Intermediate)",
  R: "耐药 (Resistant)",
  SDD: "剂量依赖敏感 (SDD)",
};

export function formatValue(v: ActivityValue): string {
  switch (v.kind) {
    case "scalar": {
      const text = `${v.operator ?? ""}${v.number ?? ""}`.trim();
      return text || v.raw;
    }
    case "range":
      return v.lower == null && v.upper == null
        ? v.raw
        : `${v.lower ?? "?"}–${v.upper ?? "?"}`;
    case "plus_minus":
      return v.number == null
        ? v.raw
        : `${v.number} ± ${v.uncertainty ?? "?"}`;
    case "categorical": {
      const q = v.qualifier ?? "";
      const word = CALL_WORDS[q.toUpperCase()];
      return word ? `${q} · ${word}` : (q || "(categorical)");
    }
    case "unparsed":
      return `${v.raw}（待解析）`;
  }
}

export function SequenceText({ sequence, residues }: { sequence: string; residues: SequenceRecordData["residues"] }) {
  const unknownPositions = useMemo(() => {
    const set = new Set<number>();
    for (const r of residues) {
      if (r.resolved === "false") set.add(Number(r.position));
    }
    return set;
  }, [residues]);

  const groups: React.ReactNode[] = [];
  for (let i = 0; i < sequence.length; i += 10) {
    const chunk = sequence.slice(i, i + 10);
    groups.push(
      <span key={i} className="residue-group">
        <span className="residue-pos">{i + 1}</span>
        {[...chunk].map((ch, j) => {
          const pos = i + j + 1;
          return unknownPositions.has(pos) ? (
            <span key={j} className="residue-unknown" title={`位置 ${pos}: 未知残基`}>
              {ch}
            </span>
          ) : (
            <span key={j}>{ch}</span>
          );
        })}
      </span>,
    );
  }
  return <div className="sequence-text">{groups}</div>;
}

export function SequenceDetail({ record }: { record: SequenceRecordData }) {
  const [copied, setCopied] = useState<string | null>(null);

  const copyWithState = async () => {
    await navigator.clipboard.writeText(record.normalized_sequence);
    setCopied(record.acceptance_state === "accepted" && record.is_complete ? "已复制（已接受且完整）" : "已复制（注意：该序列尚未接受或不完整）");
    setTimeout(() => setCopied(null), 3000);
  };

  return (
    <div>
      <dl className="detail-grid">
        <dt>SEQ ID</dt>
        <dd className="mono">{record.seq_id_no ?? record.sequence_id}</dd>
        <dt>分子类型</dt>
        <dd>{record.molecule_type}</dd>
        <dt>表示形式</dt>
        <dd>{record.representation}</dd>
        <dt>声明长度</dt>
        <dd>{record.declared_length ?? "未声明"}</dd>
        <dt>观察长度</dt>
        <dd>
          {record.observed_length}
          {record.declared_length !== null && record.observed_length !== record.declared_length && (
            <span className="status conflict" style={{ marginLeft: 8 }}>与声明长度不符</span>
          )}
        </dd>
        <dt>完整性</dt>
        <dd>{record.is_complete ? "完整" : <span className="status review">不完整 / 含未知残基</span>}</dd>
      </dl>

      <div style={{ padding: "0 14px 14px" }}>
        <div className="section-label">规范序列（未知残基红色标注）</div>
        <div className="sequence-box">
          <SequenceText sequence={record.normalized_sequence} residues={record.residues} />
        </div>
        <div className="section-label" style={{ margin: "12px 0 6px" }}>原始序列</div>
        <div className="sequence-box sequence-text dim">{record.raw_sequence}</div>
        <div style={{ marginTop: 12, display: "flex", gap: 8, alignItems: "center" }}>
          <button onClick={copyWithState}>复制规范序列</button>
          {copied && <span className="dim">{copied}</span>}
        </div>
      </div>
    </div>
  );
}
