# v2 标注规范（B10）

1. 每个 scope 明确 document_id、pages 与 source_sha256；`scope_verified=true` 只在人
   工逐页核对“该范围内所有序列/活性都已列全”后填写。
2. 确认“没有序列”的页填 `no_sequence_region=true`（0 gold 的负例基准）；未标注范围
   既不计 FP 也不计 TP。
3. 序列：listing 标注 seq_id_no 与逐字符真值；正文片段标注 page 与残基串。区间/误差/
   比较符按人工阅读结果填写，不使用被评测的解析函数生成 gold。
4. 活性：完整元组（entity/target/metric/value/unit/conditions 原文）；图中无数值的曲线
   不进入精确测量 gold。
5. draft 可由工具辅助，`human_verified`+`status=verified` 只能由人工确认后填写。
