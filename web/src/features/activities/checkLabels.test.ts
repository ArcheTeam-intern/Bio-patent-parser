/**
 * 检查结果中文翻译：已知 ID/文案必须翻译；未知文案原样回退（绝不吞信息）。
 */
import { describe, expect, it } from "vitest";
import {
  CHECK_LABELS,
  checkLabel,
  translateCheckMessage,
  translateReason,
} from "./checkLabels";

describe("checkLabels", () => {
  it("labels every check id the backends can emit", () => {
    for (const id of [
      "SEQ_DECLARED_LENGTH_MATCH",
      "SEQ_ALLOWED_ALPHABET",
      "SEQ_COMPLETENESS",
      "SEQ_REFERENCE_MATCH",
      "ACT_VALUE_PARSE",
      "ACT_UNIT_PRESENT",
      "ACT_ENTITY_LINK",
      "ACT_TARGET_LINK",
      "ACT_EVIDENCE_KIND",
      "EVIDENCE_EXISTS",
      "EVIDENCE_GEOMETRY_VALID",
    ]) {
      expect(CHECK_LABELS[id], id).toBeTruthy();
      expect(checkLabel(id)).not.toBe(id);
    }
  });

  it("translates the observed measurement/sequence messages", () => {
    expect(translateCheckMessage("categorical value: R")).toBe("分类判定：R");
    expect(translateCheckMessage("no unit recorded")).toBe("未记录单位");
    expect(translateCheckMessage("no entity linked")).toContain("未关联实体");
    expect(translateCheckMessage("target linked: 15 0126 1779")).toBe("已关联靶标：15 0126 1779");
    expect(translateCheckMessage("evidence kind undetermined")).toContain("未断言为实测");    expect(translateCheckMessage("3 evidence refs")).toBe("3 条证据引用");
    expect(translateCheckMessage("all evidence has geometry")).toContain("可定位");
    expect(translateCheckMessage("length not fully known")).toContain("无法与声明核对");
    expect(translateCheckMessage("all residues in allowed alphabet")).toContain("字母表");
    expect(translateCheckMessage("explicitly displayed fragment; full-listing completeness not claimed")).toContain("片段");
    expect(translateCheckMessage("no official reference to compare")).toContain("官方参考");
  });

  it("falls back to the original text for unknown messages (never hides)", () => {
    expect(translateCheckMessage("some future check output")).toBe("some future check output");
    expect(translateReason("a brand new reason")).toBe("a brand new reason");
    expect(checkLabel("FUTURE_CHECK")).toBe("FUTURE_CHECK");
  });

  it("translates review reasons", () => {
    expect(translateReason("measurement has no unit")).toBe("缺少单位");
    expect(translateReason("measurement has no linked entity")).toBe("实体未关联");
    expect(translateReason("declared length does not match observed length")).toBe(
      "声明长度与观察长度不一致",
    );
    expect(translateReason("REQUIRES_HUMAN_OR_REFERENCE_VERIFICATION")).toContain("人工");
  });
});
