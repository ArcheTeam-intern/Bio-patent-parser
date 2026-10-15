/** 原文 tab 的块合并规则（阅读视图）。 */
import { describe, expect, it } from "vitest";
import { mergeBlocks } from "./TableView";

const b = (id: string, text: string, kind = "text", role = "body") => ({
  block_id: id,
  kind,
  semantic_role: role,
  text_raw: text,
});

describe("mergeBlocks", () => {
  it("merges consecutive body lines into one reading group", () => {
    const groups = mergeBlocks([
      b("1", "The MIC50 values for"),
      b("2", "peptides were determined"),
      b("3", "by broth dilution."),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].fragments).toEqual([
      "The MIC50 values for",
      "peptides were determined",
      "by broth dilution.",
    ]);
    expect(groups[0].cellLike).toBe(false);
  });

  it("keeps headers and titles standalone", () => {
    const groups = mergeBlocks([
      b("h", "Materials", "text", "header"),
      b("p", "Bacteria were cultured."),
    ]);
    expect(groups).toHaveLength(2);
    expect(groups[0].role).toBe("header");
  });

  it("flags runs of cell-like fragments (R/S/strain codes) without fusing them", () => {
    const groups = mergeBlocks([
      b("1", "R"),
      b("2", "S"),
      b("3", "R"),
      b("4", "17 0129 1317"),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].cellLike).toBe(true);
    expect(groups[0].fragments).toEqual(["R", "S", "R", "17 0129 1317"]);
  });

  it("starts a new group at an empty block boundary", () => {
    const groups = mergeBlocks([
      b("1", "First sentence."),
      b("2", "   "),
      b("3", "Second sentence."),
    ]);
    expect(groups.length).toBeGreaterThanOrEqual(2);
  });

  it("preserves the original ids for traceability", () => {
    const groups = mergeBlocks([b("a", "One"), b("bb", "Two")]);
    expect(groups[0].ids).toEqual(["a", "bb"]);
  });
});
