import { StageRun } from "../../api/client";

/** 阶段条：映射 STAGE_ORDER；ingest+inspect 合并为“源文件检查”（design §7.2）。 */

interface StageDef {
  key: string;
  label: string;
  members: string[];
}

const STAGE_GROUPS: StageDef[] = [
  { key: "ingest", label: "源文件检查", members: ["ingest", "inspect"] },
  { key: "native_parse", label: "原生解析", members: ["native_parse"] },
  { key: "layout_parse", label: "结构解析", members: ["layout_parse"] },
  { key: "normalize", label: "归一化", members: ["normalize"] },
  { key: "extract", label: "领域提取", members: ["extract"] },
  { key: "validate", label: "校验", members: ["validate"] },
  { key: "export", label: "导出", members: ["export"] },
];

const SEVERITY: Record<string, number> = { pending: 0, skipped: 0, succeeded: 1, running: 2, failed: 3 };

export function StageStrip({ stages }: { stages: StageRun[] }) {
  // Only the latest run per stage counts: a resumed job keeps the failed
  // attempts of earlier runs as history, which must not paint the strip red
  // after a successful re-run.
  const latest = new Map<string, StageRun>();
  for (const s of stages) {
    const prev = latest.get(s.stage_name);
    if (
      !prev ||
      (s.ended_at ?? "") >= (prev.ended_at ?? "") && s.attempt >= prev.attempt
    ) {
      latest.set(s.stage_name, s);
    }
  }

  return (
    <div className="stage-strip">
      {STAGE_GROUPS.map((group, idx) => {
        const runs = group.members
          .map((name) => latest.get(name))
          .filter((s): s is StageRun => s !== undefined);
        const state = aggregate(runs);
        const failedRun = runs.find((r) => r.status === "failed");
        return (
          <span key={group.key} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
            {idx > 0 && <span className="stage-arrow">→</span>}
            <span
              className={`stage-pill ${state}`}
              title={
                failedRun
                  ? `${group.label}: ${failedRun.error["message"] ?? "失败"}`
                  : `${group.label}: ${state}`
              }
            >
              {group.label}
              {state === "running" && " …"}
              {state === "failed" && " ✕"}
            </span>
          </span>
        );
      })}
    </div>
  );
}

function aggregate(runs: StageRun[]): string {
  if (runs.length === 0) return "pending";
  let worst = "succeeded";
  for (const r of runs) {
    if ((SEVERITY[r.status] ?? 0) > (SEVERITY[worst] ?? 0)) worst = r.status;
  }
  return worst;
}
