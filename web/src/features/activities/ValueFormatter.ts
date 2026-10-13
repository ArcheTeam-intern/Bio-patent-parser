import { ActivityValue } from "../../api/client";

/** 活性值格式化：完整保留区间/误差/比较符（design §7.5）。 */

export function formatValueBase(v: ActivityValue): string {
  switch (v.kind) {
    case "scalar":
      return `${v.operator ?? ""}${v.number ?? ""}`.trim();
    case "range":
      return `${v.lower ?? "?"}–${v.upper ?? "?"}`;
    case "plus_minus":
      return `${v.number ?? "?"} ± ${v.uncertainty ?? "?"}`;
    case "categorical":
      return v.qualifier ?? "(categorical)";
    case "unparsed":
      return `${v.raw}（待解析）`;
  }
}
