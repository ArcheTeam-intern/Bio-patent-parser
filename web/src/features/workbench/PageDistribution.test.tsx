/**
 * PageDistribution（结果分布总览）：页码计数只来自 record_pages；
 * 点击行跳页；无页码记录如实单独标注；没有记录时不渲染。
 */
import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PageDistribution } from "./PageDistribution";

describe("PageDistribution", () => {
  it("renders per-page sequence/activity counts and jumps on click", () => {
    const onJump = vi.fn();
    render(
      <PageDistribution
        pageCount={12}
        recordPages={{
          "seq-1": [9],
          "seq-2": [9, 10],
          "meas-1": [2],
        }}
        kinds={{ "seq-1": "sequence", "seq-2": "sequence", "meas-1": "measurement" }}
        currentPage={9}
        onJump={onJump}
      />,
    );
    // 三页有结果，按页码排序；第 9 页为当前页并高亮。
    const rows = screen.getAllByRole("button").filter(b => b.className.includes("pd-row"));
    expect(rows).toHaveLength(3);
    expect(rows[0].textContent).toContain("2");
    expect(rows[0].textContent).toContain("活性 1");
    expect(rows[1].className).toContain("current");
    expect(rows[1].textContent).toContain("序列 2");

    fireEvent.click(rows[0]);
    expect(onJump).toHaveBeenCalledWith(2);
  });

  it("counts a record on each of its evidence pages exactly once per page", () => {
    render(
      <PageDistribution
        pageCount={5}
        recordPages={{ "seq-x": [3, 3, 4] }}
        kinds={{ "seq-x": "sequence" }}
        currentPage={1}
        onJump={() => {}}
      />,
    );
    // 第 3 页重复出现两次也只计 1 次；第 3、4 两页各有一个"序列 1"。
    const chips = screen.getAllByText("序列 1");
    expect(chips).toHaveLength(2);
    const rows = screen.getAllByRole("button").filter(b => b.className.includes("pd-row"));
    expect(rows.map(r => r.querySelector(".pd-page")?.textContent)).toEqual(["3", "4"]);
  });

  it("honestly reports records without page locations instead of inventing pages", () => {
    render(
      <PageDistribution
        pageCount={5}
        recordPages={{ "seq-a": [], "meas-b": [] }}
        kinds={{ "seq-a": "sequence", "meas-b": "measurement" }}
        currentPage={1}
        onJump={() => {}}
      />,
    );
    expect(screen.getByText(/缺少可定位页码/).textContent).toContain("2 条");
    expect(screen.getByText(/序列 1/)).toBeTruthy();
    expect(screen.getByText(/活性 1/)).toBeTruthy();
    // 没有任何可跳转的页行
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("renders nothing when there are no records at all", () => {
    const { container } = render(
      <PageDistribution pageCount={5} recordPages={{}} kinds={{}} currentPage={1} onJump={() => {}} />,
    );
    expect(container.querySelector(".pd")).toBeNull();
  });
});
