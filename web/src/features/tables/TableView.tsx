import { useQuery } from "@tanstack/react-query";
import { getJob, getPage } from "../../api/client";

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
        <div key={table.table_id} className="panel" style={{ marginBottom: 12 }}>
          <div className="panel-header">
            <span className="mono dim">{table.table_id}</span>
            <span className="dim">
              {table.row_count}×{table.column_count}
              {table.structure_issues.length > 0 && (
                <span className="status conflict" style={{ marginLeft: 8 }}>
                  结构问题 {table.structure_issues.length}
                </span>
              )}
            </span>
          </div>
          {table.caption && <div className="dim" style={{ padding: "4px 8px" }}>{table.caption}</div>}
          <TableGrid table={table} />
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
  const job = useQuery({ queryKey: ["job", jobId], queryFn: () => getJob(jobId), enabled: !!jobId });

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
  return (
    <div style={{ padding: 8 }}>
      <div className="dim" style={{ marginBottom: 6 }}>
        页面路由：{page.data?.route} · 块数：{blocks.length} · 文档解析器：{job.data?.document_id.slice(0, 18)}…
      </div>
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
    </div>
  );
}
