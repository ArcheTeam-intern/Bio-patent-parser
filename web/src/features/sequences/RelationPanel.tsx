import type { EntityData, RelationData, SequenceRecordData } from "../../api/client";

/**
 * 关联表（C04/方案 §2.5）：被测对象｜关联角色｜序列编号/片段｜状态｜证据。
 * 只展示真实登记的关系（link_status + 可解析证据 id）；未解决引用
 * 单独列出，不冒充已关联。点击证据在右侧面板打开。
 */

const PREDICATE_LABELS: Record<string, string> = {
  has_sequence: "包含序列",
  has_cdr: "包含 CDR",
  heavy_chain: "重链",
  light_chain: "轻链",
  assayed_entity: "被测对象",
  is_variant_of: "变体",
  composed_of: "组成",
  references_sequence: "引用序列",
};

const STATUS_LABELS: Record<RelationData["link_status"], string> = {
  linked: "已关联",
  ambiguous: "有歧义",
  missing_sequence: "序列缺失",
  outside_scope: "范围外",
  not_applicable: "不适用",
  no_evidence: "未找到证据",
};

export function RelationPanel({
  entities,
  relations,
  sequences,
  onSelectSequence,
}: {
  entities: EntityData[];
  relations: RelationData[];
  sequences: SequenceRecordData[];
  onSelectSequence: (sequenceId: string) => void;
}) {
  const entityById = new Map(entities.map((e) => [e.entity_id, e]));
  const seqById = new Map(sequences.map((s) => [s.sequence_id, s]));

  // 实体→序列类关系（清单/构建体/链）；被测关系在活性详情里呈现。
  const entitySeq = relations.filter((r) => seqById.has(r.object_id));
  if (entitySeq.length === 0) return null;

  const byEntity = new Map<string, RelationData[]>();
  for (const r of entitySeq) {
    const arr = byEntity.get(r.subject_id) ?? [];
    arr.push(r);
    byEntity.set(r.subject_id, arr);
  }

  return (
    <div style={{ marginTop: 8 }}>
      <div className="section-label">关联表（实体 → 序列，真实证据）</div>
      <table className="data">
        <thead>
          <tr>
            <th>被测对象</th>
            <th>关联角色</th>
            <th>序列编号</th>
            <th>状态</th>
            <th>证据</th>
          </tr>
        </thead>
        <tbody>
          {[...byEntity.entries()].slice(0, 100).map(([entityId, rels]) => {
            const entity = entityById.get(entityId);
            return rels.slice(0, 12).map((r, i) => {
              const seq = seqById.get(r.object_id);
              const seqNo = seq?.seq_id_no ?? seq?.sequence_id.slice(-8) ?? "?";
              return (
                <tr key={r.relation_id}>
                  <td>{i === 0 ? entity?.name_raw ?? entityId.slice(0, 12) : ""}</td>
                  <td>{PREDICATE_LABELS[r.predicate] ?? r.predicate}</td>
                  <td className="mono">SEQ ID {seqNo}</td>
                  <td>
                    <span
                      className={`status ${r.link_status === "linked" ? "muted" : "review"}`}
                    >
                      {STATUS_LABELS[r.link_status] ?? r.link_status}
                    </span>
                  </td>
                  <td>
                    {r.evidence_ids.slice(0, 2).map((ev) => (
                      <button
                        key={ev}
                        className="linkbtn"
                        title={`证据 ${ev}（打开对应序列查看详情与证据）`}
                        onClick={() => onSelectSequence(r.object_id)}
                      >
                        原文
                      </button>
                    ))}
                  </td>
                </tr>
              );
            });
          })}
        </tbody>
      </table>
    </div>
  );
}
