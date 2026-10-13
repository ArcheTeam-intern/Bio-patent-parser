/** 解析器对比（design §9）：同源同页同规则才可比；无 gold 不显示准确率。 */

export function ComparePage() {
  return (
    <div>
      <h2 style={{ fontSize: 16, margin: "0 0 12px" }}>解析器对比</h2>
      <div className="panel">
        <div className="empty-state">
          <div style={{ fontSize: 15 }}>对比功能随 D5 交付</div>
          <div style={{ maxWidth: 480 }}>
            对比要求：同一文档（相同 SHA-256）、相同请求页集合、相同领域规则版本。
            满足条件后并列展示“仅 A / 仅 B / 字段不同 / 一致”的差异与证据位置。
            没有人工 gold 时只显示差异，不显示准确率、雷达图或“最优引擎”结论。
          </div>
        </div>
      </div>
    </div>
  );
}
