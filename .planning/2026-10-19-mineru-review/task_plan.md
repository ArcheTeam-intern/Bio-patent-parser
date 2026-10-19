# 2026-10-19 J01–J06 与 MinerU 比较失败复核

目标：只读复核新增实现和真实失败链路，交付可执行修改方案。

- [complete] 固定基线 dc7409c、HEAD 4893aaa；读取上轮方案与本轮执行报告。
- [complete] 分别复核 Standards 与 Spec；定位运行时 MinerU 失败原因。
- [complete] 运行本轮独立回归与有界反例；完整后端690通过4跳过1失败，前端37通过/build成功。
- [complete] 汇总证据、风险、具体修改与验收，报告已生成并核对最终交付。

约束：不修改业务源码，不提交、不重启服务、不自动重跑云任务；不输出凭据。

错误记录：先前探测 /api/health 与 /api/comparisons 均404；已核对 API 前缀为 /api/v1，比较列表读取成功。

回归失败：test_budget_timeout_terminates_only_the_registered_tree 的真实Docling合法提前完成，测试仍期望failed。列为K08，不将整轮报告为全绿。定向单项复跑再次失败，93.96秒、退出1，实际done。
