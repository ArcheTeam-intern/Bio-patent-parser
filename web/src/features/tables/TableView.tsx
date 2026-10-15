import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getPage, pageFigureUrl } from "../../api/client";

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
    return <div className="empty-state">第 {pageNumber} 页没有结构化表格（页面的表格块仍可在原文视图查看）。</div>;
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
  const rows: JSX.Element[] = [];
  for (let r = 0; r < table.row_count; r++) {
    const cells: JSX.Element[] = [];
    for (let c = 0; c < table.column_count; c++) {
      const cell = byPosition.get(`${r}:${c}`);
      if (!cell) {
        continue; // covered by a rowspan/colspan from above/left
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
  const [raw, setRaw] = useState(false);

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
  const hiddenCount = blocks.length - proseBlocks.length;
  const groups = mergeBlocks(proseBlocks);
  return (
    <div style={{ padding: 8 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
        <span className="dim">
          页面路由：{page.data?.route} · 块数：{blocks.length} · 合并为 {groups.length} 段
        </span>
        <span className="spacer" />
        <button
          aria-pressed={raw}
          title={raw ? "切换回阅读视图" : "按解析器原始切分逐块查看"}
          onClick={() => setRaw(v => !v)}
        >
          {raw ? "阅读视图" : "逐块查看"}
        </button>
      </div>
      {hiddenCount > 0 && !raw && (
        <div className="wb-hint" style={{ marginBottom: 6 }}>
          另有 {hiddenCount} 个文本块属于本页表格的单元格，已隐藏在「表格」标签页呈现（含颜色与行列结构）。
        </div>
      )}
      {raw ? (
        <table className="data">
          <thead>
            <tr>
              <th>类型</th>
              <th>角色</th>
              <th>文本</th>
            </tr>
          </thead>
          <tbody>
            {blocks.map((block: { block_id: string; kind: string; semantic_role?: string; text_raw: string }) => (
              <tr key={block.block_id}>
                <td className="dim">{block.kind}</td>
                <td className="dim">{block.semantic_role ?? "unknown"}</td>
                <td style={{ whiteSpace: "pre-wrap" }}>{block.text_raw}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="reading-flow">
          {groups.map((g, i) => (
            <TextGroup key={g.ids.join("|") || i} group={g} />
          ))}
        </div>
      )}
    </div>
  );
}

export interface TextBlockLike {
  block_id: string;
  kind: string;
  semantic_role?: string;
  text_raw: string;
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
