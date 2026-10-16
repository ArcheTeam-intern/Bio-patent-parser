/** H08/P05：问题动作按钮严格由服务端 capability 驱动。 */
import { describe, expect, it } from "vitest";
import { reparseButtonFor } from "./IssuePanel";
import type { IssueAction } from "../../api/client";

const TABLE_ACTIONS: IssueAction[] = [
  { action: "dismiss", supported: true, label: "标记非目标（需原因）" },
  { action: "note", supported: true, label: "保留未解决（加说明）" },
  {
    action: "reparse",
    supported: true,
    label: "区域复解析",
    produces: "region OCR text candidate (grid NOT reconstructed)",
  },
];

const NO_PAGE_ACTIONS: IssueAction[] = [
  { action: "dismiss", supported: true, label: "标记非目标（需原因）" },
  {
    action: "reparse",
    supported: false,
    label: "区域复解析（不可用）",
    reason: "issue has no known page",
  },
];

describe("reparseButtonFor", () => {
  it("shows an ENABLED reparse button for a supported table-issue capability", () => {
    const button = reparseButtonFor(TABLE_ACTIONS, "open");
    expect(button.visible).toBe(true);
    expect(button.enabled).toBe(true);
    expect(button.title).toContain("NOT reconstructed");
  });

  it("shows a DISABLED button with the server's reason when unsupported", () => {
    const button = reparseButtonFor(NO_PAGE_ACTIONS, "open");
    expect(button.visible).toBe(true);
    expect(button.enabled).toBe(false);
    expect(button.title).toBe("issue has no known page");
  });

  it("hides the button for dismissed issues", () => {
    expect(reparseButtonFor(TABLE_ACTIONS, "dismissed").visible).toBe(false);
  });

  it("hides the button when the server sends no capability at all", () => {
    expect(reparseButtonFor(undefined, "open").visible).toBe(false);
  });
});
