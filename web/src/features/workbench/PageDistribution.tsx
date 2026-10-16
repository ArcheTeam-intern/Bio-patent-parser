/**
 * 结果分布总览（用户需求）：序列/活性/表格/图表这些结果出现在哪些页。
 * 页码与计数完全来自 records API 的 record_pages 与 table_pages、以及
 * figures API（真实证据位置），不做任何推断；点击一行跳转到该页。
 * 无页码定位的记录单独、如实地标注出来，绝不猜一个页码。
 *
 * 四个结果 tab（序列/活性/表格/图表）共用这一份完整分布——
 * 之前每类结果只在各自 tab 出现，序列/活性页看不到表格/图表的分布。
 */

export interface PageDistributionProps {
  pageCount: number;
  recordPages: Record<string, number[]>;
  kinds: Record<string, "sequence" | "measurement">;
  currentPage: number;
  onJump: (page: number) => void;
  /** Tables per page (tables-tab overview); rendered as a third series. */
  tableCounts?: Record<string, number>;
  /** Figures per page (figures API); rendered as a fourth series. */
  figureCounts?: Record<string, number>;
}

export function PageDistribution({
  pageCount,
  recordPages,
  kinds,
  currentPage,
  onJump,
  tableCounts,
  figureCounts,
}: PageDistributionProps) {
  const perPage = new Map<number, { seq: number; meas: number; tab: number; fig: number }>();
  let unlocatedSeq = 0;
  let unlocatedMeas = 0;
  for (const [id, pages] of Object.entries(recordPages)) {
    if (!pages || pages.length === 0) {
      if (kinds[id] === "measurement") unlocatedMeas += 1;
      else unlocatedSeq += 1;
      continue;
    }
    for (const page of new Set(pages)) {
      const entry = perPage.get(page) ?? { seq: 0, meas: 0, tab: 0, fig: 0 };
      if (kinds[id] === "measurement") entry.meas += 1;
      else entry.seq += 1;
      perPage.set(page, entry);
    }
  }
  for (const [page, count] of Object.entries(tableCounts ?? {})) {
    const page_no = Number(page);
    if (!Number.isInteger(page_no) || page_no < 1 || !count) continue;
    const entry = perPage.get(page_no) ?? { seq: 0, meas: 0, tab: 0, fig: 0 };
    entry.tab += count;
    perPage.set(page_no, entry);
  }
  for (const [page, count] of Object.entries(figureCounts ?? {})) {
    const page_no = Number(page);
    if (!Number.isInteger(page_no) || page_no < 1 || !count) continue;
    const entry = perPage.get(page_no) ?? { seq: 0, meas: 0, tab: 0, fig: 0 };
    entry.fig += count;
    perPage.set(page_no, entry);
  }

  const pages = [...perPage.entries()].sort((a, b) => a[0] - b[0]);
  if (pages.length === 0 && unlocatedSeq + unlocatedMeas === 0) return null;
  const max = Math.max(1, ...pages.map(([, c]) => c.seq + c.meas + c.tab + c.fig));

  return (
    <div className="pd">
      <div className="section-label">结果分布（点击跳页）</div>
      <div className="pd-list">
        {pages.length === 0 && (
          <div className="wb-hint">尚无带页码定位的结果。</div>
        )}
        {pages.map(([page, counts]) => (
          <button
            key={page}
            className={`pd-row${page === currentPage ? " current" : ""}`}
            onClick={() => onJump(page)}
            title={`跳到第 ${page} 页（共 ${pageCount || "?"} 页）`}
          >
            <span className="pd-page">{page}</span>
            <span className="pd-bar">
              <span className="pd-seq" style={{ width: `${(counts.seq / max) * 100}%` }} />
              <span className="pd-meas" style={{ width: `${(counts.meas / max) * 100}%` }} />
              <span className="pd-tab" style={{ width: `${(counts.tab / max) * 100}%` }} />
              <span className="pd-fig" style={{ width: `${(counts.fig / max) * 100}%` }} />
            </span>
            <span className="pd-counts">
              {counts.seq > 0 && <span className="pd-chip seq">序列 {counts.seq}</span>}
              {counts.meas > 0 && <span className="pd-chip meas">活性 {counts.meas}</span>}
              {counts.tab > 0 && <span className="pd-chip tab">表格 {counts.tab}</span>}
              {counts.fig > 0 && <span className="pd-chip fig">图表 {counts.fig}</span>}
            </span>
          </button>
        ))}
        {unlocatedSeq + unlocatedMeas > 0 && (
          <div className="wb-hint" style={{ marginTop: 4 }}>
            另有 {unlocatedSeq + unlocatedMeas} 条记录缺少可定位页码
            {unlocatedSeq > 0 && `（序列 ${unlocatedSeq}`}
            {unlocatedSeq > 0 && unlocatedMeas > 0 && "、"}
            {unlocatedMeas > 0 && `活性 ${unlocatedMeas}`}
            {unlocatedSeq + unlocatedMeas > 0 && "）"}，不在上图内。
          </div>
        )}
      </div>
    </div>
  );
}
