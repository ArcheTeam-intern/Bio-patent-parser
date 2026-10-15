/**
 * 检查结果的中文呈现（展示层翻译）。
 *
 * 后端检查 ID 与消息是稳定的机器契约（导出、审核决定都引用它们），
 * 这里只做界面翻译：ID → 中文检查名，消息按模板翻译；匹配不到的
 * 消息**原样回退显示**，并把原始 ID/消息放进 tooltip —— 界面可以更
 * 好懂，但不允许隐藏原文。
 */

export const CHECK_LABELS: Record<string, string> = {
  // 序列
  SEQ_DECLARED_LENGTH_MATCH: "声明长度核对",
  SEQ_ALLOWED_ALPHABET: "残基字母表",
  SEQ_POSITION_CONTINUITY: "位置连续性",
  SEQ_COMPLETENESS: "序列完整性",
  SEQ_REFERENCE_MATCH: "官方参考核对",
  // 活性
  ACT_VALUE_PARSE: "数值解析",
  ACT_UNIT_PRESENT: "单位记录",
  ACT_ENTITY_LINK: "实体关联",
  ACT_TARGET_LINK: "靶标关联",
  ACT_EVIDENCE_KIND: "证据类型",
  // 证据
  EVIDENCE_EXISTS: "证据存在性",
  EVIDENCE_GEOMETRY_VALID: "证据坐标",
};

const VALUE_KINDS: Record<string, string> = {
  scalar: "标量",
  range: "区间",
  plus_minus: "均值±误差",
  categorical: "分类",
  unparsed: "待解析",
};

const EVIDENCE_KINDS: Record<string, string> = {
  measured: "实测",
  claim_threshold: "权利要求阈值",
  general_statement: "一般陈述",
  cited_result: "引用结果",
  undetermined: "未确定",
};

export function checkLabel(checkId: string): string {
  return CHECK_LABELS[checkId] ?? checkId;
}

/** 检查消息翻译；未知文案原样返回（绝不吞掉信息）。 */
export function translateCheckMessage(message: string): string {
  const m = (message ?? "").trim();
  if (!m) return "";

  // 活性
  let match = m.match(/^value unparsed: (.*)$/);
  if (match) return `数值无法解析：${match[1]}`;
  match = m.match(/^categorical value: (.*)$/);
  if (match) return `分类判定：${match[1]}`;
  match = m.match(/^value kind: (\w+)$/);
  if (match) return `数值类型：${VALUE_KINDS[match[1]] ?? match[1]}`;
  match = m.match(/^unit present: (.*)$/);
  if (match) return `已记录单位：${match[1]}`;
  if (m === "no unit recorded") return "未记录单位";
  match = m.match(/^entity linked: (.*)$/);
  if (match) return `已关联实体：${match[1]}`;
  if (m === "no entity linked") return "未关联实体（药名尚未接入实体表）";
  match = m.match(/^target linked: (.*)$/);
  if (match) return `已关联靶标：${match[1]}`;
  if (m === "no target linked") return "未关联靶标";
  if (m === "evidence kind undetermined") return "未确定（未断言为实测）";
  match = m.match(/^evidence kind: (\w+)$/);
  if (match) return `${EVIDENCE_KINDS[match[1]] ?? match[1]}`;

  // 序列
  if (m === "declared length matches observed") return "声明长度与观察长度一致";
  match = m.match(/^declared (\d+) != observed (\d+)$/);
  if (match) return `声明长度 ${match[1]} ≠ 观察长度 ${match[2]}`;
  if (m === "length not fully known") return "长度未完整给出（无法与声明核对）";
  if (m === "all residues in allowed alphabet") return "所有残基均在允许字母表内";
  match = m.match(/^disallowed residues: (.*)$/);
  if (match) return `存在不允许的残基：${match[1]}`;
  if (m === "alphabet not defined for molecule type") return "该分子类型未定义字母表";
  if (m === "sequence marked complete") return "序列标记为完整";
  if (m === "explicitly displayed fragment; full-listing completeness not claimed")
    return "正文中明确展示的片段（不声称涵盖完整清单）";
  if (m === "sequence incomplete or trailing") return "序列不完整或尾部截断";
  if (m === "no official reference to compare") return "无官方参考序列可核对";

  // 证据
  match = m.match(/^(\d+) evidence refs$/);
  if (match) return `${match[1]} 条证据引用`;
  if (m === "no evidence ids present") return "没有证据引用";
  if (m === "evidence index unavailable") return "证据索引不可用";
  match = m.match(/^missing evidence: (.*)$/);
  if (match) return `证据缺失：${match[1]}`;
  if (m === "all evidence has geometry") return "所有证据均有坐标（可定位）";
  match = m.match(/^unanchored evidence: (.*)$/);
  if (match) return `缺少定位的证据：${match[1]}`;

  return m; // 未知文案原样显示 — 不做臆测翻译
}

/** 审核原因行（“· …”）的翻译。 */
export function translateReason(reason: string): string {
  const r = (reason ?? "").trim();
  if (!r) return "";
  if (r === "measurement has no unit") return "缺少单位";
  if (r === "measurement has no linked entity") return "实体未关联";
  if (r === "declared length does not match observed length") return "声明长度与观察长度不一致";
  if (r === "sequence is incomplete") return "序列不完整";
  if (r === "record has no evidence") return "记录没有证据";
  if (r === "REQUIRES_HUMAN_OR_REFERENCE_VERIFICATION") return "需人工或官方参考核对";
  if (r === "VALIDATION_CONFLICT") return "存在校验冲突";
  let match = r.match(/^value could not be parsed: (.*)$/);
  if (match) return `数值无法解析：${match[1]}`;
  match = r.match(/^sequence contains disallowed residues: (.*)$/);
  if (match) return `包含不允许的残基：${match[1]}`;
  match = r.match(/^missing evidence ids: (.*)$/);
  if (match) return `证据缺失：${match[1]}`;
  return r; // 未知原因原样显示
}
