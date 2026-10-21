import { useQuery } from "@tanstack/react-query";
import { figureFileUrl, listFigures } from "../../api/client";

/**
 * 图片视图（C07）：默认只看“当前页”，需显式切换“全文”；同时显示
 * 当前页/全文两个计数；点击页标题定位到对应页；查看大图/下载是独立动作
 * （卡片本身是链接，不干扰翻页同步）。区域坐标与图题来自真实解析结果——
 * 无图片/未生成/损坏分别如实说明，不显示占位图。
 */

const KIND_LABELS: Record<string, string> = {
  chart: "统计图",
  plot: "曲线图",
  diagram: "示意图",
  photo: "照片",
  picture: "图",
};

export function FiguresTab({
  jobId,
  onJump,
  currentPage,
  scope,
  onScopeChange,
}: {
  jobId: string;
  onJump: (page: number) => void;
  /** C07: current page from the shared page state (URL-synced). */
  currentPage: number;
  scope: "page" | "all";
  onScopeChange: (scope: "page" | "all") => void;
}) {
  const filtered = useQuery({
    queryKey: ["figures", jobId, scope, scope === "page" ? currentPage : "all"],
    queryFn: () => listFigures(jobId, scope === "page" ? currentPage : undefined),
    enabled: !!jobId,
  });
  // Total keeps its own cache key so switching scope never loses the count.
  const totals = useQuery({
    queryKey: ["figures", jobId, "total"],
    queryFn: () => listFigures(jobId),
    enabled: !!jobId,
  });

  if (filtered.isLoading) return <div className="empty-state">正在检测图片…</div>;
  if (filtered.error) return <div className="banner error">图片读取失败：{String(filtered.error)}</div>;

  const list = filtered.data?.figures ?? [];
  const total = totals.data?.total ?? filtered.data?.total ?? list.length;

  return (
    <div style={{ padding: 8 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
        <label className="dim">
          范围：{" "}
          <select
            aria-label="图片范围"
            value={scope}
            onChange={(e) => onScopeChange(e.target.value === "all" ? "all" : "page")}
          >
            <option value="page">当前页</option>
            <option value="all">全文</option>
          </select>
        </label>
        <span className="dim">
          {scope === "page"
            ? `第 ${currentPage} 页 ${list.length} 图 · 全文共 ${total} 图`
            : `全文 ${total} 图`}
        </span>
      </div>

      {list.length === 0 ? (
        <div className="empty-state">
          <div>
            {filtered.data?.note ??
              (scope === "page"
                ? `第 ${currentPage} 页没有图片。`
                : "本任务未检测到图片区域。")}
          </div>
          {scope === "page" && total > 0 && (
            <button onClick={() => onScopeChange("all")}>查看全文 {total} 个图</button>
          )}
        </div>
      ) : (
        <>
          <div className="dim" style={{ marginBottom: 8 }}>
            缩略图为源 PDF 该区域的真实裁切；点击“第 N 页”标题跳页，点击图片看大图/下载。
          </div>
          {[...groupByPage(list).entries()]
            .sort((a, b) => a[0] - b[0])
            .map(([page, items]) => (
              <div key={page}>
                <div
                  className="section-label"
                  style={{ margin: "10px 0 6px", cursor: "pointer" }}
                  onClick={() => onJump(page)}
                  title="跳到该页"
                >
                  第 {page} 页 · {items.length} 图
                  {page === currentPage ? "（当前页）" : ""}
                </div>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                  {items.map((f) => (
                    <a
                      key={f.figure_id}
                      className="figure-card"
                      href={figureFileUrl(jobId, f)}
                      target="_blank"
                      rel="noreferrer"
                      title="在新标签打开全分辨率 PNG（可下载）"
                    >
                      <img src={figureFileUrl(jobId, f)} loading="lazy" alt={f.caption ?? f.figure_id} />
                      <div style={{ padding: "4px 8px" }}>
                        <span className={`pd-chip ${f.kind === "chart" ? "meas" : "seq"}`}>
                          {KIND_LABELS[f.kind] ?? "图"}
                        </span>
                        <div className="dim" style={{ marginTop: 4, lineHeight: 1.5 }}>
                          {f.caption ?? "（无图题）"}
                        </div>
                      </div>
                    </a>
                  ))}
                </div>
              </div>
            ))}
        </>
      )}
    </div>
  );
}

function groupByPage<T extends { page_number: number }>(list: T[]): Map<number, T[]> {
  const byPage = new Map<number, T[]>();
  for (const f of list) {
    const arr = byPage.get(f.page_number) ?? [];
    arr.push(f);
    byPage.set(f.page_number, arr);
  }
  return byPage;
}
