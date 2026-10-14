/**
 * WorkerHint（live-ops）：排队中的任务在没有任何 worker 轮询时必须给出
 * 可操作的提示（含与 API 一致的 --data-dir），worker 在线时不显示。
 */
import type { ReactElement } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as client from "../api/client";
import { WorkerHint } from "./WorkerHint";

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function status(online: number): client.QueueStatus {
  return {
    api_version: "v1",
    data_dir: "E:\\GoLandCode\\Bio-patent-parser\\data\\post-m1-demo",
    queued_jobs: 1,
    running_jobs: 0,
    workers: online > 0
      ? [{ worker_id: "worker-1", pid: 42, seconds_since_heartbeat: 1.2, online: true }]
      : [],
    online_worker_count: online,
    stale_after_seconds: 15,
  };
}

afterEach(() => vi.restoreAllMocks());

describe("WorkerHint", () => {
  it("renders the start command when jobs wait and no worker polls", async () => {
    const spy = vi.spyOn(client, "getQueueStatus").mockResolvedValue(status(0));
    wrap(<WorkerHint active />);
    await waitFor(() => expect(screen.getByText(/没有检测到解析进程/)).toBeTruthy());
    expect(spy).toHaveBeenCalled();
    const cmd = screen.getByText(/queue_worker --data-dir/);
    expect(cmd.textContent).toContain("data\\post-m1-demo");
  });

  it("disappears once a worker heartbeat is fresh", async () => {
    vi.spyOn(client, "getQueueStatus").mockResolvedValue(status(1));
    wrap(<WorkerHint active />);
    await waitFor(() => expect(client.getQueueStatus).toHaveBeenCalled());
    expect(screen.queryByText(/没有检测到解析进程/)).toBeNull();
  });

  it("does not query or render when no job is active", () => {
    const spy = vi.spyOn(client, "getQueueStatus").mockResolvedValue(status(0));
    wrap(<WorkerHint active={false} />);
    expect(spy).not.toHaveBeenCalled();
    expect(screen.queryByText(/没有检测到解析进程/)).toBeNull();
  });
});
