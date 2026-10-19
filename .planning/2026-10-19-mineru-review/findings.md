# 当前已确认事实

- HEAD 4893aaa，新增一个 J01–J06 修复提交；工作区原有未跟踪内容保留。
- 当前 API/worker 均运行；venv launcher + 基础 Python 子进程不能直接当作双服务实例。
- GET /api/v1/comparisons：cmp-d60a9c9dd9b5，MinerU attempt=3，2026-10-19T01:45:15Z 结束失败；异常明确缺少 MINERU_TOKEN。Docling 侧 done。
- 该失败发生在本地 adapter 构造前，不能推断 token失效、额度、云网络/CDN失败。
- capabilities 的 MinerU 是硬编码 available=false，使用名 mineru；实际比较执行名 mineru_http。
- 比较前端没有消费引擎可用性，允许创建与无条件重试；当前只有原始错误字符串。
- 当前不存在 .env/.env.local/.env.example，源码检索无 dotenv加载；仅在启动环境读取token。

已完成边界复核：

- 直接读取运行中 API PID3460 与 worker PID12348 的环境，仅输出布尔值：两者 MINERU_TOKEN 均不存在。没有输出凭据。
- 真实浏览器页面确认最近失败任务和可点击的重试按钮；受控页面拦截重试响应（无真实 POST）确认列表完成而详情仍显示旧失败。
- J01受控延迟轮询：b2/gen2/completed/history[b1] 被迟到 b1 失败响应覆盖为 b1/gen1/terminal_failed/history[]；主审已复跑。
- J04跨行OCR生成两条210，inventory conflict2、molecule_type unknown；声明field来源未被普通region OCR制品消费；主审复跑辅助领域链，持久化遗漏另由源码确认。
- J05真实领域链 MIC→mic 普通表头大小写，旧pair1当前pair0；主审复跑。
- J02/J03进程测试隔离、J06严格尺寸/旋转核验已修，无新确证缺陷。
- 当前前端独立37通过/build退出0；完整pytest退出1：690通过、4跳过、1失败，304.22秒。失败为预算测试实际done仍断言failed；4skip为3Docling live+1MinerU live开关未开。Standards定向36、Spec定向37均通过，均为完整集子集。
- 新方案8任务：K01配置、K02能力预检错误、K03详情同步、K04身份围栏、K05多行声明、K06字段证据、K07词/单位规范、K08确定性预算测试。报告 reports/2026-10-19_J01_J06_recheck_and_K01_K08_plan.md。
