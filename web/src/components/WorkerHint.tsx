import { useQuery } from "@tanstack/react-query";
import { getQueueStatus } from "../api/client";

/**
 * 排队中的任务在没有任何解析进程时给出可操作的提示。
 *
 * 空闲 worker 不持有任务租约，只有 presence 心跳能证明它在轮询；
 * 心跳过期即视为离线（进程崩溃后提示也会出现）。启动 worker 后
 * 提示会自动消失。
 */
export function WorkerHint({ active }: { active: boolean }) {
  const status = useQuery({
    queryKey: ["queue-status"],
    queryFn: getQueueStatus,
    refetchInterval: active ? 3000 : false,
    enabled: active,
  });

  if (!active || !status.data || status.data.online_worker_count > 0) return null;

  return (
    <div className="banner warn">
      <div>任务在排队，但没有检测到解析进程（worker），不会自动开始。</div>
      <div className="dim" style={{ marginTop: 4 }}>
        请在项目目录启动 worker（data-dir 必须与 API 一致）：
      </div>
      <code className="mono" style={{ fontSize: 12 }}>
        python -m bio_pdf.workflow.queue_worker --data-dir "{status.data.data_dir}"
      </code>
      <div className="dim" style={{ marginTop: 4 }}>
        启动后本提示会自动消失；若任务状态是“运行中”，请检查原进程是否已中断。
      </div>
    </div>
  );
}
