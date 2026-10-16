/** @vitest-environment node */
import { describe, expect, it } from "vitest";
import {
  readingParagraphs,
  groupBySourceChannel,
  type TextBlockLike,
} from "./TableView";

function block(
  id: string,
  text: string,
  options: Partial<TextBlockLike> = {},
): TextBlockLike {
  return {
    block_id: id,
    kind: "text",
    semantic_role: "body",
    text_raw: text,
    bbox: [10, 20, 100, 30],
    page_width: 595,
    page_height: 842,
    source_parser: "native",
    ...options,
  };
}

describe("readingParagraphs (D08 column detection)", () => {
  it("groups single column with furniture first/last", () => {
    const blocks = [
      block("a", "Page header", {
        bbox: [200, 30, 400, 50],
        semantic_role: "header",
      }),
      block("b", "First paragraph", { bbox: [50, 200, 500, 220] }),
      block("c", "page number 32", {
        bbox: [300, 810, 309, 820],
        semantic_role: "footer",
      }),
    ];
    const out = readingParagraphs(blocks);
    expect(out.map((g) => g.fragments[0])).toEqual([
      "Page header",
      "First paragraph",
      "page number 32",
    ]);
  });

  it("respects columns in left-to-right order with top-down reads", () => {
    const blocks = [
      block("L1", "Left top", { bbox: [40, 100, 180, 120] }),
      block("L2", "Left bottom", { bbox: [40, 200, 180, 220] }),
      block("R1", "Right top", { bbox: [380, 100, 540, 120] }),
      block("R2", "Right bottom", { bbox: [380, 200, 540, 220] }),
    ];
    const out = readingParagraphs(blocks);
    // Two columns: each column reads top-down, and adjacent same-kind
    // lines inside a column merge into one paragraph.
    expect(out).toHaveLength(2);
    expect(out[0].fragments).toEqual(["Left top", "Left bottom"]);
    expect(out[1].fragments).toEqual(["Right top", "Right bottom"]);
  });

  it("falls back to linear order when no bbox is available", () => {
    const blocks = [
      block("a", "First", { bbox: undefined }),
      block("b", "Second", { bbox: undefined }),
    ];
    const out = readingParagraphs(blocks);
    // No geometry → everything is one column; adjacent same-kind lines
    // merge into a single paragraph with both fragments in order.
    expect(out).toHaveLength(1);
    expect(out[0].fragments.join(" ")).toContain("First");
    expect(out[0].fragments.join(" ")).toContain("Second");
  });
});

describe("groupBySourceChannel (D08 source compare)", () => {
  it("buckets blocks by source_parser and returns one channel per", () => {
    const blocks = [
      block("n1", "native line", { source_parser: "native" }),
      block("d1", "docling line", { source_parser: "docling" }),
      block("n2", "another native", { source_parser: "native" }),
    ];
    const channels = groupBySourceChannel(blocks);
    expect(channels.map((c) => c.parser)).toEqual(["native", "docling"]);
    expect(channels[0].blockCount).toBe(2);
    expect(channels[1].blockCount).toBe(1);
  });

  it("excludes absorbed/furniture blocks (empty text) from channels", () => {
    const channels = groupBySourceChannel([block("e", "   ", {})]);
    expect(channels).toEqual([]);
  });
});
