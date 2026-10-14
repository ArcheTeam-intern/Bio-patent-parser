/**
 * B01 high-risk navigation behaviour: a slow response for evidence A must
 * never overwrite the target of a later click on B; location comes from the
 * API payload (no id parsing); missing evidence surfaces a real error.
 */
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as client from "../../api/client";
import { useEvidence } from "./useEvidence";

function span(id: string, page: number, bbox: number[] | null): client.EvidenceData {
  return {
    api_version: "v1",
    evidence_id: id,
    document_id: "sha256:doc",
    baseline_id: "b1",
    page_number: page,
    bbox,
    char_start: 3,
    char_end: 12,
    raw_text: "RRRQRRKKRG",
    source_parser: "native",
    source_locator: "block-1",
    fragments: [
      {
        page_number: page,
        bbox,
        block_id: "block-1",
        table_id: null,
        cell_id: null,
        char_start: 3,
        char_end: 12,
        raw_text: "RRRQRRKKRG",
        source_artifact_id: null,
      },
    ],
    text_precision: "offset" as const,
    geometry_precision: bbox ? ("block" as const) : ("page" as const),
  };
}

afterEach(() => vi.restoreAllMocks());

describe("useEvidence", () => {
  it("loads the first evidence and exposes an API-derived target", async () => {
    vi.spyOn(client, "getEvidence").mockResolvedValue(span("ev-a", 9, [10, 20, 110, 40]));
    const { result } = renderHook(() => useEvidence("job-1", ["ev-a"]));
    await waitFor(() => expect(result.current.evidence?.evidence_id).toBe("ev-a"));
    expect(result.current.target).toEqual({ page: 9, bbox: [10, 20, 110, 40] });
  });

  it("a stale slow response cannot overwrite a newer selection", async () => {
    let resolveA: (v: client.EvidenceData) => void = () => {};
    const slowA = new Promise<client.EvidenceData>((r) => (resolveA = r));
    vi.spyOn(client, "getEvidence").mockImplementation((_job, id) => {
      if (id === "ev-a") return slowA;
      return Promise.resolve(span("ev-b", 3, [1, 2, 3, 4]));
    });
    const { result } = renderHook(() => useEvidence("job-1", ["ev-a", "ev-b"]));
    await waitFor(() => expect(client.getEvidence).toHaveBeenCalledWith("job-1", "ev-a"));

    act(() => result.current.select("ev-b"));
    await waitFor(() => expect(result.current.evidence?.evidence_id).toBe("ev-b"));
    expect(result.current.target?.page).toBe(3);

    await act(async () => {
      resolveA(span("ev-a", 9, [10, 20, 110, 40]));
      await slowA;
    });
    expect(result.current.evidence?.evidence_id).toBe("ev-b");
    expect(result.current.target?.page).toBe(3);
  });

  it("page-only evidence reports page with a null bbox (no invented box)", async () => {
    vi.spyOn(client, "getEvidence").mockResolvedValue(span("ev-c", 5, null));
    const { result } = renderHook(() => useEvidence("job-1", ["ev-c"]));
    await waitFor(() => expect(result.current.evidence?.evidence_id).toBe("ev-c"));
    expect(result.current.target).toEqual({ page: 5, bbox: null });
  });

  it("a missing evidence id surfaces a real error", async () => {
    vi.spyOn(client, "getEvidence").mockRejectedValue(new Error("404 not found"));
    const { result } = renderHook(() => useEvidence("job-1", ["ev-missing"]));
    await waitFor(() => expect(result.current.error).toContain("证据加载失败"));
    expect(result.current.target).toBeNull();
  });

  it("clear() drops the current selection and target", async () => {
    vi.spyOn(client, "getEvidence").mockResolvedValue(span("ev-a", 9, [10, 20, 110, 40]));
    const { result } = renderHook(() => useEvidence("job-1", ["ev-a"]));
    await waitFor(() => expect(result.current.evidence?.evidence_id).toBe("ev-a"));

    act(() => result.current.clear());
    expect(result.current.evidence).toBeNull();
    expect(result.current.target).toBeNull();
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("changing the selection key reloads the record evidence", async () => {
    const spy = vi.spyOn(client, "getEvidence").mockResolvedValue(span("ev-a", 9, [10, 20, 110, 40]));
    const { result, rerender } = renderHook(
      ({ key }) => useEvidence("job-1", ["ev-a"], key),
      { initialProps: { key: "rec-1" } },
    );
    await waitFor(() => expect(result.current.evidence?.evidence_id).toBe("ev-a"));
    expect(spy).toHaveBeenCalledTimes(1);

    rerender({ key: "rec-2" });
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.evidence?.evidence_id).toBe("ev-a"));
  });

  it("an in-flight response from the previous selection key never lands", async () => {
    let resolveSlow: (v: client.EvidenceData) => void = () => {};
    const slow = new Promise<client.EvidenceData>((r) => (resolveSlow = r));
    let call = 0;
    vi.spyOn(client, "getEvidence").mockImplementation(() => {
      call += 1;
      return call === 1 ? slow : Promise.resolve(span("ev-a", 3, [1, 2, 3, 4]));
    });
    const { result, rerender } = renderHook(
      ({ key }) => useEvidence("job-1", ["ev-a"], key),
      { initialProps: { key: "rec-1" } },
    );
    await waitFor(() => expect(client.getEvidence).toHaveBeenCalledTimes(1));

    rerender({ key: "rec-2" });
    await waitFor(() => expect(result.current.target?.page).toBe(3));

    // The stale first response (page 9) resolves late and must be dropped.
    await act(async () => {
      resolveSlow(span("ev-a", 9, [10, 20, 110, 40]));
      await slow;
    });
    expect(result.current.target?.page).toBe(3);
  });
});
