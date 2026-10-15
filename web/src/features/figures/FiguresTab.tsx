import { useQuery } from "@tanstack/react-query";
import { figureFileUrl, listFigures } from "../../api/client";

/**
 * 图表视图：把结构引擎（Docling）检测到的统计图 / 曲线图 / 示意图区域,
 * 逐个作为独立图片展示, 可放大查看或导出 PNG。区域坐标与图题来自真实
 * 解析结果——原生引擎不做图表检测时如实说明, 不显示占位图。
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
}: {
  jobId: string;
  onJump: (page: number) => void;
}) {
  const figures = useQuery({
    queryKey: ["figures", jobId],
    queryFn: () => listFigures(jobId),
    enabled: !!jobId,
  });

  if (figures.isLoading) return <div className="empty-state">正在检测图表…</div>;
  if (figures.error) return <div className="banner error">图表读取失败：{String(figures.error)}</div>;

  const list = figures.data?.figures ?? [];
  if (list.length === 0) {
    return (
      <div className="empty-state">
        <div>{figures.data?.note ?? "本任务未检测到图表区域。"}</div>
      </div>
    );
  }

  const byPage = new Map<number, typeof list>();
  for (const f of list) {
    const arr = byPage.get(f.page_number) ?? [];
    arr.push(f);
    byPage.set(f.page_number, arr);
  }

  return (
    <div style={{ padding: 8 }}>
      <div className="dim" style={{ marginBottom: 8 }}>
        共检测到 {list.length} 个图区（缩略图为源 PDF 该区域的真实裁切, 点击可看大图/下载）
      </div>
      {[...byPage.entries()]
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
    </div>
  );
}
