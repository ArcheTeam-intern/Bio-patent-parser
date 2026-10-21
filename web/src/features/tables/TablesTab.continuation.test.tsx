/** K11/K12: a page whose rows were folded into the PREVIOUS page's table
 * must show that table instead of an empty tab — and only when the previous
 * table is really marked as continuing. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as client from "../../api/client";
import { TablesTab } from "./TableView";

function pageData(pageNumber: number, tables: unknown[]): client.PageData {
  return {
    api_version: "v1",
    job_id: "job-1",
    page_number: pageNumber,
    width_pt: 595,
    height_pt: 842,
    route: "layout",
    blocks: [{ block_id: `p${pageNumber}-b0`, kind: "text", bbox: null, text_raw: "页脚" }],
    tables,
  };
}

function tableData(issues: string[], continuation: string | null) {
  return {
    table_id: "mineru:table:23:1",
    page_number: 23,
    row_count: 2,
    column_count: 2,
    cells: [
      { cell_id: "c0", row_index: 0, column_index: 0, rowspan: 1, colspan: 1, text_raw: "Strain", header_role: "column", bbox: null },
      { cell_id: "c1", row_index: 0, column_index: 1, rowspan: 1, colspan: 1, text_raw: "MIC", header_role: "column", bbox: null },
      { cell_id: "c2", row_index: 1, column_index: 0, rowspan: 1, colspan: 1, text_raw: "Chain104", header_role: "data", bbox: null },
      { cell_id: "c3", row_index: 1, column_index: 1, rowspan: 1, colspan: 1, text_raw: "2", header_role: "data", bbox: null },
    ],
    header_cell_ids: ["c0", "c1"],
    caption: null,
    structure_issues: issues,
    continuation_group: continuation,
    footnote_refs: [],
    bbox: [84.5, 74.9, 504.1, 751.91],
  };
}

function renderTab(pageNumber: number) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <TablesTab jobId="job-1" pageNumber={pageNumber} />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.restoreAllMocks());

describe("TablesTab cross-page continuation", () => {
  it("renders the previous page's continued table on a page with no table", async () => {
    const spy = vi.spyOn(client, "getPage").mockImplementation(async (_job, no) => {
      if (no === 24) return pageData(24, []);
      return pageData(23, [
        tableData(["TABLE_CONTINUES_ON_NEXT_PAGE: rows below the fold"], "mineru:table:23:1:continues"),
      ]);
    });
    renderTab(24);
    await waitFor(() => expect(screen.getByText(/没有独立表格/)).toBeTruthy());
    expect(screen.getByText("Chain104")).toBeTruthy();
    // Both pages are read: the case only exists because page 24 is empty.
    expect(spy).toHaveBeenCalledWith("job-1", 24);
    expect(spy).toHaveBeenCalledWith("job-1", 23);
  });

  it("keeps the honest empty state when the previous table is not a continuation", async () => {
    vi.spyOn(client, "getPage").mockImplementation(async (_job, no) => {
      if (no === 24) return pageData(24, []);
      return pageData(23, [tableData([], null)]);
    });
    renderTab(24);
    await waitFor(() => expect(screen.getByText(/没有结构化表格/)).toBeTruthy());
    expect(screen.queryByText(/没有独立表格/)).toBeNull();
  });

  it("shows the page's own tables without pulling in the previous page", async () => {
    vi.spyOn(client, "getPage").mockImplementation(async (_job, no) => {
      if (no === 23) return pageData(23, [tableData([], null)]);
      return pageData(22, []);
    });
    renderTab(23);
    await waitFor(() => expect(screen.getByText("Chain104")).toBeTruthy());
    expect(screen.queryByText(/没有独立表格/)).toBeNull();
    expect(screen.queryByText(/没有结构化表格/)).toBeNull();
  });

  // K12 follow-up: a table that is only the TAIL of the previous page's grid
  // must NOT be re-shown on the FOLLOWING page — it belongs to its own page.
  it("does not pull a table that only continues FROM the previous page forward", async () => {
    vi.spyOn(client, "getPage").mockImplementation(async (_job, no) => {
      if (no === 24) return pageData(24, []);
      return pageData(23, [
        tableData(
          ["TABLE_CONTINUED_FROM_PREVIOUS_PAGE: tail of the table that ends page 22"],
          "mineru:table:22:1:split",
        ),
      ]);
    });
    renderTab(24);
    await waitFor(() => expect(screen.getByText(/没有结构化表格/)).toBeTruthy());
    expect(screen.queryByText(/没有独立表格/)).toBeNull();
  });

  it("labels a table that is the tail of the previous page", async () => {
    vi.spyOn(client, "getPage").mockImplementation(async (_job, no) => {
      if (no === 22) return pageData(22, []);
      return pageData(23, [
        tableData(
          ["TABLE_CONTINUED_FROM_PREVIOUS_PAGE: the rows above the fold are on page 22"],
          "mineru:table:22:1:split",
        ),
      ]);
    });
    renderTab(23);
    await waitFor(() => expect(screen.getByText("接上页")).toBeTruthy());
    expect(screen.getByText(/本表是第 22 页表格的后半/)).toBeTruthy();
    // The continuation note is NOT counted as an extra structure problem.
    expect(screen.queryByText(/结构问题/)).toBeNull();
  });

  // K12 follow-up: the fold direction hides THIS page's top rows inside the
  // previous page's table — a page with its own table must say so.
  it("tells a page with its own table that its top rows live in the previous one", async () => {
    vi.spyOn(client, "getPage").mockImplementation(async (_job, no) => {
      if (no === 22) {
        return pageData(22, [
          tableData(
            ["TABLE_CONTINUES_ON_NEXT_PAGE: page 23's own table starts 31% down the page"],
            "mineru:table:22:0:continues",
          ),
        ]);
      }
      return pageData(23, [tableData([], null)]);
    });
    renderTab(23);
    await waitFor(() =>
      expect(screen.getByText(/本页顶部的行已经并进那张表/)).toBeTruthy(),
    );
    // The hint names the page whose table holds the rows.
    expect(screen.getByText(/第 22 页的表格延续到本页/)).toBeTruthy();
  });

  it("does not announce the split pair twice on the continuation page", async () => {
    vi.spyOn(client, "getPage").mockImplementation(async (_job, no) => {
      if (no === 25) {
        return pageData(25, [
          tableData(
            ["TABLE_CONTINUES_ON_NEXT_PAGE: this table is SPLIT across the fold"],
            "mineru:table:25:0:split",
          ),
        ]);
      }
      return pageData(26, [
        tableData(
          ["TABLE_CONTINUED_FROM_PREVIOUS_PAGE: the tail of page 25's table"],
          "mineru:table:25:0:split",
        ),
      ]);
    });
    renderTab(26);
    await waitFor(() => expect(screen.getByText("接上页")).toBeTruthy());
    expect(screen.queryByText(/本页顶部的行已经并进那张表/)).toBeNull();
  });

  it("marks an unverifiable top-of-page fragment as a suspicion", async () => {
    vi.spyOn(client, "getPage").mockImplementation(async (_job, no) => {
      if (no === 22) return pageData(22, []);
      return pageData(23, [
        tableData(
          [
            "TABLE_MAYBE_CONTINUED_FROM_PREVIOUS_PAGE: page 22 is outside the parsed range",
          ],
          null,
        ),
      ]);
    });
    renderTab(23);
    await waitFor(() => expect(screen.getByText("可能接上页")).toBeTruthy());
    expect(screen.getByText(/上一页不在本次解析范围内/)).toBeTruthy();
  });
});
