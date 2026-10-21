import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  getPage,
  getPageChannels,
  pageFigureUrl,
  type PageChannelCell,
} from "../../api/client";

/**
 * 表格与原文视图（plan B12）：
 * - 表格 tab 展示真实 tables（合并头/header_role/分类 issue 来源在 IssuePanel）；
 * - 原文 tab 按页展示原生/结构来源的块并允许切换。
 * 结构缺失时如实显示"该页无表格/文本层为空"，不显示占位句冒充功能完成。
 */

export function TablesTab({ jobId, pageNumber }: { jobId: string; pageNumber: number }) {
  const page = useQuery({
    queryKey: ["page", jobId, pageNumber],
    queryFn: () => getPage(jobId, pageNumber),
    enabled: !!jobId,
  });

  if (page.isLoading) return <div className="empty-state">加载中…</div>;
  if (page.error) return <div className="banner error">读取失败：{String(page.error)}</div>;
  const tables = (page.data?.tables ?? []) as TableData[];
  if (tables.length === 0) {
    return (
      <div className="empty-state">
        第 {pageNumber} 页没有结构化表格（页面的表格块仍可在原文视图查看）。
        <div className="dim" style={{ marginTop: 6 }}>
          若上一页的表格延续到本页，MinerU 会把两页的行合并进上一页的那一个表；打开上一页的表格可见完整内容。
        </div>
      </div>
    );
  }
  return (
    <div style={{ padding: 8 }}>
      {tables.map((table) => (
        <TablePanel key={table.table_id} jobId={jobId} table={table}
          blocks={(page.data?.blocks ?? []) as { block_id: string; text_raw: string }[]} />
      ))}
    </div>
  );
}

function TablePanel({
  jobId,
  table,
  blocks,
}: {
  jobId: string;
  table: TableData;
  blocks: { block_id: string; text_raw: string }[];
}) {
  const blockText = new Map(blocks.map((b) => [b.block_id, b.text_raw]));
  const legends = (table.footnote_refs ?? [])
    .map((id) => blockText.get(id))
    .filter((t): t is string => !!t);
  return (
    <div className="panel" style={{ marginBottom: 12 }}>
      <div className="panel-header">
        <span className="mono dim">{table.table_id}</span>
        <span className="dim" style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          {table.row_count}×{table.column_count}
          {table.source === "geometry_reconstructed" && (
            <span className="status review" title="结构由文本块坐标重建（解析器未识别此表），需人工核对">
              几何重建
            </span>
          )}
          {table.structure_issues.length > 0 && (
            <span className="status conflict" title={table.structure_issues.join("；")}>
              结构问题 {table.structure_issues.length}
            </span>
          )}
          {table.structure_issues.some((i) => i.startsWith("TABLE_CONTINUES_ON_NEXT_PAGE")) && (
            <span
              className="status review"
              title="表格框触到页底：后面的行可能属于下一页，解析器把它们并入了这一个表"
            >
              跨页续表
            </span>
          )}
          {table.bbox && (
            <a href={pageFigureUrl(jobId, table.page_number, table.bbox)} target="_blank" rel="noreferrer">
              导出图像
            </a>
          )}
        </span>
      </div>
      {table.caption && <div className="dim" style={{ padding: "4px 8px" }}>{table.caption}</div>}
      <TableGrid table={table} />
      {legends.map((text, i) => (
        <div key={i} className="dim" style={{ padding: "6px 8px", borderTop: "1px dashed var(--border-soft)" }}>
          图例：{text}
        </div>
      ))}
    </div>
  );
}

interface TableCellData {
  cell_id: string;
  row_index: number;
  column_index: number;
  rowspan: number;
  colspan: number;
  text_raw: string;
  header_role: string;
  bbox: number[] | null;
}

interface TableData {
  table_id: string;
  page_number: number;
  row_count: number;
  column_count: number;
  cells: TableCellData[];
  header_cell_ids: string[];
  caption: string | null;
  structure_issues: string[];
  footnote_refs?: string[];
  source?: string;
  bbox?: number[] | null;
}

function TableGrid({ table }: { table: TableData }) {
  const byPosition = new Map<string, TableCellData>();
  for (const cell of table.cells) {
    byPosition.set(`${cell.row_index}:${cell.column_index}`, cell);
  }
  // Positions covered by a rowspan/colspan from an earlier cell. Only THESE
  // may be skipped: emitting no <td> for a genuinely absent cell shifted
  // every following cell one column left (the reviewer's "strainless" rows
  // under a rowspan). An absent-but-uncovered position gets an empty <td> so
  // the grid stays aligned.
  const covered = new Set<string>();
  for (const cell of table.cells) {
    const rowSpan = Math.max(1, cell.rowspan ?? 1);
    const colSpan = Math.max(1, cell.colspan ?? 1);
    for (let dr = 0; dr < rowSpan; dr++) {
      for (let dc = 0; dc < colSpan; dc++) {
        if (dr === 0 && dc === 0) continue;
        covered.add(`${cell.row_index + dr}:${cell.column_index + dc}`);
      }
    }
  }
  const rows: JSX.Element[] = [];
  for (let r = 0; r < table.row_count; r++) {
    const cells: JSX.Element[] = [];
    for (let c = 0; c < table.column_count; c++) {
      const cell = byPosition.get(`${r}:${c}`);
      if (!cell) {
        if (covered.has(`${r}:${c}`)) continue; // really covered by a span
        cells.push(
          <td
            key={`empty-${r}-${c}`}
            style={{ border: "1px solid var(--border-soft)", padding: "3px 8px" }}
          >
            {"\u00a0"}
          </td>,
        );
        continue;
      }
      const isHeader = cell.header_role !== "data";
      cells.push(
        <td
          key={cell.cell_id}
          rowSpan={cell.rowspan}
          colSpan={cell.colspan}
          className={isHeader ? "dim" : ""}
          style={{ border: "1px solid var(--border-soft)", padding: "3px 8px", background: isHeader ? "var(--bg)" : undefined }}
        >
          {cell.text_raw || "\u00a0"}
        </td>,
      );
    }
    rows.push(<tr key={r}>{cells}</tr>);
  }
  return (
    <div style={{ overflowX: "auto", padding: "0 8px 8px" }}>
      <table style={{ borderCollapse: "collapse", fontSize: 12 }}>{rows}</table>
    </div>
  );
}

export function TextTab({ jobId, pageNumber }: { jobId: string; pageNumber: number }) {
  const page = useQuery({
    queryKey: ["page", jobId, pageNumber],
    queryFn: () => getPage(jobId, pageNumber),
    enabled: !!jobId,
  });

  // D08: 原始块 / 阅读顺序 / 来源对照 — 三个视图共享同一份"按列阅读、跳
  // 家具"的解析后段落，区别只在呈现层。
  const [view, setView] = useState<"raw" | "reading" | "source">("reading");

  if (page.isLoading) return <div className="empty-state">加载中…</div>;
  if (page.error) return <div className="banner error">读取失败：{String(page.error)}</div>;
  const blocks = page.data?.blocks ?? [];
  if (blocks.length === 0) {
    return (
      <div className="empty-state">
        第 {pageNumber} 页文本层为空（扫描页或空白页）；如需文本请请求区域复解析（issues 面板）。
      </div>
    );
  }
  // 表格重建已吸收的块（cell.source_refs 指回 block_id）不再作为正文/气泡
  // 出现在阅读视图——它们是矩阵单元格，完整形态去「表格」标签页看。
  const absorbed = new Set<string>();
  for (const t of (page.data?.tables ?? []) as { cells: { source_refs?: string[] }[] }[]) {
    for (const c of t.cells ?? []) {
      for (const ref of c.source_refs ?? []) absorbed.add(ref);
    }
  }
  const proseBlocks = (blocks as TextBlockLike[]).filter((b) => !absorbed.has(b.block_id));
  const pageW = (page.data as unknown as { width_pt?: number; height_pt?: number });
  const enriched = proseBlocks.map((b) => ({
    ...b,
    page_width: b.page_width ?? pageW.width_pt ?? null,
    page_height: b.page_height ?? pageW.height_pt ?? null,
  }));
  const hiddenCount = blocks.length - enriched.length;

  // 阅读视图用列感知：家具（页眉/页脚/边距）单独排，主体分列后
  // 左→右、列内上→下；任一"行短句号"作为 chip 单独显示。
  const paragraphs = readingParagraphs(enriched);
  // 来源对照：按 source_parser 分桶，桶内再走 readingParagraphs，
  // 就能让 Docling 与 OCR 对同一区域分别呈现、互不掺混。
  const channels = groupBySourceChannel(enriched);

  return (
    <div style={{ padding: 8 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8, flexWrap: "wrap" }}>
        <span className="dim">
          页面路由：{page.data?.route} · 块数：{blocks.length} · 合并为 {paragraphs.length} 段
          {channels.length > 1 ? ` · 来源 ${channels.length} 通道` : ""}
        </span>
        <span className="spacer" />
        <button
          aria-pressed={view === "reading"}
          onClick={() => setView("reading")}
          title="按解析器返回顺序合并同段落，跳过表格/页眉/页脚"
        >
          阅读顺序
        </button>
        <button
          aria-pressed={view === "raw"}
          title="逐块查看原始解析器输出"
          onClick={() => setView("raw")}
        >
          原始块
        </button>
        <button
          aria-pressed={view === "source"}
          title="按 source_parser 分桶对照各通道文本"
          onClick={() => setView("source")}
        >
          来源对照
        </button>
      </div>
      {hiddenCount > 0 && view !== "raw" && (
        <div className="wb-hint" style={{ marginBottom: 6 }}>
          另有 {hiddenCount} 个文本块属于本页表格的单元格，已隐藏在「表格」标签页呈现（含颜色与行列结构）。
        </div>
      )}
      {view === "raw" ? (
        <table className="data">
          <thead>
            <tr>
              <th>来源</th>
              <th>类型</th>
              <th>角色</th>
              <th>文本</th>
            </tr>
          </thead>
          <tbody>
            {blocks.map((block: { block_id: string; kind: string; semantic_role?: string; text_raw: string; source_parser?: string | null }) => (
              <tr key={block.block_id}>
                <td className="dim">{block.source_parser ?? "—"}</td>
                <td className="dim">{block.kind}</td>
                <td className="dim">{block.semantic_role ?? "unknown"}</td>
                <td style={{ whiteSpace: "pre-wrap" }}>{block.text_raw}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : view === "source" ? (
        <SourceCompareView jobId={jobId} pageNumber={pageNumber} fallbackChannels={channels} />
      ) : (
        <div className="reading-flow">
          {paragraphs.map((g, i) => (
            <TextGroup key={g.ids.join("|") || i} group={g} />
          ))}
        </div>
      )}
    </div>
  );
}

const CHANNEL_LABELS: Record<string, string> = {
  native: "原生字形层",
  layout: "结构引擎（Docling/MinerU）",
  rapidocr_region: "区域 OCR（RapidOCR）",
  adopted_candidate: "人工采用候选（F09 消费输入）",
  unknown: "未知通道",
};

/**
 * E9/P05 + F08/P04+P05: the source comparison reads the RAW per-engine
 * artifacts (GET …/pages/n/channels) from the CURRENT published manifest
 * (hash-verified server-side). Each channel keeps its own lines AND table
 * cells so the reader can compare what every engine actually produced —
 * a table-only page still shows its channel. Channels that could not be
 * hash-verified (legacy jobs) and non-current stage attempts (history)
 * are labelled as such, never mixed into the current view. When a job has
 * no readable channels at all, it falls back to the fused channel buckets
 * with the fallback honestly labelled.
 */
function SourceCompareView({
  jobId,
  pageNumber,
  fallbackChannels,
}: {
  jobId: string;
  pageNumber: number;
  fallbackChannels: SourceChannel[];
}) {
  const channelsQuery = useQuery({
    queryKey: ["page-channels", jobId, pageNumber],
    queryFn: () => getPageChannels(jobId, pageNumber),
    enabled: !!jobId,
  });
  const rawChannels = channelsQuery.data?.channels ?? [];
  const history = channelsQuery.data?.history ?? [];
  if (rawChannels.length > 0 || history.length > 0) {
    return (
      <div className="source-compare">
        {rawChannels.map((ch) => {
          const tables = ch.tables ?? [];
          const conflicts = ch.content_conflicts ?? [];
          const supersedes = ch.native_superseded ?? [];
          return (
            <div key={`${ch.engine}:${ch.run ?? ""}`} className="source-channel">
              <div className="source-channel-head">
                <strong>{CHANNEL_LABELS[ch.engine] ?? ch.engine}</strong>
                <span className="dim">
                  {ch.run ? ` · ${ch.run}` : ""} · {ch.lines.length} 行
                  {tables.length > 0 ? ` · ${tables.length} 表` : ""} · {ch.artifact_rel}
                  {ch.manifest_verified === false ? " · 未校验（退化读取）" : " · 发布清单已校验"}
                </span>
              </div>
              {ch.lines.length > 0 && (
                <div className="mono" style={{ whiteSpace: "pre-wrap", fontSize: 12, lineHeight: 1.7 }}>
                  {ch.lines.map((ln, i) => (
                    <div key={i}>{ln.text}</div>
                  ))}
                </div>
              )}
              {tables.length === 0 && ch.lines.length === 0 && (
                <div className="dim">本通道在本页无文本行与表格。</div>
              )}
              {tables.map((table, tIdx) => (
                <div key={table.table_id || tIdx} style={{ marginTop: 6 }}>
                  <div className="dim" style={{ fontSize: 11 }}>
                    表 {table.table_id}
                    {table.caption ? ` · ${table.caption}` : ""}
                    {table.row_count != null ? ` · ${table.row_count}×${table.column_count ?? "?"}` : ""}
                  </div>
                  <table className="source-table">
                    <tbody>
                      {sourceTableRows(table).map((row, rIdx) => (
                        <tr key={rIdx}>
                          {row.map((cell, cIdx) => (
                            <td
                              key={cIdx}
                              colSpan={cell.colspan ?? 1}
                              rowSpan={cell.rowspan ?? 1}
                              className={
                                cell.header_role === "column" || cell.header_role === "row" || cell.header_role === "both"
                                  ? "source-table-header"
                                  : undefined
                              }
                            >
                              {cell.text || "\u00a0"}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ))}
              {(conflicts.length > 0 || supersedes.length > 0) && (
                <div className="dim" style={{ fontSize: 11, marginTop: 4 }}>
                  {conflicts.length > 0 ? `本页原生/OCR 内容冲突 ${conflicts.length} 处` : ""}
                  {conflicts.length > 0 && supersedes.length > 0 ? " · " : ""}
                  {supersedes.length > 0 ? `短原生片段被更完整 OCR 行取代 ${supersedes.length} 处（见侧车记录）` : ""}
                </div>
              )}
            </div>
          );
        })}
        {history.length > 0 && (
          <div className="source-channel" style={{ borderStyle: "dashed" }}>
            <div className="source-channel-head">
              <strong>历史尝试（非当前发布）</strong>
              <span className="dim"> · {history.length} 个 run</span>
            </div>
            <div className="dim mono" style={{ fontSize: 11, lineHeight: 1.8 }}>
              {history.map((h) => (
                <div key={h.run}>
                  {h.stage} · {h.run} · {h.status}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    );
  }
  const channels = fallbackChannels;
  if (channels.length === 0) return <div className="dim">本页暂无可对照的原始文本。</div>;
  return (
    <div className="source-compare">
      <div className="wb-hint" style={{ marginBottom: 6 }}>
        该任务的阶段制品不可读，退化为按融合块的来源标注分桶（非各引擎原始文件）。
      </div>
      {channels.map((c) => (
        <div key={c.parser} className="source-channel">
          <div className="source-channel-head">
            <strong>{CHANNEL_LABELS[c.parser] ?? c.parser}</strong>
            <span className="dim"> · {c.blockCount} 块 · {c.paragraphs.length} 段</span>
          </div>
          {c.paragraphs.length === 0 ? (
            <div className="dim">本通道在本页无文本块。</div>
          ) : (
            c.paragraphs.map((g, i) => (
              <TextGroup key={g.ids.join("|") || i} group={g} />
            ))
          )}
        </div>
      ))}
    </div>
  );
}

/** Group a channel table's cells into visual rows honouring spans. */
function sourceTableRows(table: { cells: PageChannelCell[] }): PageChannelCell[][] {
  const rows: PageChannelCell[][] = [];
  for (const cell of table.cells) {
    const rowIndex = cell.row ?? 0;
    while (rows.length <= rowIndex) rows.push([]);
    rows[rowIndex].push(cell);
  }
  return rows.filter((row) => row.length > 0);
}

export interface TextBlockLike {
  block_id: string;
  kind: string;
  semantic_role?: string;
  text_raw: string;
  bbox?: number[] | null;
  page_width?: number | null;
  page_height?: number | null;
  source_parser?: string | null;
}

export interface MergedGroup {
  kind: string;
  role: string;
  /** Original fragments in document order. */
  fragments: string[];
  ids: string[];
  /** True when the group is a run of cell-like short fragments (R/S/…). */
  cellLike: boolean;
}

function mergeable(b: TextBlockLike): boolean {
  const role = b.semantic_role ?? "unknown";
  return (
    b.text_raw.trim().length > 0 &&
    (b.kind === "text" || b.kind === "caption") &&
    role !== "title" &&
    role !== "header"
  );
}

/** Merge consecutive same-kind/same-role text blocks into reading groups.
 *
 * A page's raw blocks are parser line boxes — one sentence arrives as many
 * fragments and a native-parsed table as single letters. Grouping runs of
 * the same kind+role restores readable paragraphs; short fragments (R, S,
 * strain numbers) stay individually visible as chips instead of pretending
 * to be a sentence. Headers/titles/captions always stand alone.
 */
export function mergeBlocks(blocks: TextBlockLike[]): MergedGroup[] {
  const groups: MergedGroupWithFlag[] = [];
  for (const b of blocks) {
    const role = b.semantic_role ?? "unknown";
    const text = b.text_raw.trim();
    const prev = groups[groups.length - 1];
    if (mergeable(b) && prev && prev.canMerge && prev.kind === b.kind && prev.role === role) {
      prev.fragments.push(text);
      prev.ids.push(b.block_id);
      if (text.length <= 3) prev.cellLike = true;
      continue;
    }
    groups.push({
      kind: b.kind,
      role,
      fragments: text.length > 0 ? [text] : [],
      ids: [b.block_id],
      cellLike: mergeable(b) && text.length > 0 && text.length <= 3,
      canMerge: mergeable(b),
    });
  }
  return groups.map(({ canMerge: _omit, ...g }) => g);
}

// ---- D08 reading-order + source-compare -----------------------------------

/** Block columns: cluster mergeable blocks' x-centres, split when the gap
 * between consecutive sorted x-centres exceeds 60% of the typical column
 * width. Furniture (header/footer roles, top/bottom margins) is excluded
 * from the column analysis but listed at the top and bottom of the result.
 */

function _isFurniture(b: TextBlockLike): boolean {
  const role = b.semantic_role ?? "unknown";
  if (role === "header" || role === "footer") return true;
  if (b.bbox && b.page_width && b.page_height) {
    const [_x0, y0, _x1, y1] = b.bbox;
    if (y1 <= 0.08 * b.page_height || y0 >= 0.92 * b.page_height) return true;
  }
  return false;
}

function _xCenter(b: TextBlockLike): number | null {
  if (!b.bbox || b.bbox.length < 4) return null;
  return (b.bbox[0] + b.bbox[2]) / 2;
}

function _yTop(b: TextBlockLike): number {
  if (!b.bbox || b.bbox.length < 2) return 1e9;
  return b.bbox[1];
}

export function readingParagraphs(blocks: TextBlockLike[]): MergedGroup[] {
  const furnitureTop: TextBlockLike[] = [];
  const furnitureBottom: TextBlockLike[] = [];
  const body: TextBlockLike[] = [];
  for (const b of blocks) {
    if (_isFurniture(b)) {
      if (b.bbox && b.bbox.length >= 2 && b.bbox[1] < (b.page_height ?? 0) / 2) {
        furnitureTop.push(b);
      } else {
        furnitureBottom.push(b);
      }
      continue;
    }
    body.push(b);
  }

  // No geometry → fall back to plain merge (legacy behaviour).
  const centres = body
    .map((b) => _xCenter(b))
    .filter((c): c is number => c != null) as number[];
  if (centres.length === 0) {
    return [
      ...mergeBlocks(furnitureTop),
      ...mergeBlocks(body),
      ...mergeBlocks(furnitureBottom),
    ];
  }
  const sorted = [...centres].sort((a, b) => a - b);
  // 60% of the most common gap between consecutive centres = "column gap"
  // threshold; points further apart than that are in a different column.
  const gaps: number[] = [];
  for (let i = 1; i < sorted.length; i++) gaps.push(sorted[i] - sorted[i - 1]);
  let threshold: number;
  if (gaps.length >= 2) {
    const sortedGaps = [...gaps].sort((a, b) => a - b);
    const median = sortedGaps[Math.floor(sortedGaps.length / 2)] || 60;
    threshold = Math.max(40, median * 1.6);
  } else {
    // A single gap between x-centres IS the column boundary.
    threshold = Math.max(40, (gaps[0] || 60) * 0.5);
  }
  const columnBreaks: number[] = [];
  for (let i = 1; i < sorted.length; i++) {
    if (sorted[i] - sorted[i - 1] > threshold) columnBreaks.push((sorted[i - 1] + sorted[i]) / 2);
  }

  // Assign each body block to a column by x-centre.
  const columns: TextBlockLike[][] = columnBreaks.length + 1 >= 1
    ? Array.from({ length: columnBreaks.length + 1 }, () => [])
    : [[]];
  for (const b of body) {
    const cx = _xCenter(b);
    if (cx == null) {
      columns[0].push(b);
      continue;
    }
    let col = 0;
    for (const boundary of columnBreaks) {
      if (cx > boundary) col += 1;
      else break;
    }
    columns[col].push(b);
  }
  // Read each column top-down, then concatenate left-to-right.
  const out: MergedGroup[] = [];
  for (const col of columns) {
    col.sort((a, b) => _yTop(a) - _yTop(b));
    out.push(...mergeBlocks(col));
  }
  return [
    ...mergeBlocks(furnitureTop),
    ...out,
    ...mergeBlocks(furnitureBottom),
  ];
}

/** D08 source compare: group blocks by source_parser and per-channel
 * text content. Furniture and absorbed cells are excluded.
 */
export interface SourceChannel {
  parser: string;
  paragraphs: MergedGroup[];
  blockCount: number;
}

export function groupBySourceChannel(blocks: TextBlockLike[]): SourceChannel[] {
  const filtered = blocks.filter((b) => b.text_raw.trim().length > 0);
  const bySource = new Map<string, TextBlockLike[]>();
  for (const b of filtered) {
    const key = b.source_parser ?? "unknown";
    if (!bySource.has(key)) bySource.set(key, []);
    bySource.get(key)!.push(b);
  }
  const channels: SourceChannel[] = [];
  for (const [parser, items] of bySource.entries()) {
    channels.push({
      parser,
      paragraphs: readingParagraphs(items),
      blockCount: items.length,
    });
  }
  // Stable order: native first, then any others alphabetically.
  channels.sort((a, b) => {
    const an = a.parser === "native" ? 0 : 1;
    const bn = b.parser === "native" ? 0 : 1;
    return an - bn || a.parser.localeCompare(b.parser);
  });
  return channels;
}

type MergedGroupWithFlag = MergedGroup & { canMerge: boolean };

function TextGroup({ group }: { group: MergedGroup }) {
  const text = group.fragments;
  if (text.length === 0) return null;
  if (group.role === "title" || group.role === "header") {
    return <div style={{ fontWeight: 700, fontSize: 14, margin: "14px 0 4px" }}>{text.join(" ")}</div>;
  }
  if (group.role === "caption" || group.kind === "caption") {
    return <div className="dim" style={{ fontStyle: "italic", margin: "8px 0" }}>{text.join(" ")}</div>;
  }
  if (group.cellLike) {
    return (
      <div style={{ margin: "6px 0", display: "flex", flexWrap: "wrap", gap: 4 }}>
        {text.map((t, i) => (
          <span key={i} className="pd-chip muted">{t}</span>
        ))}
      </div>
    );
  }
  return (
    <p style={{ margin: "8px 0", lineHeight: 1.8, whiteSpace: "pre-wrap" }}>
      {/* Line boxes rejoin without a space after a hard hyphen break. */}
      {text.reduce((acc, cur, i) => (i === 0 ? cur : acc.endsWith("-") ? acc + cur : `${acc} ${cur}`), "")}
    </p>
  );
}
