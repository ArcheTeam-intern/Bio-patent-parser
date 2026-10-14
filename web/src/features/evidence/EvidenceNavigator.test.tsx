/**
 * EvidenceNavigator（B01）：字段证据切换按钮与 1/N 导航只有在真实存在
 * 多条证据时出现；定位精度标签必须如实区分字符级/段落级/页级。
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import * as client from "../../api/client";
import { EvidenceNavigator, precisionLabel } from "./EvidenceNavigator";
import type { EvidenceState } from "./useEvidence";

function span(overrides: Partial<client.EvidenceData>): client.EvidenceData {
  return {
    api_version: "v1",
    evidence_id: "ev-1",
    document_id: "sha256:doc",
    baseline_id: "b1",
    page_number: 9,
    bbox: null,
    char_start: null,
    char_end: null,
    raw_text: null,
    source_parser: "docling_sdk",
    source_locator: "job:docling:#/texts/12",
    fragments: [],
    text_precision: "block",
    geometry_precision: "block",
    ...overrides,
  };
}

function state(overrides: Partial<EvidenceState>): EvidenceState {
  return {
    evidence: span({}),
    target: { page: 9, bbox: null },
    loading: false,
    error: null,
    select: vi.fn(),
    clear: vi.fn(),
    next: vi.fn(),
    prev: vi.fn(),
    position: { index: 1, total: 1 },
    ...overrides,
  };
}

describe("EvidenceNavigator", () => {
  it("hides prev/next for a single evidence and shows them for several", () => {
    const { rerender } = render(
      <EvidenceNavigator state={state({})} fieldEvidence={undefined} />,
    );
    expect(screen.queryByTitle("下一条证据")).toBeNull();

    rerender(
      <EvidenceNavigator
        state={state({ position: { index: 1, total: 2 } })}
        fieldEvidence={undefined}
      />,
    );
    expect(screen.getByTitle("下一条证据")).toBeTruthy();
    expect(screen.getByTitle("上一条证据")).toBeTruthy();
    expect(screen.getByText("1/2")).toBeTruthy();
  });

  it("wires prev/next to the state callbacks", () => {
    const next = vi.fn();
    const prev = vi.fn();
    render(
      <EvidenceNavigator
        state={state({ position: { index: 2, total: 3 }, next, prev })}
        fieldEvidence={undefined}
      />,
    );
    screen.getByTitle("下一条证据").click();
    screen.getByTitle("上一条证据").click();
    expect(next).toHaveBeenCalledTimes(1);
    expect(prev).toHaveBeenCalledTimes(1);
  });

  it("field evidence buttons appear only for multiple fields and select by id", () => {
    const select = vi.fn();
    const { rerender } = render(
      <EvidenceNavigator
        state={state({ select })}
        fieldEvidence={{ value: ["ev-v"] }}
      />,
    );
    expect(screen.queryByRole("button", { name: "数值" })).toBeNull();

    rerender(
      <EvidenceNavigator
        state={state({ select })}
        fieldEvidence={{ value: ["ev-v"], unit_raw: ["ev-u"] }}
      />,
    );
    screen.getByRole("button", { name: "数值" }).click();
    expect(select).toHaveBeenCalledWith("ev-v");
  });

  it("precision labels never upgrade a block bbox to character precision", () => {
    expect(
      precisionLabel(span({ geometry_precision: "block", text_precision: "offset", char_start: 3, char_end: 9 })),
    ).toContain("段落区域");
    expect(precisionLabel(span({ geometry_precision: "block" }))).toContain("无字符级坐标");
    expect(precisionLabel(span({ geometry_precision: "character" }))).toContain("字符级高亮");
    expect(precisionLabel(span({ geometry_precision: "page", bbox: null }))).toContain("页级证据");
  });
});