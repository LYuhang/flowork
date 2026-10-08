# Flowork 应用复查与事件驱动优化清单

记录日期：2026-10-07。代码基线：`main@ccf4e98`。

2026-10-08 补充复查基线：`main@dc60797`。新增的浏览器连接恢复、资源释放与效率优化项见[本轮补充清单](#2026年10月8日全仓复查与行业方案对照)。这些新增项的修复和验收状态见末尾实施记录，不属于下文旧轮次的已完成验收。

范围包括主应用、API、Workflow 引擎、侧边栏插件和部署脚本。本轮新增发现为两项已局部复现的缺陷、三项性能风险，以及仍依赖轮询的主要链路。优先修复中文文件下载与并发引用，再推进 Agent 和 Task 的事件流及取消通知。

本清单保留扫描时的问题描述；实施与验收状态以下表及末尾进度记录为准。扫描、局部复现和静态检查通过不代表修复完成。上一轮已完成事项保留在[代码质量与风险优化清单](code-quality-and-risk-review-2026-10-07.zh-CN.md)，不重复计为本轮未完成问题。

**最新范围纠偏：用户要求避免为事件化而复杂化。下文“待事件化”不是必须全部替换的验收要求；仍须逐项评估成本、正确性与用户体验，合理保留轮询并记录理由。未部署的 worker 轮换方案已从工作树撤回。已上线功能不可仅为回退而破坏消息完整性或权限边界。**

### 执行语义：用户最新要求（优先于下文历史方案）

调用或执行发生错误时，平台不得自动重新派发、重跑或通过隐式重试掩盖错误。同步调用返回实际错误或超时；异步调用保存实际状态及错误，供 Agent 查询排查。再次执行由用户或 Agent 明确发起。撤销先前引入的槽序号和完成历史去重记录；文末旧槽方案记录仅是过程证据，不再作为交付要求。数据库业务结果、日志、Trace 持久化保留。相关后端简化已部署；实机结果与未通过项见文末，不把部署成功等同业务验收完成。

### 当前收尾清单（以本节与文末验收证据为准）

历史进度记录保留排查过程，不代表其中的“待部署/待定位”今天仍未解决。以下明确剩余范围，避免重复验收已经通过的链路。

| 范围 | 已有实机证据 | 仍需补齐 |
| --- | --- | --- |
| F1、F5、P5 | 文件下载与账号隔离；完成记录释放及去槽后的真实 CLI 单条/批量、Deployment 调用通过；inbox 扫描优化 | 原订单样例按用户要求替换为简单审批样例；当前验收详见文末，服务商失败保留且不冒充成功 |
| F2 引用授权 | Terra 跨窗口多标签页读取；模块并发/关闭/移动；真实页面退出、第二账号登录及原生引用隔离通过 | 双窗口原生引用在受控写入重叠下已补验；与账号、标签页生命周期和 Terra 读取证据合并覆盖，不声称高负载压测 |
| F3 / P1 对话消息 | 长历史分批回放对照、多窗口、离线恢复、历史翻页 | 刷新重复消息、两屏历史、编辑框溢出已实机复核；当前页流订阅与提前显示运行状态已部署并通过延迟流连接测试；原对话两条失败记录刷新前后内容、数量一致；运行故障证据见 P3 |
| F4 结果表格 | 20,000 行真实文件并发查询/事件循环响应；线上 300 行双并发查询与健康 API 对照；账号隔离 | 已补响应性证据；线上未构造 20,000 行执行结果，不声称容量压测 |
| P2 Task 日志 | 活跃批量任务双窗口、离线跨终态恢复、10 条日志逐项核对；刷新终态订阅修复已上线 | 本次批量日志链路通过；不扩张为所有执行故障均已覆盖 |
| P3 取消与运行故障 | 子进程退出、整体沙盒关闭、共享 worker 隔离；审批/取消竞争和迟到取消实机 | 独立进程退出、共享服务退出及自然租约失效已补实机证据；不代表所有基础设施故障均覆盖 |
| P4 CLI、P6/P7/P10 页面 | CLI 单条/批量人工审批状态查询；版本跟随/固定版本；详情恢复；评估终态 | 按文末相应证据，已通过的正常及恢复场景不重跑 |
| P8 原生下载确认 | Terra 正常确认、刷新恢复、读取结果；取消不传输、不重试 | 本轮下载确认链路通过 |
| P9 Preview / 后台任务 | POSIX 扫描与恢复；后台接口真实空流重连/账号隔离、隔离库并发顺序 | 当前后台记录无生产创建调用方；不新增执行机制或声称完整执行通过，后续接入生产者时再验完整生命周期 |

收尾时仍需整体检查工作树、部署内容与测试结果，并提交适用改动；不把上述局部通过等同整体目标完成。

### 行业方案参考及实施门槛（用户最新要求）

先参考成熟实现，再决定是否改造；若相对现有轮询增加维护复杂度且无足够收益，保持现状。暂不继续扩大事件化或部署未经评估的反向改造。

- [TanStack Query polling](https://tanstack.com/query/latest/docs/framework/react/guides/polling) 与 [query options](https://tanstack.com/query/latest/docs/framework/react/reference/interfaces/InfiniteQueryObserverOptions)：框架直接支持定期刷新及后台刷新控制。适用于允许秒级延迟的页面；优先复用现有查询框架，不自建通知状态机。
- [PostgreSQL 16 LISTEN](https://www.postgresql.org/docs/16/sql-listen.html)：监听会话接收通知；官方要求先建立监听，再读取初始状态，避免初始化竞争。因此只适合用通知唤醒读取，不能当作持久结果仓库；当前数据库可提供跨进程通知，不必额外引入消息中间件。
- [Kubernetes API concepts](https://kubernetes.io/docs/reference/using-api/api-concepts/)：list/watch 使用 resourceVersion，客户端需要处理历史不可用后的重新获取。这说明可靠事件消费本身有状态恢复成本；借鉴版本/游标思路，不为普通列表照搬集群控制面的完整 watch 系统。

Flowork 的选择是应用层判断，不是上述文档的统一结论：对话 token/日志等已有持续流优先复用通知和持久游标；普通列表/文件树/详情优先可见时查询、聚焦刷新、操作后刷新及合适的终态停止。权限复核不能为了表面上的零轮询被取消。

每项改动必须先写清现状成本、拟减少的成本、新增连接/状态/恢复路径，以及实测对照。没有足够证据时保留现状，不把“更先进”或“方便未来集群”作为单独改造理由。当前未部署的 Workflow 版本页简化草稿亦需经过该门槛及相应测试，不视为已完成。

执行状态补充：未部署的 Workflow 版本页反向改造草稿已撤回，包括移除 activity 的迁移 174；当前工作树恢复已部署的 173 方案及对应 API/前端测试，避免留下半成品迁移或不匹配测试。前端版本/固定图隔离回归 2 项通过（4.21 秒），证据 `/tmp/flowork-workflow-restored.log`。并未重启或修改线上服务。下一步普通页面的改造仍以成本对照为前提。

## 问题与优先级

| 编号 | 优先级 | 问题 | 证据 | 状态 |
| --- | --- | --- | --- | --- |
| F1 | 优先修复 | Storage 中文文件名下载可能失败 | 响应头构造局部复现 | 已部署；真实浏览器文件名、内容及跨账号拒绝通过 |
| F2 | 优先修复 | 并发引用可能丢失标签页访问记录 | 调用实际插件函数的并发模拟复现 | 已部署；受控原生并发、账号边界、标签页生命周期及 Terra 跨窗口读取通过 |
| F3 | 后续优化 | 长对话事件回放没有分页上限 | 静态调用链确认 | 已部署；分页回放、多窗口、离线恢复与失败历史展示证据见末尾 |
| F4 | 后续优化 | 结果表格重复加载整份结果并在 API 事件循环筛选排序 | 静态调用链确认 | 已改造部署；线上 12 组结果一致，自动化通过 |
| F5 | 后续优化 | 常驻推理进程的完成记录持续增长 | 静态与实测确认 | 槽方案已撤销；完成后 ACK 释放内存记录，去槽后 CLI 单条/批量及 Deployment 调用已实测 |

### F1 中文文件名下载

[storage.py](../api/src/vibecanvas_api/routes/storage.py) 的 `raw_storage_content` 将文件名直接拼入 `Content-Disposition: attachment; filename="..."`，涉及 Task 结果、POSIX 文件及对象存储文件分支。

使用项目运行环境中的 Starlette Response 构造相同响应头，`report.csv` 正常，`测试结果.csv` 触发 `UnicodeEncodeError`。这是响应头编码问题，不是文件不存在或权限不足；本轮没有通过生产下载请求复现。

建议统一使用 UTF-8 编码的 `filename*`，参考 [previews.py](../api/src/vibecanvas_api/routes/previews.py) 已有处理，统一处理引号及控制字符。

验收：英文、中文、空格、引号及特殊字符文件名均能下载，文件字节正确；覆盖实际 POSIX 下载入口和其余仍支持的分支，错误响应及权限判断保持正确。

### F2 并发引用覆盖标签页访问记录

[page-quotes.ts](../extension/src/page-quotes.ts) 在处理原生右键引用时，先读取 `pageQuoteTabs:<chatId>`，再追加当前标签页并整组写回。两个处理过程重叠时，可能读取到同一个旧值，随后互相覆盖。

局部复现调用实际 `registerPageQuotes`，模拟同一对话的两个标签页并发引用：交付了两个引用附件，但最终只保留一个标签页访问记录。附件文本仍存在，Agent 后续通过 Browser CLI 读取被覆盖记录对应的外部窗口标签页时可能失败。此前逐条引用的实测通过，不覆盖这个并发场景。

建议按对话串行更新，或按对话与标签页独立存储，避免整组读后覆盖。原生用户引用才可建立访问记录的边界须保留，不能通过伪造附件 JSON 获得访问权限。

验收：同一对话跨窗口、跨标签页并发引用时，附件与访问记录完整；不同对话和账号不串用；关闭、移动或替换标签页后不误读其他页面。真实侧边栏 Agent 验收使用 GPT-5.6-Terra。

### F3 长对话回放缺少分页上限

[agent_runs_repo.py](../api/src/vibecanvas_api/storage/agent_runs_repo.py) 的 `list_events` 一次读取指定游标之后的全部事件，并逐条解密；[agent_run_stream.py](../api/src/vibecanvas_api/services/agent_run_stream.py) 等这批结果返回后才开始向客户端发送。

长回合重连或从较早游标回放时，可能产生较大的内存峰值和首批消息等待时间。当前结论来自调用链，尚未量化生产影响。

建议按序号分批读取和解密，保留连续性检查、去重与持久化后发送的契约。不要用截断历史来降低内存。

验收：构造长回合事件，验证多批回放无缺失、重复或乱序；终态事件正确；记录首批返回延迟和峰值内存，与原实现比较。

### F4 结果表格重复全量处理

[tasks.py](../api/src/vibecanvas_api/routes/tasks.py) 的 `query_results` 每次翻页、搜索或筛选都会重新加载完整结果；加载已在线程中执行，但随后的计数、JSON 搜索、筛选和排序在 API 事件循环执行。

[batch_evaluation.py](../api/src/vibecanvas_api/services/batch_evaluation.py) 已有 32 MiB 交互结果上限，因此不是无限制读取。问题是重复处理和事件循环占用，不能描述为已经实测发生的线上阻塞。

建议先将完整查询计算移出 API 事件循环；按结果版本引入有内存上限的复用或索引，避免每翻一页重新解码整份结果。

验收：大结果文件翻页、筛选、排序正确，版本变化后不返回旧数据；并发查询期间无关 API 仍及时响应；缓存隔离、内存上限和文件大小限制保持有效。

### F5 常驻执行完成记录无回收边界

[executions.py](../engine/src/vibecanvas_engine/runtime/executions.py) 的 `WorkflowRuntime.completed` 在执行完成并 ACK 后保存防重复执行记录，目前生命周期内没有数量限制或清理。业务结果已释放，但轻量记录仍随调用总数增长。

当前要求：不为请求重发维护完成历史、槽序号或轮换机制；宿主机不自动重发执行。worker 只保留正在执行及尚未交付的结果，结果被确认接收后释放。数据库继续保存业务状态、结果和 Trace。

验收：完成并确认交付后内存记录释放；未完成调用正常保留；错误、超时直接返回或持久化，不自动重新执行。旧防重方案及实验仅保留为历史记录，不再作为交付要求。

## 扫描时仍依赖轮询的主要链路

下表频率为扫描时的代码设置，不是生产负载测量值。每个订阅者或执行可能分别建立轮询；不能把全仓定时器命中数当作独立缺陷数量。

| 编号 | 链路 | 当前行为及代码位置 | 建议 |
| --- | --- | --- | --- |
| P1 | Agent 对话 SSE | [agent_run_stream.py](../api/src/vibecanvas_api/services/agent_run_stream.py)：每 250ms 查询运行状态和事件 | 优先改为通知唤醒，再按持久化游标补齐；结合 F3 分页 |
| P2 | Task 日志 SSE | [sse_bridge.py](../api/src/vibecanvas_api/services/sse_bridge.py)：已订阅 Redis，但正常空闲等待仍每 200ms 查库 | 优先移除正常路径高频查库，通知负责唤醒，数据库保持权威 |
| P3 | 执行取消 | [batch_exec.py](../api/src/vibecanvas_api/background_tasks/batch_exec.py) 每 250ms、[scheduled_runs.py](../api/src/vibecanvas_api/background_tasks/scheduled_runs.py) 每 500ms、[turn_runtime.py](../api/src/vibecanvas_api/streaming/turn_runtime.py) 的 Agent 回合每 750ms 检查取消状态 | 优先改成持久化取消请求加跨进程通知 |
| P4 | CLI 人工审批 | [local_approval_client.py](../api/src/vibecanvas_api/flowork_cli/local_approval_client.py)：等待期间每秒请求决定 | 按最新决定暂不事件化；改为遇到审批自动后台执行，CLI 返回 run_id 和审批链接，支持主动查询 |
| P5 | 沙盒通用作业通道 | [sandbox_entry.py](../api/src/vibecanvas_api/sandbox_entry.py) 的 `serve_loop_parallel` 默认每 20ms 扫描 inbox；[warm.py](../api/src/vibecanvas_api/services/sandbox/warm.py) 还有作业完成检查 | 评估使用 Unix socket 等进程间通知，保留完成确认、取消与回收语义 |
| P6 | Workflow 最新版本和文件树 | [workflow.ts](../web/src/lib/api/queries/workflow.ts) 的 `useWorkflowHead`、[vfs.ts](../web/src/lib/api/queries/vfs.ts)：启用时每 3 秒查询 | 版本发布与文件变化事件触发刷新，重连后核对 |
| P7 | 执行与任务详情 | [workflow-history.ts](../web/src/lib/api/queries/workflow-history.ts)：活跃执行详情每秒、历史每 3 秒；[TaskDetailPage.tsx](../web/src/pages/tasks/TaskDetailPage.tsx)：活跃任务约每 5 秒 | 统一订阅资源状态变化，按当前可见内容刷新 |
| P8 | 插件下载确认 | [sidepanel.ts](../extension/src/sidepanel.ts)：每秒查询，同时已有 `DOWNLOAD_CONFIRM_CHANGED` 和 `BROWSER_SESSION_CHANGED` 处理 | 优先做小范围精简：使用通知，打开或恢复侧边栏时补查，过期由明确事件或截止时间处理 |
| P9 | Preview 与后台任务事件流 | [previews.py](../api/src/vibecanvas_api/routes/previews.py)、[chats.py](../api/src/vibecanvas_api/routes/chats.py)：空闲期间每秒查事件表 | 纳入事件唤醒与按游标回放方案 |
| P10 | 评估页面 | [EvaluationTab.tsx](../web/src/pages/tasks/EvaluationTab.tsx)：每 3 秒查询，评估结束后仍继续 | 至少先停止终态高频查询，再接入状态事件 |

其他较低频的查询包括：主对话、侧边栏和画布每 30 秒对话核对；草稿每 20 秒刷新；沙盒状态每 5 秒；部分资源、组织及权限列表每 15 至 30 秒。应随对应资源事件逐步收敛，而不是分别增加一套通知框架。

SSE 是服务端向浏览器发送数据的传输方式，不代表服务端内部已经由事件驱动。P1、P2、P9 正是外部推送、内部轮询的实现。

## 已使用事件机制或不应直接删除的定时行为

- Deployment 正常调用等待：[deployment_observer.py](../api/src/vibecanvas_api/services/deployment_observer.py) 订阅执行状态变化，超时由截止时间触发。
- Workflow 执行桥接：[workflow_execution_driver.py](../api/src/vibecanvas_api/services/sandbox/workflow_execution_driver.py) 正常等待执行事件与持久化命令通知。故障重试中的短暂 sleep 不等同于正常持续查库。
- 定时任务到点调度：[schedule_timer.py](../api/src/vibecanvas_api/services/schedule_timer.py) 根据下一个截止时间等待，PostgreSQL 通知打断等待。P3 指定时执行的取消检查，不是调度器仍每分钟扫描。
- WebSocket 心跳用于发现失联；倒计时和耗时显示只更新本地界面；指标曲线定时刷新属于采样展示需求，不应全部删除。
- 权限撤销检查、断线恢复核对、租约续期、闲置资源回收及失败退避需要保留明确的正确性保障。改为事件机制时，应设计恢复路径，不能假设通知永不丢失。

## 实施原则与顺序

1. 先修 F1、F2，并补充有针对性的回归与实机验证。
2. 优先处理 P1、P2、P3，结合 F3 完成长对话分批回放。
3. 精简 P8、P10 等已具备通知或终态判断的小范围轮询，再统一前端资源订阅。
4. 处理 P4、P5 及其余事件流，随后优化 F4、F5。

事件只负责唤醒，持久化状态保持权威。订阅建立、首次读取、断线重连和重复通知必须有明确顺序，避免“查询后才订阅”造成漏事件。通知不携带访问授权，读取结果时仍执行用户、组织和资源权限校验。

复用现有事件机制，按链路渐进替换；不为消除轮询引入多套事件总线、长期双轨兼容或隐式自动重跑。POSIX 文件变化涉及多个写入进程和可能的网络存储，不能直接假定本地文件监听覆盖所有写入。

## 验收标准

- 正常无状态变化时，验证数据库查询次数不再随高频 tick 持续增长；记录变更前后同条件数据。
- 覆盖通知重复、断线期间漏通知、重连、服务重启、取消与完成竞争、审批超时及权限撤销；持久化状态和界面保持一致。
- 取消只影响对应执行，不能误停同 worker 的其他调用。同步超时不能隐式变成异步，Human 审批异步语义保持一致。
- 主应用、画布对话及侧边栏分别验证历史回放、继续对话、状态展示和停止行为；事件方案不得造成历史缺失、重复或乱序。
- 插件相关 Agent 实测使用 GPT-5.6-Terra，核对真实工具调用和最终结果。
- 修复完成需部署真实服务验收；本地测试或构建通过不能单独标记完成。

## 本轮检查记录

Web ESLint 与仓库现有 Ruff 配置检查通过。现有 Ruff 主要启用 E9、F，不能代表异步、性能或权限行为无问题。工作树 Gitleaks 扫描约 27.22 MB，未发现敏感信息泄露；本轮未重新扫描完整 Git 历史。

F1、F2 已进行受控局部复现，其余为代码调用链和静态检查结论。本轮没有修改线上应用、开展生产压力测试或完成全功能实机验收，不将此清单视为完整安全审计。

## 共享通知组件设计与实施进展

用户确认需要支持多 worker，以及未来组件跨机器部署。当前采用 PostgreSQL 持久化状态与提交通知，抽出 [state_notifications.py](../api/src/vibecanvas_api/services/state_notifications.py) 作为共享组件：按通道与事件循环复用监听连接，按资源 ID 唤醒订阅者，断线重连后触发核对，最后一个订阅退出时清理连接。读取端继续负责授权，通知不承载业务数据。

状态广播与任务领取保持分离。已有执行框架负责任务领取和执行归属；本组件不广播执行任务，也不代替防重与持久化历史。不同机器连接同一 PostgreSQL 时可以复用；未来若各服务使用独立数据库，可靠跨服务发布再采用事务 Outbox 与 CDC，不在本轮新增消息中间件。

参考：[PostgreSQL NOTIFY](https://www.postgresql.org/docs/16/sql-notify.html)、[LISTEN 初始化顺序](https://www.postgresql.org/docs/16/sql-listen.html)、[Redis Pub/Sub 交付语义](https://redis.io/docs/latest/develop/pubsub/)、[Redis Streams](https://redis.io/docs/latest/develop/data-types/streams/)、[事务 Outbox](https://microservices.io/patterns/data/transactional-outbox.html)、[Debezium Outbox](https://debezium.io/documentation/reference/stable/transformations/outbox-event-router.html)。

本地实施进展如下，均未达到真实服务验收完成状态：

- F1 已改为 UTF-8 下载文件名响应头，POSIX 文件及相关回归共 7 项通过。
- F2 改为按对话与标签页独立保存访问记录，避免并发读后覆盖；引用相关 8 项测试和插件 TypeScript 检查通过。
- P1 与 F3 已实现 Agent 通知唤醒、最多 256 条分批读取，并保留独立的权限复核和心跳定时器。初轮 Agent 仓库、等待组件与 Deployment 观察器组合 16 项通过。
- P3 的 Agent 取消路径已改为独立命令通道，避免每个输出事件触发取消查询；批量与定时任务尚待接入。共享组件重构后的组合回归正在验证。
- 其余项目继续按本清单推进。数据库迁移 167 尚未应用到线上，不能将本地测试描述为线上已生效。

### Task 日志通知接入（本地验证，未部署）

- P2：Task SSE 改为 PostgreSQL 提交通知唤醒、持久化日志分页回放；移除原 Redis 缓冲与每 0.2 秒数据库轮询。心跳和权限复核保留，不触发日志查询。
- 新增迁移 168：Task 日志插入提交后通知共享组件。重连沿用共享监听器的状态核对。
- 补修顺序风险：同一 Task 的日志在分配 ID 前获取任务行锁，防止较大 ID 先提交、游标跳过较小 ID。尚需补充并发事务专项测试。
- 实际 PostgreSQL 下，日志顺序、游标续读、空闲不轮询、提交前不可见/提交后唤醒等组合测试 6 项通过（16.92 秒）；Ruff 通过。
- Agent 组件重构组合回归 26 项通过；新增取消等待专项所在测试文件 3 项通过。
- 尚未进行这批改动的线上部署和真实页面验收，不代表整份清单已完成。Task Redis 发布端清理、批量/定时取消通知等继续处理。

### Task 取消等待与并发写入回归（本地，未部署）

- P3：批量取消、定时单次执行取消均接入共享通知组件；取消状态更新触发独立命令通道，日志输出不会反复触发取消查询。
- 删除批量/定时 worker 旧的 Redis 日志发布路径。迁移 168 同时包含日志通知与取消通知触发器。
- 批量、定时、Task CLI、日志 SSE、权限撤销组合回归：88 项通过（62.32 秒）。取消等待测试确认空闲期间不重复查询，收到通知后才检查取消状态。
- PostgreSQL 双事务并发日志测试：1 项通过（12.07 秒），验证第二个写入等待前一事务提交，最终游标顺序一致。
- 尚待真实数据库取消触发器专项、跨进程恢复与线上部署验收；此阶段不标记整体完成。

### 弱网完整性补充（本地，未部署）

- Task 前端 SSE 增加非终态 EOF 重连；重复 ID 去重；解析失败不确认游标。重连请求显式使用应用已接收游标，覆盖底层库解析消息前自动记录的原始 ID。
- 两项前端测试通过：中途 EOF 后补读并去重；JSON 解析失败后仍补读该条。ESLint 通过。
- 数据库取消提交、通知连接终止后恢复、权限撤销等专项组合 9 项通过（13.78 秒）。
- 通知连接配置统一命名为 `STATE_NOTIFICATION_DATABASE_URL`，安装文档及 `.env.example` 同步；删除已无调用方的 Redis Task 通道模块及对应旧测试，保留权限撤销测试。
- 线上弱网、切后台、刷新、主对话及侧边栏完整性验收仍未完成。前端模拟测试不替代这些验收。

### 首批线上部署与回放验收（2026-10-07 UTC）

- 前端 `pnpm build`（含 TypeScript、Vite、部署路径检查）通过。
- 已将首批后端、前端和插件源码更新至 `/opt/flowork`，重启 `flowork.service`；迁移 166→167→168 成功，服务 active。测试账号部署启用数、活跃任务数均为 0。
- 真实已完成批量任务 `85ee0ecf-1d8e-4444-b051-17c2cb72fc9e`：SSE 读取 5 条后主动断开，以 Last-Event-ID 重连补回 13 条，18 条 ID、类型、payload 与 REST 持久化日志逐条一致，重复数 0。
- 这是线上 HTTP 回放验证；运行中写入、浏览器弱网/后台/刷新、Terra 侧边栏验收仍待完成，不以此代替完整验收。

### P8 插件下载确认改造

- 删除侧边栏每秒 `DOWNLOAD_CONFIRM_LIST` 查询；改为初始化、重新获得焦点/可见、WebSocket 恢复、下载确认/浏览器会话变化时刷新。
- 实际下载确认不存在独立 TTL；沿用现有捕获/会话生命周期，不新增到期策略。补上观察超限、标签页跨窗口移动导致失效时的事件通知。
- 侧边栏和原生下载测试 22 项通过，TypeScript 检查通过。新增测试确认空闲 60 秒没有额外查询，状态事件及重连会刷新。
- 已执行线上 `launch.sh extension` 构建并发布下载包，未重启主服务。真实浏览器加载新版插件后仍需验收，已安装的旧插件不会因服务器 ZIP 更新而自动替换。

### P9 Preview / 后台任务事件流（开发中）

- 新增共享 `event_batches`：有界读取历史页，空闲等待提交通知，心跳与权限检查不触发事件表读取。
- Preview 文件变更及 Chat 后台任务流已接入该函数，移除一秒轮询；使用 `aclosing` 在外层流退出时显式释放监听。
- 新增迁移 169，按 Preview 存储范围及 Chat ID 通知。此迁移与这批路由改动尚未部署。
- 待验证事项包括不同文件/不同后台 Job 并发提交的游标顺序，以及真实连接断开后的恢复；不能由前面的 Task 测试替代。
- 回归正在运行：`/tmp/flowork-review-preview-events.log`，执行会话 33018；首轮测试开始后补充了显式关闭监听的代码，需针对该变化补验。

### 高频文本事件与 P9 并发补验（本地，未部署）

- 高频 Agent 文本：在编号/落库前合并同一消息、同一元数据的连续 `message_delta`；40ms 窗口、8KiB 累计阈值，其他事件形成边界。原始单个超大事件不拆分，阈值限制新增合并量。
- 生产端使用容量 16 的队列传递事件、对慢消费者施加背压；生产迭代始终在同一协程中，保留上下文变量连续性。输出仍经过原先的先持久化、再发布逻辑。
- 文本聚合与 Turn 回归 18 项通过（15.34 秒）：500 片文本的完整性、字节上限、工具/消息边界、静默期间超时刷新、生产异常先送出已有文本、慢消费背压和清理。
- P9：Preview 范围内分配事件 ID 前获取事务锁；后台任务同一 Chat 跨 Job 分配事件 ID 前获取事务锁，避免提交顺序与游标顺序相反。Preview 并发/后台状态机/等待组件 6 项通过；不同后台 Job 并发提交及通知专项 1 项通过。
- 这些改动尚未部署。主应用/侧边栏真实高频输出、网络断开恢复、观察写入次数及延迟的验收仍需完成。

### 多窗口发现与边界验收补充（开发中，尚未部署）

- 新增 Chat lifecycle 通知（迁移 170），仅在回合创建或状态改变的事务提交后广播，不随每个 token 触发历史重载。主对话、画布对话、插件内嵌对话各自订阅，替换 30 秒轮询发现新回合。
- 通知只是失效提示；首次连接及重连都重新核对数据库快照，实际消息继续走带游标的持久化事件流。一个窗口离开不会取消其他窗口订阅。
- 避免“开始快照尚未返回，完成通知已到达”被合并丢弃：正在重建期间收到新通知，结束后再次读取。
- 前端活动发现、终态恢复、画布对话 15 项通过；文本合并及等待 9 项通过；真实 PostgreSQL 两个订阅者在提交后同时唤醒及空闲/权限撤销 2 项通过。
- 用户要求补足 corner cases：同对话多窗口；不同对话不串流；断网/弱网/EOF；刷新/后台恢复；重复或格式错误事件；长历史分页；高频文本与慢消费者；停止/完成/取消竞态；账号/空间切换及权限撤销；通知连接重建；监听、队列、数据库连接清理。
- 上述为验收范围，不代表全部通过。自动化、HTTP 实测、浏览器实测分别记录。插件 Agent 使用 Terra。尚需部署后实测；旧的实验文档不属于本次变更。

### 第二批部署与主对话真实验收（2026-10-07 UTC）

- `flowork.service` 已重新部署并恢复 active；迁移 169、170 成功，新前端构建与部署路径检查通过。P9、文本聚合及多窗口回合通知现已上线。
- 新增通知入口沿用私有 Chat 鉴权；跨账号集成测试 1 项通过。测试库销毁后 DBOS 后台线程有清理期日志，测试退出码为 0，非生产连接错误。
- 真实 Terra 主对话：`d494948c-4011-48d1-9027-6cf18d35d36d`，两个独立浏览器上下文。第二个窗口自动发现新回合，在运行中断网，主窗口完成后恢复第二个窗口，再刷新。
- 三种展示状态的完整助手正文（截至终止标识）逐字相同，浏览器错误 0；刚重连的快照尚无消息时间，刷新后出现，因此不能直接把包含操作栏/时间的全部 DOM 文本当消息正文比较。时间稳定性需另行检查。
- 首轮脚本未校验选中的 Chat ID，落入此前 QA 对话；不作为指定新会话的验收证据。已修正为使用正式深链入口并在发送前严格校验两个窗口的 Chat ID。
- 新版插件侧边栏 Terra 多窗口/刷新验收进行中。其它未完成清单仍有效，没有标记全量验收完成。

### 插件真实验收与执行存活边界

- 新版插件真实 Chromium 扩展环境（`sidepanel.html` 测试页及其真实 iframe，不是原生 dock 的视觉验收），Terra 对话 `5df09fa4-435e-49d6-85f2-3b40f0eefcc6`：第二个面板自动发现回合，两个面板的助手正文与刷新后的正文一致。
- 新发现的设计边界：执行进程崩溃时可能没有终态通知，原来的 30 秒全量核对还承担了孤儿回合收尾。保留仅运行期间每 60 秒的执行租约核对，调用已有 active-runs 检查并由服务端产生持久化 worker_lost 终态；不轮询历史/文本，空闲无定时请求。
- 租约前端 3 项测试通过：空闲无请求、运行期间检查、结束后停止，以及卸载清理。此最后补充尚在构建部署，真实故障注入验收未完成。
- 执行租约补充的完整前端构建通过，已发布线上静态资源（先资源文件、最后 index，保留插件下载包），首页 HTTP 200；没有为这一前端补充再次重启 API。故障注入仍待补验。

### F4 结果表格线程化与有界缓存（正在部署）

- 将加载、解码、搜索、筛选、排序及分页复制统一移出 API 事件循环。每个请求仍先鉴权；缓存键包含租户、用户、任务、URI 和当前存储版本。
- ObjectStore 新增 revision 接口：POSIX 加密文件使用文件身份/尺寸/纳秒时间戳（与读取相同的活动文件来源），S3 使用 HEAD 版本元数据，测试内存存储使用内容摘要。加载前后版本改变返回 409，不缓存不一致快照；删除返回 404。
- 缓存采用 LRU，最多 8 份、16 MiB Python 对象大小预算（不是整个进程 RSS 上限）。大表仍遵守原 32 MiB 文件限制；超过缓存预算的表可查询但不驻留。缓存填充串行，避免并发重复解码；返回页面深复制，排序/调用方不能修改缓存原表。
- 表格/评估首轮 21 项通过；补充并发、权限、过大/损坏文件、文件存储/挂载组合 47 项通过。
- 本地合成 10,000 行：首次查询含建索引 334ms，后续约 15ms，索引约 12.8MB。此为局部对照，不作为生产延迟结论。
- 已保存线上两个已完成任务（4 行及 300 行）的 12 组查询基线：分页、倒序、状态筛选、全局搜索和列筛选。服务重启部署中，待核对部署后返回值完全一致。

### F4 线上结果核对

- 服务 active，前端构建通过。部署后对两个既有任务执行同样的 12 组查询，与部署前保存的完整响应逐项相等，包含 rows、类型、统计、分页、版本和 partial 标记。
- 300 条任务的成功筛选返回 256 条，错误筛选返回 44 条，未因缓存或分页丢失样本。最后的表格专项 9 项通过，包含 S3 HEAD 版本更新/缺失处理。
- 本项不代表整份清单完成：F5、P4–P7、P10 及前面明确待补的实机边界仍需推进。
- 真实 300 行任务以 4 并发发起 8 次查询，全部返回正确的 300 总数/256 筛选数，总耗时 1.12 秒；同时 5 次 healthz 均为 200，耗时 219/12/8/8/7ms。此为轻量并发验证，不代表高负载或大文件容量验收。


### P4 最新范围调整：CLI 异步交接，不引入 SSE

- 普通 `workflow run` / `run-batch` 保持同步结果语义。只有实际执行遇到 Human 节点时，前台命令返回 `waiting_approval`、`async=true`、`run_id`、审批链接和查询命令。不能因图中含 Human 节点而提前返回，也不能将普通超时转成异步。
- 同一沙盒内的本地 worker 保留原执行、版本、输入和批量队列；不重跑已完成节点，不创建一份新的 Task。后台等待审批，前台 CLI 和 Agent 无须保持连接。后台审批传输暂用现有实现，不在本次新增 SSE；先前未部署的 CLI 审批 SSE 已撤回。
- `workflow status --run-id ID` 查询该沙盒的状态、计数和待审批列表；`workflow result --run-id ID` 分页读取已完成样本，支持 `--index` 定位样本。查询无执行副作用，查询成功不代表推理成功。
- 状态/事件文件统一按 run_id 存于 `/data/runs/<run_id>/`，业务结果仍允许 --output 指定位置。批次 ID 与样本 execution_id 区分；每个待审批项包括 index、execution_id、approval_id 和审批链接。
- 当前已实现本地进程交接和查询入口，正在回归，尚未部署或完成实机验收。必须补验普通同步、实际路由跳过 Human、审批通过/驳回/超时/取消、批量多个审批、CLI 退出后继续执行、按 ID 查询完整结果、进程异常退出及沙盒生命周期。

- CLI 最新回归 22 项通过（31.08 秒）：真实引擎审批通过/驳回/超时/取消、审批 API 集成、按 ID 查询/结果分页/样本筛选、拒绝路径式 ID；独立进程交接测试确认前台返回后原子进程继续并完成。该进程交接测试使用受控 worker，不能代替真实沙盒端到端验收。测试库删除后 DBOS 监听线程有 teardown 警告，非线上错误。
- 待补的明确缺口：worker 异常消失时不可继续仅展示最后落盘的 running/waiting_approval；需结合运行资源证据标识中断或状态未知，不伪造成功。完整普通执行/混合批量审批/断开前台后的真实沙盒验收仍未完成。

### CLI 查询输出契约补充（实现中）

- status/result 共用 execution_status、terminal、workflow_id、固定 version、mode、run_id、时间、progress、approvals 和 next_action。unknown 的 terminal 为 null，不能解释为仍在运行或已经完成。
- progress 的互斥数量为 queued/running/waiting_approval/completed/unprocessed，合计 total；completed 再按 succeeded/failed/timed_out/cancelled 划分。等待审批按样本计数，不按审批节点数计数。
- approvals 包含样本 index、execution_id、approval_id、node_id、instruction、deadline 和 execution_url。result 每行包含 execution_id，results_complete 区分全体样本是否有结果；pagination.next_offset 区分是否还有结果页。
- next_action 区分 check_status_later / request_human_approval / read_results / inspect_errors / verify_runtime，并提供明确说明和查询命令。查询成功不代表执行成功。
- 运行资源识别补充 23 项回归通过，包含真实 SIGKILL 后锁释放、缺失资源返回 unknown、终态提交与查询竞争。最新输出契约回归进行中，仍未部署。

- 输出契约回归 24 项通过（25.48 秒），随后补充数量守恒断言 1 项通过。CLI 输出协议 15 项通过（14.28 秒），包含实际启动独立 Python worker、真实引擎同步执行、按 run_id 读取业务结果；仅资源准备响应由测试 socket 提供，不作为线上鉴权/审批验收。

- 2026-10-07 09:58 UTC 已发布 CLI 相关 7 份源码并重启服务；部署前 test 账号 active-runs 为 0。准备以 Terra 新建独立 QA 对话进行真实审批验收。CLI 审批 SSE 源码未发布，后台审批传输仍为原实现。

- 09:58:52 UTC 服务启动完成，healthz=200。已在 test 个人空间建立 QA CLI async approval Terra 20261007：Chat `3add8cac-1cbd-4e46-81ed-d2ce9eee9bb8`，Project `prj_d584fb690612466da7ff4b0b97eefd35`。模型选择确认为 GPT-5.6-Terra，Agent 正在自行读取节点/命令说明并构建同步/审批分支。验收未结束，暂不清理该沙盒。

### P4 真实 Terra 单条 CLI 验收

- Terra 自主创建 Workflow `56dd608084db` / `v1.sv0`，包含 Start、Condition、Human 和两个分支 End，无模型节点。
- 跳过审批的 run `1272e0ba429e496e9361329cac1b2e04` 同步 completed，async=false；Terra 使用 result 查询同一运行。
- 审批 run `daafda89a4094f329c62fbd4ff4061be` 返回 waiting_approval、async=true、run_id、完整 approvals 和查询命令，Agent 回合正常结束。审批样本 execution_id 为 `a8923c8a-20c5-59bf-b484-d8dda9419327`。
- 浏览器实际打开 execution 链接，在只读画布看到说明、倒计时、Approve/Reject。点击 Approve 后，同一执行状态 succeeded，最终 End 输出 approved=true，耗时 62.75 秒；证明前台 CLI/Agent 回合结束后后台仍可接受审批并继续。
- 已在同一 Terra 对话发起续轮：查询原 run_id 后运行 3 条批量样本（1 跳过、2 审批）。批量验收进行中。
- 本地真实引擎三样本同时审批测试通过：通过、驳回、超时分别保留结果；状态计数守恒，timeout 无业务输出。此单测不替代线上批量验收。

- 真实混合批量 run `832f64c456684302bda75686e84acacd`：共 3 样本，1 成功、2 待审批；Agent 正确展示两条独立审批入口。浏览器分别点击样本 execution `67e18913-d3fe-58af-90b0-cd00646fabac` 的 Approve 和 `75741e70-4761-5d12-8088-006fd70a0286` 的 Reject。两条数据库状态均 succeeded，End 分别输出 approved=true/false。
- 查询整批结果的下一轮已发起，脚本显式重新选择 Terra。上一续轮发送前 UI 显示 No model configured，虽然沿用原对话运行成功，但未取得其服务端实际模型证据，不能将该续轮单独当作已确认 Terra 的模型验收。

- 最后一轮显式确认 GPT-5.6-Terra，成功仅通过 status/result 查询原批次。核对实际工具 JSON：execution_status=completed；total=completed=succeeded=3；failed/timed_out/cancelled/waiting_approval=0；results_complete=true；pagination.next_offset=null。三行 index 为 0/1/2 且 execution_id 各不相同，输出分别为跳过分支结果、approved=true、approved=false，errors 均为空。
- 已实证：正常同步、单条审批自动异步交接、Agent 回合结束后继续执行、跨回合按 run_id 查询、批量多审批、通过与驳回、正常驳回不算失败、完整结果/计数。线上 timeout/cancel/进程丢失与其余审查项仍未完成；不能标记整个目标完成。

- 真实 Terra 超时验收：仅本地副本将 Human timeout 改为 2 秒，保存版本 v1.sv0 未变。run `314e3d9bfce744599c9e0e416d4a9210` 最终批次状态 completed_with_errors，计数 timed_out=1，其样本 execution `b72f67ec-a8f0-5969-9e7d-ce8bbfccdea3` 返回 timed_out/approval_timeout/output=null，无 approved=false。
- 已通过平台取消另一条执行 `d2853bd7-2103-598e-bd3a-f25664d02543`（run `196bfb2255ba45ff83febb9802b18f28`），正让 Terra 核对本地结果，并继续针对独立新测试 worker 做异常退出验收。
- 本地查询缺失/损坏结果的错误契约补充通过 7 项测试：command_status=failed、execution_evidence_unavailable，不伪造 execution_status=failed。该最后补充代码尚未发布。

### P4 运行资源丢失与结果持久化实机核对

- 取消 run `196bfb2255ba45ff83febb9802b18f28` 的实际 CLI 结果：样本 cancelled，output=null，error_code=null；区别于超时和正常驳回。
- 故障测试 run `9bac1b143b9b49f4a88d718b832fd325` 返回的 worker PID 在 Agent /proc 中不可见；Agent 未执行无法核实目标的 kill，故此项不记为线上 SIGKILL 验收通过（本地进程 SIGKILL 单测已通过）。
- 确认该 QA 对话无活跃回合后，调用专属 Project 的 sandbox DELETE，平台返回 runtime_process stopped、mount detached。仅回收运行资源，保留持久化文件。
- 在恢复的同一 QA 对话显式选择 Terra，仅查询原 ID：未完成 run 返回 unknown、terminal=null、execution_runtime_unavailable，last_known_status=waiting_approval，不误报当前仍在等待；此前 completed 批次仍能读全 3 条结果。

### P10 评估页面通知刷新（未部署）

- 新增 Task activity 快照失效通知入口，复用 flowork_task_state / invalidations；任务推理终态不关闭此订阅，后续评估仍可更新。连接前鉴权、释放请求事务，连接期间复核权限，初连/重连触发快照读取。
- 评估排队和结束复用既有日志提交通知；新增 running 和配置保存的事务内通知，回滚不发送。通知只带资源 ID，不携带评估指标或脚本。
- EvaluationTab 移除 3 秒轮询，复用通用 resource-activity 客户端；100ms 合并高频通知，读取期间再次变更会补读，窗口恢复/重连重新读取。
- 前端 2 项测试通过：空闲零读取、100 次通知合并、读取期间变更不丢、独立窗口连接、权限撤销停止、卸载清理。后端评估和新路由组合回归进行中；还需完整构建及真实页面验收。

- P10 后端评估/路由回归 18 项通过（16.72 秒），完整前端构建与部署路径审计通过。10:14 UTC 开始发布 6 份源码并重启，包括最后的 CLI 查询读取错误处理。真实双窗口评估验收脚本已准备，服务启动后运行；不将单测结果视为完成线上验收。

### P10 线上双窗口验收通过

- 新服务 active，线上前端构建和部署路径审计通过。对既有 4 行 QA 批量任务 `85ee0ecf-1d8e-4444-b051-17c2cb72fc9e` 打开两个独立浏览器上下文的 Evaluation 页。
- 首次稳定后观察 9 秒：两个窗口 evaluation GET 计数保持 [2,2]；跨连接保存配置后，两者均立即补查至 [3,3]。
- 第二窗口离线，第一窗口见到真实评估成功指标 QA_P10_rows=4；第二窗口恢复联网后补读并展示相同指标。数据库评估 succeeded、row_count=4、error=null。
- 评估结束后再次观察 9 秒：GET 计数稳定在 [6,4]，无周期查询；两个页面 JS 错误 0。测试 finally 恢复原评估配置。证据 `/tmp/flowork-eval-activity-live-result.json` 和截图 `/tmp/flowork-eval-activity-live.png`。
- 当前工作区 Gitleaks 扫描通过（27.35MB，6.34 秒），无泄露发现。尚未提交；F5、P5–P7 和前述其他边界验证仍属于完整目标，未宣称全项目审查完成。

### P7 任务详情状态刷新（未部署）

- 新增 migration 171：tasks、task_schedules、scheduled_run_executions 的业务状态/内容改变，在事务提交后通知 flowork_task_activity；纯 worker 心跳不触发。定时执行通过 schedule_id 定位所属 task_id。
- 将 Task activity 与高频 task log 通道分开。Evaluation 的配置/排队/running/终态都由相同数据库通知覆盖，移除 P10 初版的手动 notify_resource 调用，避免遗漏其他写入入口。
- 详情页概览、定时计划、执行列表和评估 Tab 共用一条订阅，状态通知按 1 秒合并后只刷新相关查询，不重读整个日志历史。移除活跃任务/定时执行的 5 秒轮询。
- 授权变更尚无对应事件，任务主详情仍保留 15 秒权限复核，不能宣称这里已经完全没有轮询。Workflow 执行详情与历史轮询也仍待处理。
- migration/任务/定时/评估组合 40 项通过（41.20 秒），含真实 PG 的提交唤醒、回滚不通知、心跳不通知。前端任务详情/活动客户端初轮 18 项通过（21.43 秒）；其后已合并父页面与评估 Tab 的重复订阅，需再做构建及实机验收。

### P4 CLI status/result 输出契约细化

两个命令只读取当前工作区中原 run_id 的证据，不提交、不等待执行，也不订阅消息。单条和批量使用相同契约。

| execution_status | 含义 | terminal | Agent 下一步 |
| --- | --- | --- | --- |
| preparing | 正在准备快照与资源 | false | 稍后查询同一 ID |
| running | 正在执行，可能还有排队样本 | false | 稍后查询同一 ID |
| waiting_approval | 至少一个样本等待审批，其他样本可以继续运行 | false | 展示 approvals 的链接与说明，再查询原 ID |
| completed | 全部样本执行成功 | true | 分页读取并分析结果 |
| completed_with_errors | 全部样本已产生终态结果，其中有失败、超时或取消 | true | 查看失败样本、节点错误和模型调用证据 |
| failed | 执行准备或整批运行异常，可能只留下部分结果 | true | 查看执行错误和已有结果，不自动重跑 |
| cancelled | 整次执行被取消，可能有未完成样本 | true | 交付已有证据，不自动重跑 |
| interrupted | 确认执行进程已退出，但没有写入终态 | true | 保留已有结果，说明执行中断 |
| unknown | 当前沙盒缺少足以判断运行状态的证据 | null | 核实原工作区与运行资源，不冒充失败或仍在运行 |

`command_status` 只表示查询是否成功；查询成功返回退出码 0，即使 execution_status 是 failed。ID 不合法、记录不存在、文件损坏分别是查询错误，不得伪造成业务执行失败。`unknown` 是成功读取了历史记录但缺少当前运行证据。

`status` 提供 run_id、workflow_id、准备成功后的 version、mode、async、时间、progress、approvals、错误和下一步指令。async 表示曾发生异步交接，不是当前是否完成。progress 区分 queued/running/waiting_approval/completed/unprocessed；completed 分为 succeeded/failed/timed_out/cancelled。历史顶层 failed 是所有非成功样本合计，细分统计以 progress 为准。运行资源丢失时，原 progress/updated_at 仅是最后观测值，结合 last_known_status 解读。

每个待审批项提供 index（从 0 开始的输入行号）、execution_id、approval_id、node_id、instruction、deadline、execution_url。同一样本多个并行审批分别列出，但 waiting_approval 计数按样本去重。普通未审批执行不承诺有平台 Trace 页面。审批驳回是 approved=false 的正常业务输出；审批超时则该样本 timed_out、approval_timeout、无业务输出。单条命令也使用批次外层状态，因此单条超时为 completed_with_errors，具体原因看唯一结果行。

`result` 在相同上下文上增加 results，每行含 index、execution_id、input、status、output、error_code、errors、node_outputs、model_calls、execution_time。行状态 success/error/timed_out/cancelled；按完成顺序返回，不保证输入顺序。空字典业务结果和无结果 null 不等价。

分页参数为 --index / --offset / --limit。pagination.next_command 给出保留筛选和页大小的完整下一页命令。next_offset=null 只表示当前没有下一页；results_complete=true 表示全部样本有终态记录，不表示全部成功，也不表示当前页已包含全部记录。result_available 只描述当前页是否有行。partial=true 也可能是已取消/中断而永久不完整，不能据此无限等待。

next_action 包含 type/message/command：进行中 check_status_later；审批 request_human_approval；未知 verify_runtime；status 终态 read_results/inspect_errors；result 终态有下一页 read_next_page，读完 analyze_results（command=null），避免 Agent 重复查询同一页。任何错误均不暗示可以自动重放有副作用的执行。

本次补充包含分页下一步命令、终态最后一页分析提示、run_not_found 的原工作区提示；修正了 Agent 指令中保留前台等待审批的旧描述。该轮补充尚未部署，测试结果另记。

- 本轮查询契约回归 8 项通过（11.68 秒）：审批交接不结束 worker、状态/结果查询不调用宿主机、损坏记录作为查询错误、运行证据丢失、SIGKILL 释放标记、分页下一步、终态部分结果和空筛选页。git diff --check 通过。此次输出细化仍未部署，不将其报告为线上验收完成。

### P7 执行详情通知与弱网补读（验收中）

- migration 172 为执行事件、审批状态、执行终态增加提交后失效通知，使用独立 flowork_execution_activity 通道，不把每个节点事件广播给 Deployment 内部等待器。
- 执行详情订阅沿用原权限：被指派审批人可以查看自己的审批执行；共享 Workflow 本身不授予其他人的执行历史。连接持续复核当前登录/空间/成员和资源权限。
- 去掉执行详情与节点事件的 1 秒轮询。补读按序分页，校验缺口、不前进页，保留已有缓存；以首个快照的 head 为本轮补读目标，避免持续产生事件导致页面迟迟不显示。
- 通用 activity 客户端补上“通知成功但快照请求失败”的退避重试；成功后恢复空闲零查询。任务与执行详情显式传递读取失败，避免误认为同步完成。
- 前端 7 项回归通过（6.47 秒），覆盖分页 1003 条、持续生产、序号缺口、拒绝访问/中止、突发合并、多窗口、读取中变更及请求失败无新通知时补读。后端与完整构建进行中。

- 后端 10 项通过（19.91 秒），含真实 PostgreSQL 提交/回滚与审批/终态唤醒、审批人例外、他人私有执行拒绝、权限失效与资源不存在。首次测试的事件类型误用了 node_started，已按真实协议 node_event 修正后重跑通过；数据库清理后的 DBOS 测试线程仍有 teardown 日志，未将其作为生产错误。
- 完整前端构建与部署路径检查通过，13 份配套源码和 migration 171/172 已发布；线上 flowork.service active，开始双窗口弱网验收。CLI 输出契约本轮补充也已随此次发布。

### P7 首轮线上证据与追加修正

- 171 部署后评估双窗口复测通过：空闲请求 [2,2] 不变，配置保存双方刷新 [3,3]，离线窗口恢复后见到 QA_P171_rows 指标；终态请求稳定 [5,4]，页面错误 0，恢复了原评估脚本。
- 新建两次独立 QA 画布审批执行 c8f4fed2-f446-44a5-8a99-12c25b670484 / 753caba1-3fe0-4309-9502-7ab929d4a40c，均经真实按钮审批成功、离线窗口恢复成功、空闲无周期查询。但故意中断一次节点记录 GET 时产生 CancelledError，故本组不能记为验收通过。
- 堆栈指向 QueryClient.refetchQueries 取消并发查询；增加 refreshResourceQuery，先等待已有读取，再做不取消的最新补读；审批后的详情刷新也统一使用它。新增真实 QueryObserver 并发测试，8 项前端回归通过（6.44 秒），正在重新构建发布和实机复测。

- 并发刷新修正完整构建、发布后，第三次真实审批执行 b2b3184e-d211-466f-878d-aaad6ee4e7e7 通过：两个窗口待审批时查询稳定 [4,4]；窗口 B 离线，窗口 A 点击通过并故意丢弃一次节点事件 GET；A 自动补读成功，B 恢复联网后显示 succeeded；12 条事件与数据库 last_seq 一致，终态查询稳定 [9,6]，页面错误 0。证据 `/tmp/flowork-execution-activity-live-result.json`、截图 `/tmp/flowork-execution-activity-live.png`。
- 本次前端热发布先复制资源文件，再原子替换 index.html，避免中途页面引用尚未就绪的新文件；API 无需再次重启。原源码也同步到 /opt/flowork，后续标准启动仍会按源码构建。

- 最终任务详情 + 执行事件补读 + 通用通知客户端组合回归 24 项通过（17.86 秒）。已人工检查离线窗口恢复后的截图：Start、Condition、Human、End 状态正确，Human 与 End 输出 approved=true，未走分支保持未执行；不是仅依赖页头的成功文字。
- 本轮只关闭专属 QA Workflow 的沙盒，未删除 Workflow、历史或结果。完整审查目标仍有 F5、P5、P6、P7 的历史列表刷新及其他已列边界未完成，保持继续推进。

- 使用最终前端版本再次复测评估页：QA_P172_rows 指标成功，两窗口配置变化、执行完成、断网恢复全部正常；请求计数空闲 [2,2]、终态 [5,4] 均保持稳定，页面错误 0。脚本已恢复原配置，真实服务仍 active。

### P6 Workflow 版本通知（开发验收中）

- migration 173 在 workflows / workflow_versions 写入事务提交后发送 flowork_workflow_activity。涵盖新版本、版本指针及元信息变化；回滚不发送，通知不携带工作流内容。
- 新增 Workflow activity 订阅入口，连接前验证 VIEW，连接中复核登录、空间和权限。前端复用资源通知与补读组件，只刷新当前 major 的 head 和版本树，不使固定历史图缓存失效。
- 移除 3 秒版本轮询；分享权限目前无对应变更事件，保留每 15 秒权限核对（同 Task），不声称零轮询。去掉 head 每次变化时重复触发版本树读取的旧 effect，避免与事件刷新互相取消。
- 后端 2 项通过（9.52 秒），前端 6 项通过（7.22 秒）：提交唤醒、回滚无通知、入口鉴权、固定图不失效、通知刷新 head/tree、禁用不订阅、6 秒无旧高频查询。完整构建和实机进行中。
- 执行历史列表的普通视图与 mine/审批视图需要按可见范围推送，尚未改造；VFS 文件树及跨进程文件变更也仍属于 P6 未完成部分。

- P6 版本通知完整构建、部署路径检查通过，migration 173 和配套 API/前端已部署。原有 head/固定版本加载回归另 5 项通过（18.84 秒）。线上首轮脚本遗漏展开默认折叠的版本分组，未作为产品缺陷；补上展开步骤重新验证跟随/固定/断线/跨大版本场景。

### P6 版本通知线上验收通过

- 专属 QA Workflow b99a347a6a4c：两独立浏览器上下文分别打开跟随 v1 与固定 v1.sv1（snapshot=1），展开版本树。外部 API 提交 v1.sv2 后，跟随画布节点名立即更新为 QA_updated；固定画布仍为 QA_initial，地址仍 v1.sv1，双方版本树收到更新。
- 将第二窗口切为跟随 v1 后断网，外部提交 v1.sv3。在线窗口展示 QA_recovered；离线窗口恢复后自动补读并展示相同节点名。
- 外部新增 v2：两个已打开画布仍处于 /version/v1、内容仍 QA_recovered；版本树均出现 v2，没有跟随全局 HEAD 跳错大版本。
- 页面错误 0。请求计数变化为 head [6,7] / versions [5,6]，包含首次读取、通知补读、页面导航和保留的低频权限复核；不能将这些总数误称为空闲轮询次数。6 秒空闲窗口排除了旧每 3 秒查询。证据 `/tmp/flowork-workflow-activity-live-result.json` 与截图 `/tmp/flowork-workflow-activity-live.png`。
- 人工检查最终截图确认版本树含 v2、v1.sv3/sv2/sv1/sv0，v1 标记 current，画布节点为 QA_recovered。测试只保存版本，没有启动推理沙盒或部署实例。
- 本条仅完成 P6 的版本/版本树部分，文件树仍未改造；P7 执行历史列表、P5 通用作业通道及 F5 回收边界仍待实施。整份清单未标记完成。

## 补齐验收安排（用户追加要求）

用户明确要求补齐“部分场景待补”。下一阶段优先验证已部署改动，不能把局部测试通过改写为整项完成；发现缺陷先修复并重测，再推进剩余事件化。

验收收口要求：每个待补场景记录操作步骤、预期、实际结果和证据；缺少实机证据的项目不得用单元测试替代。失败用例须修复后重新执行。真实服务中未启用的存储后端等条件明确注明适用范围，不写成全场景通过。

| 链路 | 当前缺少的主要证据 | 补验要求 |
| --- | --- | --- |
| F1 文件下载 | 实际浏览器文件名/字节、跨账号拒绝 | 英文/中文/空格/引号/百分号/emoji；匿名及 test2 拒绝；不存在/控制字符；任务结果入口 |
| F2 插件引用 | 真扩展并发跨窗口引用后 Terra 读取、标签页变动隔离 | 附件与授权都齐全；多个对话不串用；关闭/移动/替换标签页不误读 |
| F3 / P1 长回放 | 长回合跨多批回放的实机耗时/内存对照 | 不截断；无丢失重复乱序；终态、重连、旧游标、完整正文一致 |
| P2 Task 日志 | 活跃任务页面断线/多窗口和终态竞争 | 从已接收游标补读；取消与完成竞争不漏终态；权限撤销停止读取 |
| P3 取消 / 租约 | 真实多执行取消隔离及 worker 丢失 | 只取消目标执行；其他执行继续；服务或 worker 失联产生真实终态而非永久运行 |
| P8 插件下载 | 原生侧边栏通知、重开补查及确认过期 | 无旧每秒查询；不漏待确认项；过期及取消正确 |
| P9 Preview/后台任务 | 真实文件预览及后台任务事件的断线恢复 | 新事件及时更新、历史完整、跨用户隔离 |
| P6/P7 已上线部分 | 共享权限撤销、浏览器后台/重新打开等组合 | 现有双窗口、离线、单次请求失败证据保留；补齐权限与生命周期组合 |

共享通知组件还需对照已有测试证据核实数据库监听断开重连、重复通知、事务回滚、订阅资源切换和监听连接回收。所有记录注明自动化或真实服务，避免混用结论。

### F1 下载真实服务补验

- 使用 test 个人 Project 的专属 QA 子目录，经 Storage API 写入 5 个文件，真实 Chromium 点击下载：英文、中文、空格、引号/百分号、emoji 均成功；逐一核对 HTTP 原始字节、浏览器落盘字节和 SHA-256 一致。
- Content-Disposition 的 UTF-8 filename* 解码与原文件名一致，无响应头编码异常。Chromium 将双引号替换为下划线（引号_百分号%.txt），这是浏览器文件名规范化，不是内容或服务器响应头损坏；中文、空格、百分号、emoji 保留。
- 每个文件用新登录 test2 账号读取均 404，匿名均 401；控制字符文件路径写入被拒绝 400，不存在文件 404。
- 既有 QA Task results.csv 从同一 raw 入口真实返回 200、115 字节和正确下载头。
- finally 删除专属 QA 目录，清理返回 200。证据 `/tmp/flowork-download-live-result.json`。该证据覆盖当前生产 POSIX 与 Task artifact 分支，不把未启用的旧对象存储分支伪称为已生产验证。

### P6 共享权限撤销真实服务补验

- 专属 Workflow `0d9562ada359` 分享给 test2（viewer），创建者与 test2 分别使用独立浏览器上下文。撤销前，创建者提交新子版本，两窗口均收到更新。
- 撤销分享后，test2 画布停止展示，head 查询返回 404，activity 请求记录从 200 变为 404，并在后续 5 秒观察期内停止重连。
- 创建者继续提交版本仍正常更新，test2 未收到新的画布内容；两窗口脚本错误为 0，测试结束无残留分享。
- 证据 `/tmp/flowork-workflow-revoke-live-result.json`。该文件的 revocation_ms 字段实际包含后续创建者更新和观察期，不能作为权限撤销耗时或 SLA；本次只认定功能行为通过。

### F3 长回放分页边界自动回归

- 新增 1003 条事件（含中文、换行、emoji 和终态）的回放用例；从游标 0、255、256、257、1003 恢复，按完整 SSE 字节逐条比较，验证无丢失、重复、乱序或正文变化。完成状态不能导致只读第一页就退出。
- 验证每批最多 256 条、每批读取释放 session 后才向客户端交付，以及结束后取消订阅。该测试使用受控 repository，不是线上数据库耗时或内存基准。
- 配套 11 项自动测试通过（13.16 秒），证据 `/tmp/flowork-replay-corners.log`。首次运行遗漏隔离测试数据库环境，测试初始化失败；补齐已有测试 PostgreSQL 配置后重新运行全部通过，未修改生产数据库。
- 实际长历史的多页 SSE 回放与内存/首包耗时对照仍待补，不因此关闭 F3 全部验收。

### F3 长历史真实服务 SSE 补验

- 复用 test 账号已完成回合 `t_af3dc385d07046799efd6f354987b17f`，数据库 last_event_id=4269；使用该账号登录 Cookie 请求真实 API resume，未创建模型调用或改写历史。
- 从 0 读取得到连续 1–4269；分别从 255、256、257、4000、4269 恢复，逐条序号及完整 SSE 帧 SHA-256 与首次读取对应后缀完全一致。所有响应 X-Replay-Source=database，确认不是进程内缓冲回放。
- 另一次读取在第 123 条主动关闭响应，再携带 Last-Event-ID=123 重连，两段合并与完整回放完全一致，无重复或丢失。
- 最终轮：从头首条 209ms、总计 1325ms；非空断点响应首条 152–168ms；终态游标返回 0 条并在 108ms 关闭。此前一轮完整读取 1533ms。以上为同宿主机 HTTP 实测，不是公网浏览器弱网性能保证，也不是新旧实现性能对照。
- 证据 `/tmp/flowork-long-replay-live-result.json` 与 `/tmp/flowork-long-replay-live.log`。本次完成真实 API 多页与主动断线补读证据；长历史浏览器渲染和内存对照仍未完成。

### F3 同数据内存对照

- 同一生产历史 4269 条事件，以两个独立 Python 进程只读查询，比较当前 256 条分页与重建的旧整批 select/materialize 读取方式；没有回退线上代码。两者包含相同解密及 SSE 编码步骤，最终 SHA-256 完全一致。
- 分页 17 批：Python tracemalloc 峰值 8,248,695 bytes，进程峰值 RSS 90,936 KiB（开始 72,260 KiB）；整批：20,085,100 bytes、116,060 KiB（开始 72,692 KiB）。本样本 Python 分配峰值下降约 59%，不能外推所有负载或整个 API 服务的内存降幅。
- 启用 tracemalloc 时，分页首批 939ms、总计 4002ms；整批首批 3230ms、总计 3709ms。分页较早交付，查询次数增加使总时间略长。该计时包含探针开销，不与前述真实 HTTP 首包计时直接比较；这是一次受控对照，不是统计压测。
- 证据 `/tmp/flowork-replay-memory-result.json`，脚本 `/tmp/flowork-replay-memory.py`。只输出聚合计数及哈希，不落盘原始对话正文或凭据。

### F3/P1 历史对话浏览器补验

- 真实 Chromium 打开上述回合所属对话，初始尾部呈现 4 个 Markdown 块。模拟浏览器断网、恢复联网、整页刷新，逐块全文比较均与初始内容一致，页面异常为 0。
- 点击真实“Load earlier messages”，呈现块数从 4 增至 6，原有尾部全文保持一致。人工检查截图，历史正文与底部编辑框正常显示，没有空白覆盖或整页移出视口。
- 证据 `/tmp/flowork-long-chat-browser-result.json`、截图 `/tmp/flowork-long-chat-browser.png`。本次验证的是已完成对话的尾部加载、一次向前翻页及重连/刷新；不等价于把所有历史一次渲染，也未覆盖新消息正在生成时的长历史滚动。

### P3 独立执行取消隔离实机补验

- 创建两个专属 QA Workflow（`8d2faa5881a4`、`5a9e8ac3a058`），复用无模型审批图，同时保持两条执行待审批；未修改业务 Workflow。
- 取消执行 `80d44728-5544-41e6-bf05-55c46fdb6c50` 后，其详情及真实浏览器页变为 cancelled；另一执行 `a74bad51-c1ed-4a02-a529-a3f37c5b9da6` 仍为 waiting_approval。
- 在第二条执行画布点击通过，页面和 API 均变为 succeeded；再次核对第一条仍 cancelled。页面异常 0。结束后关闭两个专属沙盒，保留测试记录。
- 证据 `/tmp/flowork-cancel-isolation-live-result.json`。本例覆盖不同 Workflow 的并存执行及跨执行取消隔离，不等价于同一常驻 worker 内的并发隔离；后者及完成/取消竞争仍需单独证明。

### P3 沙盒移除后的执行收尾实机补验

- 专属 QA Workflow `8d2faa5881a4` 再次启动执行 `52314ef7-d050-4abe-a889-a52092472a4d`，确认 waiting_approval 后调用该 Workflow 的沙盒删除接口。
- 执行在观察到的 609ms 内变为 failed，error_code=execution_lost，没有永久停在等待或伪装为成功。最终再次清理专属沙盒，未删除运行历史。
- 证据 `/tmp/flowork-runtime-loss-live-result.json`。该场景是平台主动移除沙盒，覆盖正常管理路径的资源丢失收尾；不能代替 worker SIGKILL、网络分区或租约自然过期的故障注入验收。

### P3 独立常驻进程与真实 RPC 补验

- 新增 `engine/tests/test_runtime_process_rpc.py`，启动真实 `vibecanvas_engine.runtime.rpc` 子进程，通过受认证 Unix socket 操作，未 mock 推理引擎或 RPC。
- 同一 capacity=2 worker 同时执行两条审批 Workflow，取消第一条，第二条仍可通过审批并 succeeded；第一条继续保持 cancelled。逐条读取终态事件并 ACK 后，以相同 ID 重复 invoke 返回原终态和 acknowledged=true，没有重跑。
- 第三条等待审批时 SIGKILL 该测试进程，再从相同 socket 路径启动新进程；generation 改变，携旧 generation 的 invoke 明确返回 execution_lost，新 generation 查询旧 ID 返回 not_found，没有伪恢复。
- 新用例通过（1.01 秒），证据 `/tmp/flowork-runtime-process-test.log`。测试 finally 清理进程，未触碰线上 worker。此证据覆盖真实进程及协议层故障；线上宿主机在租约过期后写入终态仍需单独验证。
- F5 的完成记录容量上限仍未实现，ACK 防重通过不能视为内存有界已完成。
- 同进程审批行为、事件缓冲背压及新增真实 RPC 用例组合回归 30 项通过（8.98 秒），证据 `/tmp/flowork-runtime-process-regression.log`；git diff --check 通过。

### 共享监听连接断开与回收补验

- 新增 `api/tests/test_state_listener_reconnect.py`，使用独立测试 PostgreSQL 的真实 asyncpg 连接，不断开生产数据库监听。
- 同一通道的 3 个订阅（两个订阅资源 A，一个订阅资源 B）只建立 1 条监听连接。真实 pg_notify(A) 同时唤醒两个 A 订阅，不唤醒 B。
- 主动 terminate 测试连接，在重连尚未放行时确认没有虚假通知；允许重连后，三个订阅均被唤醒，供各自重新读取权威状态。旧连接已关闭，新连接有效。
- 退出两个订阅时仍保留连接供最后一个订阅使用；最后一个退出后连接关闭、全局 listener 注册表移除该通道，未残留监听任务。
- 用例通过（6.84 秒），证据 `/tmp/flowork-listener-reconnect.log`。这证明断线后的失效广播与连接回收，不单独证明所有上层页面的恢复，页面证据仍分别记录。

### F5 回收实现约束复核

- 当前 host slot 固定持有启动时的 RPC client/generation，完成 ACK 后引擎 tombstone 承担同 generation 防重。只对 completed 字典做 LRU/TTL 删除会允许旧 ID 重跑，不能采用。
- 安全轮换必须同时覆盖新请求接入、未结束 slot、未 ACK 结果、旧 client 的 generation fencing 与进程回收；不能仅在某次 ACK 时重启共享 worker。该项仍待实现，现有协议测试作为后续回归约束。

### F5 已撤回的 worker 轮换实验（未部署，以下仅为历史记录）

- `WorkflowRpcPool` 增加每代累计 reservation 上限，默认 10000，包含启动失败及投递结果不确定的预约，保守限制可接收的执行数量。达到上限后不向该代接入新预约；只有全部 owner 释放后才关闭旧 worker，新调用启动新代。
- 有其他可用 worker 时继续按最少执行数选择；全部处于轮换等待时使用 asyncio.Event 等待 owner 释放，不增加轮询。显式配置的并发已满仍按原约定拒绝。关闭 pool 会唤醒等待者，取消等待本身不留下 owner。
- 4 项控制逻辑测试通过（7.89 秒）。首次真实 bubblewrap 测试以 root 启动，因用户命名空间无法访问 flowork 私有 Python 安装路径而失败；未修改该目录权限。将相关源码及测试复制到临时验证目录，改以实际服务用户 flowork 运行，真实沙盒与控制逻辑组合 8 项通过（6.96 秒）。
- 新增真实沙盒用例将上限临时设置为 2，6 次执行跨 3 代：各代内重复 ID 返回 acknowledged 终态，前两代进程均确认退出，同一 `/run` 中 6 个输出文件全部保留；旧 token/client 对新进程请求被拒绝。证据 `/tmp/flowork-rotation-unprivileged.log`。
- 尚未部署：等待轮换接入的请求如何及时响应宿主机取消、Deployment 同步超时，需要补齐调用链验证与处理；不能仅凭 pool 等待取消测试宣称平台取消已覆盖。完成记录上限条目仍保持进行中。

### 复杂度复查与方向纠偏（最新）

- F5 轮换代码及专属测试已撤回，未部署过；此前通过记录仅作为已放弃方案的实验历史，不代表当前实现。保留真实 RPC 取消/进程丢失测试，它们不依赖轮换方案。
- 数据库已有 workflow `claim_dispatch` 条件更新和 Deployment `runtime_claim IS NULL` 行锁领取；因此持久防重不是空白。仍需区分“再次派发”与同次派发的迟到 RPC，不能把有数据库记录等同于可直接删除 worker 防重状态。
- P1/P2/P9：原有 SSE 内部高频查库改为共享通知，有明确减少空闲事件查询的价值；保留数据库分页回放、重连补读，不推倒已经验证的正确性机制。
- P8：插件已有本地通知，删除重复的每秒查询属于精简；无需另建跨服务事件系统。
- P6/P7/P10 普通资源页面：当前新增 activity SSE 仍每 5 秒调用授权 guard，guard 会重新读取身份、资源及权限；Task/Workflow 前端另保留 15 秒查询。因此“业务查询减少”不能冒称“总数据库压力减少”。还增加了重连、合并刷新和快照失败重试。低频详情与历史列表优先评估可见时轮询、终态停止、聚焦/操作后刷新。
- Workflow 画布仍需满足 Agent 修改后及时刷新这一用户需求；可用轻量版本检查满足，不将事件通道本身当作需求。已有上线订阅暂不盲目删除，先对照实际交互和权限撤销行为，再确定简化范围。
- 文件树保持可见范围内刷新/低频轮询作为可选最终方案，尤其跨 POSIX/NFS 写入时，不为覆盖远端文件变动再引入一套监听服务。历史列表也不再以事件化作为必须完成项。
- P5 每 20ms 扫描 inbox 与上述低频页面不同，仍需测量真实 CPU/延迟收益再决定是否更换 IPC；不以代码中存在 sleep 作为缺陷证据。
- 后续验收记录须覆盖总查询成本、连接数量、故障恢复及维护代码量，不能只统计浏览器 GET 减少。
- 新增真实 PostgreSQL 并发领取测试：8 个独立事务竞争同一 Workflow 执行，仅 1 个成功，7 个拒绝；新 session 重试、绑定运行代后重试、终态后重试均拒绝。通过（7.59 秒），证据 `/tmp/flowork-dispatch-claim.log`。首次测试误用了未支持的测试错误码，改用实际 execution_failed 后完整重跑；没有更改业务失败码约束。

### F2 真扩展跨窗口原生引用补验

- 使用部署的 `/opt/flowork/extension/dist`、真实 Chromium 和 test 登录，在两个窗口打开三个本地测试网页，经浏览器原生右键菜单逐一引用到同一 QA 对话。没有直接写入附件或访问授权来冒充用户引用。
- 第一个窗口引用后显示 2 个附件；第二个窗口切到同一对话后引用第三页，显示 3 个附件。检查 session storage 有 3 条独立 tab/window/target 记录，对应两个窗口的三个来源；截图人工确认附件内容与来源名称完整。未发送新 Agent 回合。
- 证据 `/tmp/flowork-native-multi-current-result.json`、`/tmp/flowork-native-multi-current.log`、`/tmp/flowork-quote-multi-panel.png`。测试浏览器和本地 HTTP 服务均在 finally 关闭。
- 首轮复用旧浏览器 profile 时原生菜单缺失，且旧脚本 history 定位命中多个元素；干净 profile 下菜单存在并可操作，修正选择器后完成上述验证。旧 profile 原因未确定，不能声称修复了菜单问题。
- 本轮是真实跨窗口逐次引用，不是并发压力测试；并发写入已有自动回归，但真实并发、多 tab 变化后的 Terra 读取、跨对话/账号拒绝仍需继续补验。


### F2 Terra 跨窗口读取实机补验

- 使用真实侧边栏与 GPT-5.6-Terra，在 QA 对话 `5df09fa4-435e-49d6-85f2-3b40f0eefcc6` 中通过原生右键引用两个窗口的三个测试网页；输入只给出读取和核验目标，没有提供 Browser CLI 命令或参数。
- 三份引用快照只包含收入变化文字，不包含各页独有的校验文字。Terra 通过工具读取页面后正确返回 `/one → ALPHA-731`、`/two → BRAVO-482`、`/three → CHARLIE-956`，回合正常结束。截图显示三个附件及对应结果表格。
- 证据 `/tmp/flowork-terra-multi-current.log`、`/tmp/flowork-quote-agent-result.png`；浏览器及测试 HTTP 服务在 finally 关闭。使用的是已有专属 QA 对话，不声称创建了新对话。
- 本次证明多来源引用后 Agent 能读取快照之外的页面内容；不等同于真实并发引用、账号切换或关闭标签页后的完整 Agent 调用验收。


### F2 标签页生命周期实机补验

- 真实 Chromium 加载部署插件，经原生右键建立三个引用授权；将当前 `page-quotes.ts` 转译后在扩展 worker 中调用实际导出函数，使用真实 Chrome API 和 session storage，没有 mock 标签页。
- 同一对话最初恢复 3 个来源；把一个来源移到另一个窗口后只恢复 2 个；再关闭一个来源后只恢复 1 个；其它对话恢复 0 个；调用账号退出流程使用的 `clearQuotedTabs` 后恢复 0 个。未错误地替换成当前活跃页面或其它窗口的页面。
- 证据 `/tmp/flowork-quote-lifecycle-result.json`、`/tmp/flowork-quote-lifecycle.log`。浏览器及测试服务正常关闭，git diff --check 通过。
- 验证范围是实际引用模块与浏览器 API 的集成，不是标签页变化后的完整 Agent 初始化或真实账号退出链路。QA 对话已有草稿附件，因此 UI 总附件数不作为本轮授权数量证据；本轮 3 条原生授权以实际 session storage 和函数返回为准。未向 Agent 发送额外回合。


### F5 完成记录成本实测与职责复核

- 在当前源码的真实 `WorkflowRuntime` 中串行执行 2000 次 Start→End，无模型、无 mock。每次等待引擎 task 完成，核对 succeeded，再通过实际 acknowledge 清理；每个采样点 active/unacked executions 均为 0。
- tracemalloc 保留量：100 次 45,275 字节；500 次 193,790 字节；1000 次 366,814 字节；2000 次 706,268 字节。completed 条目数分别为 100/500/1000/2000。1000→2000 次增加约 339 字节/调用，确认线性增长；数值包含 Python 其它保留分配，不是 tombstone 的精确独占内存，更不是整个 worker RSS。
- 证据 `/tmp/flowork-worker-record-cost.py`、`/tmp/flowork-worker-record-cost.json`。该实验在独立本地进程执行，不调用线上模型，不创建平台任务或改动生产数据。
- 宿主机 `WorkflowExecutionDriver._run` 的 invoke 补发只发生在初始投递响应丢失、同代 status 明确 not_found 时；完成后先持久化，再 release/ACK。`WorkflowRpcClient.call` 本身不自动重放请求。当前 worker 保存的是 ACK 后防重信息，而非待写数据库的结果副本。
- 不能据此直接删除 completed：同代迟到 invoke、ACK 响应丢失、close 重试仍须验证明确的协议行为。当前实测成本不支持为了该问题引入 worker 轮换；F5 仍未完成，不以这次测量代替有界生命周期修复。


### F5 迟到请求与 ACK 响应丢失的真实 RPC 验证

- 扩展 `engine/tests/test_runtime_process_rpc.py`，通过真实独立 runtime 进程和 Unix socket 验证 succeeded/cancelled 两类终态。
- 在结果 ACK 前建立另一个 invoke 连接，只发送前半帧；另一连接发送 ACK 后不读取响应即关闭。通过新 status 确认 ACK 已被处理，再重复 ACK，均正常。
- 随后发送旧 invoke 的后半帧。worker 返回原终态 acknowledged=true，而不是再次执行；events 查询为 not_found，证明完整事件载荷已释放。
- 这确认“宿主机已落库”与“旧控制请求不再可能到达”不是同一件事。直接删除 completed 而没有明确的旧请求拒绝规则会破坏防重；仍不引入 worker 轮换。
- 与审批及事件背压组合回归 30 项通过（9.17 秒），证据 `/tmp/flowork-runtime-late-rpc.log`；git diff --check 通过。本轮修改仅为测试和验收记录，未修改或重启线上 runtime。F5 的有界清理仍待实施。


### P5 保留轮询，减少已完成记录的重复扫描（待部署）

- 成本定位：`serve_loop_parallel` 每轮调用 `active_executions`；原实现会对历史已结束 CLI 的锁文件反复 open/flock/stat，而不仅是扫描新作业。独立本地文件系统实验中，1000 条完成记录下重复扫描 100 次耗时 3.393 秒，CPU 3.376 秒；空记录约 0.0017 秒。
- 最小修改：观察到锁已释放时，将原锁文件原子移动到已有 `local-execution-finished` 目录，保留退出凭据，移出活跃扫描范围。`execution_alive` 继续准确区分活跃、已退出、未知；已完成 ID 不允许重新使用。沙盒关闭前同时收集活跃及完成目录，避免移除沙盒后丢失既有退出证据。
- 同脚本对照：1000 条完成记录后，100 次扫描耗时 0.0070 秒，活跃目录条目为 0。证据 `/tmp/flowork-activity-scan-before.json`、`/tmp/flowork-activity-scan-after.json`、`/tmp/flowork-activity-scan-cost.py`。本地缓存与机器负载影响绝对值，不据此宣称线上吞吐增加相同比例。
- 没有修改 20ms 间隔，没有增加 IPC、线程或事件通知组件。完成凭据仍保留至沙盒清理；这是避免重复扫描，不是删除历史，也不是解决 F5 常驻推理 RPC 的 completed 字典。
- 活动生命周期、SIGKILL、CLI 查询、沙盒失败关闭与成功关闭共 15 项通过（16.97 秒）。首次未指定测试 PostgreSQL 环境导致 fixture 初始化失败，改用独立测试库后完整通过；未涉及生产数据库。
- 以服务用户 flowork 在真实 Bubblewrap 沙盒运行后台批量 CLI：网关关闭后两条样本仍完成；退出后活跃标记清空、完成凭据保留、execution_alive=False。通过（7.48 秒），证据 `/tmp/flowork-activity-real-sandbox.log`。首次运行发现测试仍查询旧 `/data/.flowork-runs` 路径，按现行 `/data/runs` 修正后重跑；业务执行本身已完成。
- 代码尚未发布到线上服务，仍需新建真实平台沙盒验收后才能标记部署完成。该变更落实用户“先测成本、简单改造、不强求事件化”的要求。


### P5 发布后平台验收及新增边界（未全部通过）

- 已部署 local_activity.py 与 manager.py 两处修改并重启 flowork.service；文件 cmp 一致，认证后的任务列表请求正常。重启前 test 无活跃任务、部署及对话执行。
- 在原 QA 对话 `3add8cac-1cbd-4e46-81ed-d2ce9eee9bb8` 选择 GPT-5.6-Terra，只给验收目的，由 Agent 自行使用 CLI。批量 run `61a0ec2f72db4929bd3d0408ac9fe23d` 终态 completed，2/2 success；按同 ID 查询状态和完整结果成功，未使用模型节点。
- 单次审批 run `3ecb782fa36a426e9573ab444b3f523e` 返回审批链接，对应平台 execution `e45827f7-0bad-590f-9c95-436610108cbc`。Agent 无法从其工具进程的 /proc 看到返回 PID 150，因此没有盲目 kill。宿主机核对实际进程 3492172 的命令模块、该 run 的 worker.log 与 NSpid=3492172/150 后，通过 pidfd 精确 SIGKILL。
- Terra 再查同一 run：execution_status=interrupted、terminal=true、execution_process_exited；无业务结果，results_complete=false，未重跑。CLI 自身退出识别通过。
- 但平台 execution 仍为 waiting_approval，且五秒内没有发布 guest finished marker。进一步检查表明当前存活进程只有 Agent runtime，没有 serve-parallel 文件作业 supervisor；平台 `local_execution_exited` 只读取 `_fileop_pool.work_root` 的完成标记，尚未覆盖 Agent runtime 自身启动 CLI 的路径。不能把 CLI 查询正确等同于平台审批记录已收尾。本次已将此缺口纳入待修复范围，未伪造数据库终态。
- 证据 `/tmp/flowork-activity-platform-response.txt`、`/tmp/flowork-activity-platform-after-kill-response.txt`、`/tmp/flowork-activity-platform-history.json`；浏览器均正常结束。精确 kill 后检查 marker 的脚本断言失败，因此没有生成预期的 kill 成功证据 JSON，不将该断言计为通过。


### Agent runtime 中本地 CLI 的退出确认修复（平台复测进行中）

- `local_execution_exited` 在文件作业完成标记不可用时，复用已存在的 RuntimeBusRouter 向当前 Agent runtime 查询 run_id 的锁状态，不冷启动沙盒或模型，不新增事件总线。
- guest 使用原有 execution_alive 判断：只有明确 False 才给平台正向退出证据；仍活跃、未知、通道断开、查询超时均不能冒称执行已结束。独立查询 ID 通过原路由隔离，完成/失败后解除订阅，不占用对话回合。
- 新增同时查询活跃/结束/未知执行的整链路组件测试，包含无 fileop worker、跨租户拒绝、断线以及两秒超时后的订阅清理；与既有运行隔离/活动标记/CLI 查询共 20 项通过（19.38 秒），补充超时后单例重跑通过（7.82 秒）。证据 `/tmp/flowork-agent-exit-bus-tests.log`、`/tmp/flowork-agent-exit-timeout-tests.log`。
- 两个修复文件已安装到部署目录，服务重启及全新审批执行的 SIGKILL 平台复测正在进行。之前的数据库等待记录不能据此自动宣称已修复；需核对新执行的真实终态。


### Agent runtime 退出确认：部署与真实平台复测通过

- 修复已发布并完成服务重启，真实 QA 对话继续使用 GPT-5.6-Terra。Agent 启动新审批 run `f2d24a606ab84499b31ac8e84a77adc2`，对应 execution `45257f09-6918-5576-b705-5c37d120223d`，确认 waiting_approval 后停止当前回合，保留后台执行。
- 宿主机通过真实进程模块及该 run 的 worker.log 关联确认 PID 3498004，使用 pidfd 精确 SIGKILL。平台从 waiting_approval 自动收敛为 failed/error_code=execution_lost，观察时间 2.267 秒；未手工改库，也未重跑。证据 `/tmp/flowork-agent-exit-platform-check.json`、`/tmp/flowork-agent-exit-platform-check.log`。
- 真实 Chromium 打开执行详情页，显示 Execution details · Failed 和进程退出原因，审批节点显示 Execution interrupted，无 Approve/Reject 按钮，无 pageerror；已人工查看截图。证据 `/tmp/flowork-agent-exit-page.json`、`/tmp/flowork-agent-exit-page.png`。首次浏览器验证因本机未安装 headless shell 改用已有 headed Chromium/Xvfb；首次页面定位误按独立 Failed 文本匹配，修正为完整 h1 后通过，未因此修改产品页面。
- 本轮修改文件 Ruff 检查和 git diff --check 通过。修复的是 Agent runtime 仍存活时其 CLI 子进程退出的识别；整个 runtime 或服务重启后，旧 run 的锁证据丢失仍需单独审计。上一条复现 execution `e45827f7-0bad-590f-9c95-436610108cbc` 在重启前已失去执行进程，不用新运行通过来掩盖该旧记录的收尾缺口。


### CLI 审批补齐沙盒进程身份（部署复测中）

- 复用 Workflow 已有 capture_process/process_group_gone：在宿主机 workflow.prepare 中，从当前 Agent runtime 的真实进程句柄获取 host/boot/PID/group/start，加入签名审批能力，并在首次审批记录创建时持久化到现有 runtime_process JSON。身份不取自 CLI 自报 PID，不新增表或生命周期管理器。
- 回收流程先检查所属沙盒进程组是否已确认退出；若仍在，再使用 guest 执行存活查询。API 重启不影响仍存活的沙盒；整个沙盒或原宿主机启动批次结束时，可依赖持久化身份收尾，不必恢复内存中的旧执行。
- 授权签名、真实数据库审批、真实进程组退出/子进程仍活跃/观察失败/取消/幂等回收等 28 项通过（30.08 秒）；真实 runtime 句柄获取身份与活动标记 8 项通过（10.24 秒），Ruff 通过。证据 `/tmp/flowork-cli-owner-process-tests.log`、`/tmp/flowork-cli-owner-handle-tests.log`。第一组测试结束后 DBOS 测试监听线程在临时数据库删除时打印清理警告，pytest 退出码为 0；这不是线上数据库故障。
- 六个文件已复制到线上部署目录，服务重启及真实平台复测进行中；未将未复测的整个沙盒退出场景标为完成。此前缺少进程身份的旧 QA 记录仍不能凭空补造来源信息。


### CLI 审批所属沙盒关闭：真实平台复测通过

- 已部署进程身份修复。Terra 在专属 QA 对话启动 run `bf297252d3874c628bfd2accd8441202`，平台 execution `a0d5c1f7-f3eb-5316-8147-71393eee3b06` 确认等待审批后，通过项目沙盒关闭 API 停止所属沙盒。
- 关闭响应确认 runtime_process=stopped；平台在 4.945 秒观察到 failed/execution_lost，关联审批均为 execution_lost，未手工修改该执行记录。证据 `/tmp/flowork-cli-owner-platform-check.json` 及同名脚本/日志。此项验证整个项目沙盒关闭，不冒充服务异常崩溃后的恢复验收。
- 另行清理此前明确 SIGKILL、但旧签名不含进程身份的单条 QA execution `e45827f7-0bad-590f-9c95-436610108cbc`：严格核对租户、发起人、Workflow、run ID 后调用既有失败收尾逻辑。这是已知测试遗留记录的人工清理，不计作自动故障识别通过，也未给历史记录伪造进程身份。


### P9 POSIX Preview 刷新缺口与最小修复

- 真实部署中创建专用 Markdown，通过 Preview 保存后 revision 改变、重新 resolve 返回新正文，但订阅只收到 preview_ready，观察 8 秒未收到变更。证据 `/tmp/flowork-preview-live-before.json`。原订阅依赖 VFS 数据库事件，POSIX 写入不产生这类事件。
- POSIX 分支改为每 2 秒检查当前文件元数据（不读取正文），仅变化时推送；15 秒空闲心跳；沿用每 5 秒权限复核。删除后保留观察以支持重建；重连通过 preview_ready 核对当前版本。POSIX 的刷新信号不宣称为持久化文件操作历史，间隔内的多次修改可以合并为最新内容。原数据库存储分支仍采用通知与游标回放。
- 不增加文件监控服务、数据库文件索引或沙盒写入代理，覆盖 Agent 直接写文件的路径。测试真实 POSIX 修改/删除/重建、权限撤销与取消等待，2 项通过（7.83 秒）；Ruff、diff check 通过。首次漏配测试数据库触发 root initdb 错误，指定隔离测试数据库后重跑通过。
- 正在部署并补真实服务更新、删除/重建、重连、跨账号拒绝验收；尚不据本地测试宣称 P9 整项完成。


### P9 POSIX Preview 部署与页面验收通过

- 按用户补充原则“从简、方案合适、资源消耗少”，POSIX 分支连首次订阅的 VFS 变更表查询也已删除。文件变化不写数据库记录；数据库仅用于资源解析/权限校验。每个打开预览仅检查目标文件元数据，关闭连接停止，非整目录扫描。
- 已部署并重启，服务 active，源码与部署文件一致。真实 API 验收：编辑约 2.005 秒收到新版本，删除收到 delete，重建收到 upsert；断线后修改再连接，ready 与当前文件一致；test2 被拒绝 404。证据 `/tmp/flowork-preview-live-accept.json`。首轮跨账号脚本未携带 Secure Cookie（通过本地 HTTP）得到 401，修正测试携带方式后复测 404，不把匿名拒绝计为账号隔离验证。
- 真实 Chromium 打开独立 Preview 页面，原始正文变更为新正文耗时 1501ms；离线期间修改后恢复网络、整页刷新均显示最新正文，无 pageerror。截图已人工确认。证据 `/tmp/flowork-preview-browser.json`、`/tmp/flowork-preview-browser.png`，测试文件 finally 清理返回 200。浏览器测试是在删除残留变更表查询的最终部署上运行。
- Preview 路由与 POSIX 观察器回归 21 项通过（78.82 秒），Ruff 与 diff check 通过。证据 `/tmp/flowork-preview-regression.log`；测试结束时 DBOS 监听线程在隔离数据库删除后打印清理警告，pytest 退出 0，非线上故障。
- 本条完成 POSIX Preview 的更新与重连页面验证；不覆盖后台任务事件流，也不替代其它待补验收。


### F5 实施方案收敛：复用执行槽，不新增生命周期组件

- 当前代码核对：WorkflowRpcPool 已复用逻辑槽；同一槽释放 ACK 完成后才归还。WorkflowInvocationSlot 在传输错误时保留 invocation_id，重试仍发同一调用。进程重启已有 generation 隔离。
- 拟利用上述边界：宿主机每个槽持有固定槽 ID 和递增调用序号，同一次 invoke 重试必须保持序号；worker 对每槽保留最高已接受序号及最近执行对应关系。收到较小序号直接拒绝，不能在已清理完成记录后重新执行。
- 新调用只在前一次终态已 ACK、输入和容量校验通过时替换槽的最近记录；仍运行或未交付完成的调用不得被替换。同序号不同执行 ID 或不同输入拒绝。同一执行 ID 在不同槽重复投递也需拒绝（宿主机持久领取仍为权威，worker 保留当前/最近记录范围内检查）。
- completed 按槽复用时移除上次完成记录，保留当前槽最近一次终态以处理 ACK 回执丢失。旧槽已复用后查询旧 ID 可明确 not_found，业务历史由宿主机数据库查询，不能因此重新 invoke。迟到旧 invoke 带原槽/序号，须始终被拒绝。
- 上限目标为实际并发槽峰值，而非累计执行数；不引入 worker 轮换、TTL 扫描、额外 RPC 或数据库查询。不限并发配置仍允许真实并发状态增长，不宣称固定常数总内存。
- 该方案尚未实施、未部署。必须先补槽复用与迟到请求/ACK 丢失/输入冲突/跨槽并发/代际隔离的验证，再以原 2000 次执行实验对照内存。已有测试不能证明尚未实现的回收行为。


### F5 执行槽回收实现及独立进程验证（尚未部署）

- 宿主机 WorkflowInvocationSlot 增加固定 slot_id 和递增 slot_sequence，同一次传输重试不递增，释放后下一次调用才递增。引擎 invoke 必须携带槽坐标，不保留旧协议别名或可选回退。
- 引擎仅在新调用校验成功、旧调用已 ACK 后替换该槽的完成记录；更低序号、同序号不同调用、未 ACK 时抢占、跨槽使用仍被记录的同一调用均拒绝。最近一次终态仍支持重复 ACK。更早业务历史由宿主机数据库负责。
- 引擎审批/背压/槽复用/真实进程 RPC 共 32 项通过（9.58 秒）；实际 Unix socket 下 ACK 回复丢失、半帧迟到、槽复用后旧请求拒绝、兄弟调用保留及进程代际拒绝通过。首轮遗漏一个旧测试的新增协议参数，补齐后完整重跑。
- 宿主机模拟丢失首次接受响应，确认两次 invoke 的槽/序号及参数完全一致，release 后下一调用序号递增；1 项通过（6.29 秒）。该项为组件测试，不冒充真实沙盒验收。
- 原 2000 次真实 Start→End 引擎实验复测：100/500/1000/2000 次 completed 均为 1，活跃及未 ACK 均为 0；Python 保留分配分别 12,745/32,256/34,296/34,686 字节。原 2000 次为 2000 条及 706,268 字节。只比较该实验分配，不等同整个进程 RSS；不限并发时边界仍是并发槽峰值。证据 `/tmp/flowork-worker-record-cost-after.json`、`/tmp/flowork-slot-fence-tests.log`、`/tmp/flowork-host-slot-tests.log`。
- Ruff 与 diff check 通过。代码尚未发布；真实 Bubblewrap 中的宿主机—worker 全链路及平台调用需继续验证后才能标记完成。


### F5 真实沙盒调用链通过，准备平台复测

- 将当前 API/engine 源码复制到专用临时可读目录，以服务用户 flowork 执行真实 Bubblewrap 测试（不是 mock provider）。共享 worker 取消隔离、调用槽释放后复用、进程退出后新代恢复、多 worker 分配与共享 /run、不限并发超过四槽，共 3 项通过（12.18 秒）。证据 `/tmp/flowork-slot-real-sandbox.log`。pytest 缓存目录只读警告不影响测试，临时源码目录随后删除。
- 新协议宿主机与 engine 两处配套文件均已安装到部署目录，重启进行中。重启前 test 无活跃 Agent 回合，其可见 deployment 均 disabled。下一步复用无模型 QA deployment，连续调用后立即停用，不保持额外常驻资源。


### F5 平台部署验收发现准备阶段失败（待定位）

- 宿主机和引擎配套修改已部署，服务 active。复用无模型 QA deployment `4d874c52-4752-4b98-8193-c3eb9c69d2fb` 验收，首次在服务尚未监听时连接失败（未提交调用）；服务起来后立即调用返回 503。
- 改为等待部署 ready 后再发业务请求，发现 rollout_status=failed、rollout_error=preparation_failed；sandboxd 同样记录 candidate_not_ready。尚未开始本轮连续 12 次业务调用，不计为平台验证通过，亦不能未经定位归因于新协议。
- 两次启动尝试均 finally 停用该专属 QA deployment，当前 enabled=false，不保留额外测试部署资源。证据 `/tmp/flowork-slot-platform.log`；下一步定位真实部署准备失败原因，再完成平台复测。


### Deployment 准备失败定位与资源目录修复

- 从真实 sandboxd 环境读取限定的资源配置，以服务用户执行资源分配器：报 FileNotFoundError，路径为已配置的 `.../flowork.service/instances/cgroup.subtree_control`。实际 cgroup 下 supervisor 存在，instances 不存在；该错误在 Workflow/worker 初始化前发生。尚未证明是谁移除了空目录，不猜测为推理或模型故障。
- 资源预留时在现有互斥区按需创建已配置的 instances 叶目录，仅新建时启用 cpu/memory/pids；仍沿用原控制器检查、预算上限与存活实例保护。不递归创建任意祖先、不新增后台修复任务。
- 使用真实委派 cgroup 和服务用户，在专属 QA 子目录连续两次完成“不存在→预留→核对 CPU/内存限额→释放→删除空目录”，通过。证据 `/tmp/flowork-cgroup-recreate-real.log`。没有停止现有部署进程。
- 既有资源回收测试 3 项通过（8.69 秒），原生 cgroup probe 增加空根目录重建断言，Ruff/diff check 通过。修复已安装，服务重启与原 QA 部署的连续调用复测进行中。


### F5 平台复测通过，历史结果仍完整

- 修复资源目录后，原无模型 QA deployment 成功 ready，连续 12 次 session-authenticated test-invoke 全部 succeeded，输出均为 answer=42。不是模型质量测试，也不是吞吐压测。证据 `/tmp/flowork-slot-platform.json`。
- 随后逐条读取这 12 次 execution：全部 succeeded，完整结果仍由数据库提供；最早和最近调用均保留 __end__.answer=42，worker 的完成记录回收不删除平台历史。证据 `/tmp/flowork-slot-history-check.json`。
- finally 停用 QA deployment，再次查询 enabled=false；服务已 active/exited（oneshot 正常状态）。本轮不保留测试部署常驻资源。
- F5 原缺口已由执行槽有界记录解决并部署验证；仍保留其它 F/P 项尚未覆盖的边界验收，不将此项通过扩大为全项目完成。


### F4 大文件与线上缓存权限补验

- 新增真实加密文件存储 20,000 行结果回归，核对错误行筛选、数值降序、偏移分页的完整预期；核对缓存总字节/条目上限；同路径替换内容后 version 更新且只返回新内容；不同租户/用户/任务/文件互不串用。与并发填充、加载期间变更拒绝、授权先于缓存等既有用例共 10 项通过（13.87 秒）。证据 `/tmp/flowork-result-table-large-tests.log`。
- 线上 QA Task `85ee0ecf-1d8e-4444-b051-17c2cb72fc9e` 查询 4 条结果：同条件重复请求返回一致；缓存填充后以真实 test2 登录查询同一入口仍 404。证据 `/tmp/flowork-result-isolation.json`。没有改动业务结果文件。
- 验证范围明确：20,000 行是独立环境真实文件与实际查询实现；线上仅既有小结果文件，不声称已完成线上大数据量压力测试。Ruff/diff check 通过。本轮只增加回归和验收证据，无需部署新业务代码。


### P8 原生下载确认验收尝试（中断，未通过）

- 使用真实部署插件、Chromium 与 GPT-5.6-Terra，原生引用本地临时页面，要求点击页面 Download CSV 后读取生成的文件；未替 Agent 编写下载命令。打开扩展的文件 URL 权限，以覆盖 Blob 下载所需的本地确认。
- 第一轮直接打开侧边栏时编辑器未在 60 秒内出现；第二轮先进入主应用再进入侧边栏后成功发送。截图确认 Terra 已开始运行，页面引用存在；此次复用了已有 QA 会话且带有之前的引用，不声称是干净的新会话。
- 观测约 70 秒时脚本进程退出码 143，未生成最终结果；最后日志 running=1，confirmed=false。不能据此断言 Agent 失败、下载成功或确认提示缺失。证据 `/tmp/flowork-download-confirm-live.log`、`/tmp/flowork-download-screen.png`。
- 此项仍待实机完成；下一次先从真实会话确定执行终态与浏览器连接，再继续确认/重开流程，不把不确定的运行重新启动为另一次任务。

### P8 下载测试页缺陷定位（继续实机复测）

- 后续 Terra 回合已正常结束，明确报告没有可归属的下载，不是后台仍在执行；未声称读取成功。侧边栏作为普通标签页测试时，发送后需切回目标网页，避免目标绑定到扩展页面。
- 独立真实 Chromium 探针捕获页面异常 `URL.createObjectURL is not a function`：测试按钮内联 onclick 的 `URL` 被解析为 document.URL 字符串，未实际创建 Blob 下载。修正为 `window.URL.createObjectURL` 后，三种下载行为下均收到真实下载事件，Chrome downloads API 均返回 complete、安全状态和 9 字节文件。证据 `/tmp/flowork-download-observer-probe.json`。这是测试页缺陷，不作为产品代码缺陷修复。
- 修正原端到端测试页，并使用浏览器默认下载行为保留正常文件名，重新交由 Terra 点击、等待本地确认和读取。该轮仍在进行，不能以探针成功替代插件完整验收。

### P8 Terra 下载确认与侧边栏重开通过

- 修正测试页后，真实部署插件已显示本地确认；刷新侧边栏后确认仍存在。最初脚本把 iframe 刷新加载期无 Stop 按钮误判为回合终态而关闭浏览器，服务端如实记录 result_unknown/browser_disconnected，未传输成功。已修正验收脚本：恢复选中的对话并等待当前回合真正完成；不是修改产品以绕过失败。
- 最终真实 GPT-5.6-Terra 流程通过：页面点击生成 Blob → waiting_for_local_confirmation → 刷新侧边栏仍显示原文件 → 原生点击确认 → approved → transferring 21/21 bytes → succeeded、durable → shell 读取 CSV → 回答 ALPHA=731。
- 服务端工具输出确认文件 sha256 为 `984fa3389970c98ab7ebbad56416743414f56beed429072b5af5b09cd6ce1abc`、21 字节，读取内容为 `name,value\nALPHA,731\n`。没有仅凭 Agent 最终回答判定通过。
- 证据：`/tmp/flowork-download-confirm-result.json`（confirmed/reopened 均 true，finishedAt=2026-10-07T12:47:45.616Z）、`/tmp/flowork-download-confirm-history-latest.json`、`/tmp/flowork-download-confirm-pending.png`、`/tmp/flowork-download-confirm-final.png`。确认截图已查看。复用了既有 QA 会话；浏览器为真实扩展侧边栏页面的独立标签页测试，不声称已验证实际 side-panel 容器尺寸。
- 用户取消确认分支继续单独实测。此前原生下载/侧边栏组件回归共 22 项通过，证据 `/tmp/flowork-download-confirm-unit.log`；组件通过不替代上述实机链路。

### P8 Terra 用户取消确认通过

- 使用不同测试页面和 CSV 内容（DELTA），仍由 GPT-5.6-Terra 自行点击下载；确认面板刷新后仍在。实际点击 Cancel 后面板消失，Agent 收到 `download_local_cancelled`，明确 `No file was transferred; the local download was kept`。
- Agent 最终说明用户取消、无法读取 DELTA，未再次触发下载、未凭上轮文件回答。本轮没有 transferring/succeeded 记录。成功传输和取消两条实机路径均已核对工具回执。
- 证据 `/tmp/flowork-download-cancel-result.json`（cancelled=true、reopened=true）、`/tmp/flowork-download-cancel-history.json`、`/tmp/flowork-download-cancel-final.png`。测试浏览器正常关闭，未留下后台 Agent 回合或测试 HTTP 服务。P8 正常确认、重开恢复、取消验收完成；其余 F/P 项仍按各自未覆盖范围继续，不扩大为全应用完成。

### P2 活跃 Task 多窗口、离线恢复通过；发现刷新终态订阅问题

- 真实服务创建无模型 2 行批量任务，一行跳过审批、一行等待审批；两个独立浏览器上下文打开任务日志，第二窗口断网，第一窗口进入执行画布点击通过，任务正常 finished。第二窗口恢复后日志补齐，再刷新也完整。
- 有效验收 Task `60b938dd-3f33-4821-b99e-24136f402d7d`：两个窗口均显示与持久事件一致的 10 行，按事件顺序逐项核对（重复消息按出现位置顺序核对），无遗漏/额外行；pageerror 为空。证据 `/tmp/flowork-task-offline-result.json`、`/tmp/flowork-task-offline-final-events.json`、`/tmp/flowork-task-offline.png`。截图已查看。
- 前两次脚本未展开默认折叠的 Events 区域，等待隐藏文字超时；两次任务均已 finished，不算展示漏日志证据。修正脚本点击展开后上述验收通过。
- 截图发现独立问题：刷新已完成任务时，历史已包含最后的 terminal，SSE 从该游标之后等待，不再收到 terminal，页面仍显示 Live 且保持无用订阅。
- 小范围修复：已完成批量任务且游标等于持久化最新 terminal 时，授权后返回 HTTP 204；前端收到 204 标记 done 并中止连接。游标落后、末行尚非 terminal、运行中及定时任务继续原订阅。API 8 项回归通过（11.85 秒），前端 3 项通过（6.34 秒）；Ruff/diff check 通过。前端初次误用旧 Vitest --minWorkers 参数，移除后实际执行通过。
- 两处业务文件已同步部署，服务重启进行中；尚需验证线上 204 与刷新后的 Stream closed，不将回归通过代替部署验收。

### P2 刷新终态订阅修复已部署验收

- 服务于 2026-10-07 12:58 UTC 完成重启，真实浏览器连续两次进入同一已完成批量任务并展开事件日志：均显示完整终态日志及 Stream closed。
- 每次访问日志订阅仅收到一次 HTTP 204，观察期间不重复请求；两次状态为 `[204,204]`，pageerror 为空。截图已查看。证据 `/tmp/flowork-task-terminal-live-result.json`、`/tmp/flowork-task-terminal-live.png`、`/tmp/flowork-task-terminal-live.log`。
- P2 本轮补齐活跃批量任务的多窗口、断网跨终态恢复、持久日志逐项核对、刷新终态状态；已修正实测发现的空订阅问题。全目标尚有其他边界验收未完成，不据此宣称全应用验收完成。

### P3 审批放行、完成与取消竞争实机通过

- 真实服务分别创建 3 个独立的无模型单样本批量任务，均等到 HumanApproval 等待后，再从两个并发 HTTP 请求提交批准与取消。取消延迟分别 0、0.05、0.5 秒，实际最终为 cancelled、succeeded、succeeded，覆盖不同获胜顺序；这不是高负载压力测试。
- 每次执行的持久事件 seq 从 1 到 last_seq 连续、只有一条 result 终态，且与 execution detail 状态一致。终态后再次取消均 HTTP 409，返回 execution_already_finished；再次读取状态、last_seq 和结果保持一致。
- 对应 task 最终为 interrupted、finished、finished；没有遗留 queued/running 任务。证据 `/tmp/flowork-cancel-race-live.json`、`/tmp/flowork-cancel-race-live.log`。未修改数据库记录以制造通过结果，本项未发现需要新增的产品修复。
- 验证范围为真实平台的审批放行/取消命令与完成竞争；服务进程异常退出、租约失效仍单独待验，不用此结果替代。


### P9 后台事件流的真实范围核实与边界验收

- 全生产源码检索 `BackgroundJobsRepo.create_idempotent` 只有定义，没有生产调用方；sandbox_bus 的 background_job_request/event/result 也仅有常量定义，没有收发处理。测试用例直接创建记录；普通 Task、Deployment 与 Agent shell 后台进程不是此 ChatToolJob 注册表，不能混为一类。
- 因此撤下“让 Agent 创建此后台任务并验收”的未经证实计划，不为验收新建后台执行机制，不在生产数据库伪造记录冒充端到端执行。
- 真实 test QA Chat 后台记录数为 0，事件接口两次连接均 HTTP 200，分别约 15.14/15.18 秒收到 heartbeat；主动断开后重连正常。真实 test2 查询该 Chat 的后台列表与事件流均 HTTP 404。证据 `/tmp/flowork-background-interface-live.json`、同名脚本和日志。
- 隔离 PostgreSQL 中并发两个 job 的提交通知、游标后读取以及 Chat 子资源私有权限两项回归通过（19.93 秒）。证据 `/tmp/flowork-background-edges.log`。测试数据库删除后 DBOS listener 发出连接警告，为测试进程清理阶段，不是线上流断开证据。
- 精确边界：真实服务验证的是已有只读/空流接口；有内容的并发提交与游标补读由隔离库证据支持。当前无真实生产者，完整后台工作生命周期不宣称通过；未来启用创建入口须重新验收该链路。现阶段不把不存在的入口作为阻止本轮收尾的运行故障。


### F4 并发查询与响应性补验

- 线上只读复用既有 300 行 Task `87efe690-a5bc-4a78-bb7b-093e21e8ceb6`，结果 JSONL 约 460,685 字节。6 组翻页/排序/状态筛选基准，两个并发查询者共 24 次返回均与基准相同；未修改任何业务结果文件。
- 同时对真实 /healthz 进行探测：空闲 12 次中位 7.785ms、最大 14.63ms；并发查询期间 80 次中位 11.795ms、最大 48.6ms；表格查询最大 324.28ms。证据 `/tmp/flowork-result-responsiveness-live.json` 及同名脚本/日志。只是此次小并发工作负载，不推导最大 QPS。
- 新增独立回归使用真实加密文件 20,000 行、实际 query_results 路由函数及实际筛选排序实现，4 个并发请求均核对完整期望页与缓存字节上限；执行期间事件循环仍推进 261 个探测 tick，最大间隔约 155.46ms，总查询约 2.83 秒。1 项测试通过（含环境准备 9.63 秒），Ruff/diff check 通过。证据 `/tmp/flowork-result-concurrency.log`。
- 此独立回归仅替换授权/任务元数据查找，文件解密、缓存、搜索、排序、线程调用均真实；没有伪造慢查询 sleep。与真实线上 API 对照分别说明，不能声称线上已运行 20,000 行任务或验证所有容量上限。已有 10 项结果正确性回归此前已通过，本轮仅新增并发真实负载证据。


### 持续生成的历史展示与画布离线恢复补验

- 主对话真实 Terra 续聊：553 次 DOM 观察中原有 4 个消息文本块均保留；53 个运行期采样的历史消息相对滚动区域偏移最大为 0，页面异常为空。证据 `/tmp/flowork-long-chat-active-result.json`。
- 此次截图同时暴露编辑框整体上移，因此上述结果仅证明消息文本和内部滚动锚点，不证明页面整体布局正常。后续重复测试 298 次观察、25 个运行期采样未复现上移，不能据此标记修复。证据 `/tmp/flowork-chat-layout-active-result.json`。
- 画布对话真实 Terra 第二轮运行期间离线 2.5 秒再恢复，前一轮文本没有消失，第二轮最终消息正常显示。初版测试以渲染 key 判断缺失，记录到 24 次 key 变化；改为比较实际可见文本后缺失为 0。没有把 key 变化当成产品消息丢失，也没有为此修改业务代码。证据 `/tmp/flowork-canvas-event-recovery-result.json`。
- 2026-10-07 本轮命令菜单更新重启期间公网短暂返回 502；服务启动完成后公网首页及 test 登录接口均返回 200，登录建立会话成功。尚未完成的布局排查不计为已上线修复。

### 一级命令先填入编辑框：已部署并实机验证

共享 ChatComposer 在处理二级菜单分支之前先写入一级命令。主对话和 Workflow 画布均通过真实浏览器验证：选择 `/goal`、`/skill-use` 后立即显示相应前缀；新建目标保留 `/goal `，退出 Skill 菜单保留一级前缀，完成选择输出 `/skill-use:[名称] ` 且没有额外斜杠。证据 `/tmp/flowork-command-prefix-live.json` 与两页截图。插件使用同一组件，但本次没有单独进行原生侧边栏菜单验收。

### 编辑框上移：外层滚动隐患定位

真实页面中，消息区域溢出使 ResizablePanel 内层容器具有额外 scrollHeight；主动设置该容器 scrollTop=330 会把编辑框从 y=589 移到 y=259。仅在消息区域设置 overflow:clip 后，同样操作的外层 scrollTop 保持 0，编辑框保持 y=589。证据 `/tmp/flowork-chat-layout-scroll-container.json`、`/tmp/flowork-chat-layout-message-clip.json`。这是可复现的结构隐患，尚未证明最初截图由哪一次浏览器焦点或滚动操作触发。

代码在 ChatPage 的消息区域容器增加 overflow-clip，编辑框与其菜单位于该容器外。浏览器临时应用相同样式后，历史内部仍可从 0 滚到 2395，主页面和画布的命令菜单交互均通过，证据 `/tmp/flowork-layout-command-regression.json`。构建与正式产物验证另行记录。

### 用户反馈：刷新后当前轮用户消息重复

用户提供 PPTX 对话截图，chat `3bf36c29-9330-4d08-bc04-c2790ddd367f`。只读查询数据库历史接口确认只有一条用户消息；以当前 turn 读取 before_turn_id 时固定历史为 0 条。前端共享 useConversationHistory 合并窗口时只追加、没有剔除已缓存的当前轮历史，因此实时恢复与历史重叠。不能用最终落库正确证明运行期显示正确，前面的消息完整性测试未覆盖这一时序。

修复：恢复 SSE 的 onopen 先载入该 turn 之前的固定历史，完成后才启用实时回放；共享历史窗口以固定历史总数作为位置边界，保留更早已加载页面、移除当前轮历史。既不按文本去重，也不修改数据库消息或重新提交用户请求。相关回归 23 项通过，覆盖首轮空历史边界、更早分页保留、先恢复历史再开放回放；构建和部署路径检查通过。真实服务重复刷新、弱网及相同内容两次发送正在补验，尚未据此宣布完整修复。

附部署记录：布局静态文件发布时因新 index.html 属主错误短暂出现 500，已将静态目录属主恢复为 flowork 并确认公网 200。后续静态发布统一以 flowork 用户执行，先发布资源再替换入口；不重启后端。原编辑框 overflow-clip 调整已随静态产物上线，完整实机回归仍需最终记录。

### 刷新重复修复：真实恢复验证通过

真实 Terra 对话连续两轮发送完全相同的验收消息，每轮生成中刷新并离线 1.5 秒后恢复。按实际消息容器观察，第一轮 273 次采样最多/最终均一条，第二轮 333 次采样最多/最终均两条；终态再次刷新仍两条。证据 `/tmp/flowork-resume-boundary-live.json`、截图及日志。初版脚本误用仅覆盖助手 Markdown 的选择器，报告 0 条；检查页面文字确认消息实际存在后，改用用户与助手共同的消息容器重新执行两轮，不将该脚本错误算产品缺陷。

用户新增两屏历史要求：初始加载按实际消息区域高度补足两屏；每次向上加载追加至少两屏高度，若已无更早历史则停止，并保留原阅读锚点。共享消息组件已修改；原 62 项回归通过，新增两屏初载/追加测试单独通过。正式产物实机验证继续中。

### 两屏历史与编辑框布局：正式产物实机验证

前端静态更新后，真实长对话消息窗口高度 499px，初始内容 1374px（超过两屏）；点击加载历史后增至 3157px，新增 1783px（超过两屏），原消息锚点偏移仅 0.359375px。编辑框输入区域 y=589、视口高 720，主消息布局外层 scrollHeight-clientHeight=0，外层溢出消除。证据 `/tmp/flowork-two-viewports-live.json`、截图与日志。公网仍返回 200；本次发布未重启后端。此证据覆盖主对话正式产物，插件原生窗口仍需单独复核。

### 用户现场联测失败与验收纠偏

用于用户跨浏览器观察的回合 `t_25bc648e6a734bf6b587705d4db47e26` 实际为 failed/engine_error，私有错误 authorization_unavailable，13:38:13 UTC 结束。脚本只观察 Stop 消失就打印 finished，不能将此当作 Agent 正常回复成功。该次联测明确失败；随后用户“你好”是另一轮成功执行，不能混算为测试通过。暂停往用户正在查看的该对话追加测试指令，后续使用独立对话。

OpenFGA 日志在 13:38:01 UTC 的 ListObjects 记录 grpc_code=4000，底层 iterator/sql read tcp 至本机 PostgreSQL 5433 端口超时；同时间段 BatchCheck 也存在单项 SQL 读取超时。因此本次失败已定位到授权服务的数据读取路径，并非用户无权限或事件传输丢失。尚未证明导致数据库连接读取超时的更底层原因已消除。

待部署改动：历史接口按当前页用户消息关联的 turn ID 一次读取失败状态（限定 chat 和 creator），把错误附到对应消息的 meta；共享 MessageItem 在用户消息下显示本次请求失败及原因，刷新后由数据库恢复。数据库及现有审批历史 13 项通过；新增 API 错误归属等 13 项通过；前端错误更新与重新挂载测试通过。主应用/插件共享组件，仍需实机验证正式产物，不据组件测试宣称全端通过。

Check/ListObjects 的只读授权查询允许瞬时通信或服务端不可用时再尝试一次，配置错误不重试，写入不重试，最终失败仍拒绝授权。新增诊断只记录异常类别或 HTTP 状态，避免泄露请求元组和凭据。此为瞬时故障缓解，不等同 PostgreSQL 超时根因修复。测试与后续部署验收需单独记录。

插件共享菜单及两屏历史已实测：真实扩展页面加载服务端嵌入对话，一级 goal 前缀正确；消息窗口高 636px，历史内容由 1743px 增至 3467px，追加 1724px，超过两屏。证据 `/tmp/flowork-sidebar-history-menu.json`。扩展页面置于普通浏览器标签，不能替代 Chrome 原生 side panel 外壳尺寸验收。

### 授权查询瞬时超时：配置调查

实际 native 启动开启 context-propagation-to-datastore=true。[OpenFGA v1.20.0 官方源码](https://github.com/openfga/openfga/blob/v1.20.0/pkg/server/server.go#L572) 说明默认 false，并说明关闭通常可减少连接 churn。这提示检查当前非默认选择，但不是当前 SQL 超时的根因证明。

以同一版本临时启动两个顺序运行的独立进程，仅改变该开关，使用出错时 ListObjects 请求对现有授权数据作只读对照：true/false 各 40 次均 HTTP 200、返回相同 5 个对象；最大请求时长分别约 65ms/41ms。临时进程均已退出。证据 `/tmp/flowork-fga-context-compare.json`。未复现故障，不据此修改生产开关或宣称性能优化；此测试没有覆盖生产并发与取消竞争。

授权只读瞬时故障重试测试 16 项通过，静态检查通过；新错误显示仍待部署。线上尚有用户回合执行，未为部署中断它。


### 2026-10-07 刷新恢复延迟：新证据与修正

用户在截图中看到 Send，但 cda3392d 对应回合 t_ea0c47b7568b4970aabe8abf8b4479d5 的 active-runs 仍为 running。独立手机尺寸浏览器也观察到 Send 先显示、activity/history/active-runs 请求随后返回，不能认为这轮同步验收已通过。另一个三分钟测试对话 462b182e 已在页面显示六轮进度及最终完成文字；仍须核对服务端终态，不能仅以 Stop 消失判成功。

修正：只恢复当前选中对话的流，避免每次打开页面都订阅同账号其它运行回合；终态补读的本地游标在 active-runs 清理过期记录之前读取；服务端确认 running 后先显示运行状态，固定历史加载完成后才切换消息投影边界。新增回归覆盖无关对话、清理游标时序、历史未就绪时运行状态。16 项针对性测试通过，前端构建进行中，尚未据此宣称正式产物验收完成。

缓存调查先测耗时：本机经 API 的三次状态读取为 649/1400/1291ms，三次历史页读取为 560/2149/184ms（鉴权、数据库、组装均包含在内）。证据 /tmp/flowork-refresh-api-timing.json。不能凭总耗时归因到 PostgreSQL，也不能据此增加 Redis 对话缓存。后续需要进一步区分鉴权、数据库、前端发起与重连等待。


服务端终态复核：三分钟六轮测试 t_4a878aa58a754380ad92bf2f344ce3a8 已于 14:01:08 UTC completed，页面包含六轮及完成标记；45 秒测试 t_ea0c47b7568b4970aabe8abf8b4479d5 此次复核仍 running、无 ended_at，需继续定位，不能标通过。前端首轮构建 SIGKILL，未发布任何静态文件；改为单线程、Node heap 512MB 后重试，待验证。以上修复尚未线上验收。


### 浏览器历史缓存与增量同步（用户明确延期，不纳入本轮实施）

用户提出利用浏览器本地缓存：刷新时先显示近期对话，再同步服务端新增内容；对话中持续更新缓存。计划使用 IndexedDB，按账号、租户/工作空间、对话分区；登出清理；缓存不是执行状态和权限依据。启动应显示同步中，不能把尚未确认的回合当作空闲。按消息 ID 合并并使用服务端顺序游标处理更新，不能只比较最后一个消息 ID：流式回复和工具结果会更新已有消息。写入需合并批次，不能每 token 写一次。缓存损坏、容量不足、不可用应回到正常服务端读取；权限撤销/资源删除清理对应缓存。应覆盖多窗口、断线补读、同 ID 更新、账号切换、缓存过期以及重复消息测试。优先复用当前持久化事件游标，不新增互相冲突的游标来源；正式实施前检查事件保留范围和历史截断边界。

前端恢复补丁已成功构建并静态发布：首次两次 SIGKILL 经内核确认 global OOM；清理可重新下载的旧 CI zip 与 apt 缓存，临时 256MiB swap 后单线程构建成功，部署路径检查通过，公网 200。没有重启 API。正在进行延迟流连接的正式产物只读验收；缓存设计不计为已完成。


### 当前页运行状态恢复正式产物验收

/tmp/flowork-recovery-deployed.json：手机尺寸独立浏览器，人为挂起 GET turn stream，页面仍显示 Stop，并保留“实时同步检查已开始”历史；放行后仍为 running。仅订阅当前 cda3392d 对话，未为其它运行会话建立 stream。测试没有发送新消息；证明消息流连接慢不再阻止已确认 running 的展示，不证明 active-runs 本身无延迟，也不证明该仍运行的业务回合已正常结束。临时构建 swap 已 swapoff 并删除；没有持久修改系统配置。浏览器缓存按用户要求延期，本轮不实施。


45 秒测试进一步取证：心跳 14:09:14 UTC 仍更新，事件 36–40 停在 shell sleep 45 后的 reasoning_summary/tool_start，最后事件时间 13:54:02 UTC。没有 done/error。故不是单纯已完成但前端未收到终态；仍需定位 Runtime/桥接读取是否阻塞。当前保留现场，不强改数据库终态、不重复发起该测试。


### 用户对话中断与释放沙盒纠偏

用户确认释放保护本身正常：项目下确有另一个活跃对话；不将 HTTP409 拒绝释放列为按钮缺陷，不移除保护。该测试回合已经通过正常 cancel API 请求停止，不能仅凭 202 当成取消完成。

用户实际对话 7319957c 的最近两轮数据库终态：t_bb0ea16… 于 13:56:15 UTC failed/engine_error/Deadline Exceeded；t_4e5a2ad… 于 14:05:34 UTC failed/worker_lost。后者紧邻 14:05:28 的主机构建 OOM，但被杀的是构建进程，不是直接证明 Agent 进程被杀；心跳超时与资源压力的因果仍需进一步确认。

独立复现一个消息合并层缺陷：source task 自取消后，coalesce_text_events 不向消费者发终止信号，消费端永久等待。新增测试在修复前 1 failed/4 passed（TimeoutError）；修复将源取消传给消费者，同时在消费者主动关闭时跳过入队，保持满队列清理不阻塞。组合回归进行中；尚未部署，不据此断言上述线上回合根因已经完全查清。


API 已单独重启并通过 healthz；主应用、sandboxd、数据库未重启。此前唯一活动记录为已请求取消的 QA 回合，重启后 active-runs 为空。历史 API 实机返回用户原对话两条 turn_error：engine_error 与 worker_lost。消息取消补丁及授权只读重试已加载；前端失败原因 UI 和电源图标仍待后续静态发布。用户要求重跑原订单报价审计示例，正在独立新对话中选择 Terra；初次自动化选错来源文本导致未提交，不能计作业务执行失败或已启动。


订单审计原始示例复测：独立对话 3e792fb8-ff5c-4641-a3d2-f1933edec609，Terra，run t_27395bc6b41c4b368233ce1eabf481a6，事件从 39 推进至 157，仍 running。只读双独立浏览器上下文、短断网后重连和刷新检查完成，详细证据 /tmp/flowork-order-audit-sync.json；仍须业务终态及部署交付验证。


F2 真实账号切换补测未通过：/tmp/flowork-quote-account.cjs 使用独立新登录 session、真实原生右键引用，AUTH_CLEAR 之后旧引用 grant 已为空；test2 登录 API 成功，但在观察窗口内 sidepanel 的 quote context 没有切到第二账号，断言 Second account did not reach panel，浏览器 finally 已退出。尚未证明这是产品切换缺陷还是测试触发路径问题，不能标为账号切换通过。期间主机内存与 swap 紧张；不立即重开浏览器与订单推理争抢资源。订单复测事件已推进至 281，并进入本地 workflow 执行。

### 串行验收：订单示例暴露本地 CLI 引擎接口遗漏

用户要求逐项测试，禁止 Agent 执行、浏览器测试套件、前端构建重叠。订单复测 t_27395bc6b41c4b368233ce1eabf481a6 于 14:29:53 UTC 正常 completed，无 error_code/error_message；这只证明本轮对话正常结束，业务未完成。Agent 保存了 0d8f15e9a14a v1.sv1，并在整图、单节点、最小 Start→End 执行均失败后明确报告阻塞，没有伪造部署交付。

代码与回归确认根因：WorkflowRuntime.invoke 新增必填 slot_id/slot_sequence，而 local_workflow.run_rows 调用遗漏，节点执行前抛 TypeError。/tmp/flowork-local-slot-repro.log 记录原有本地执行测试失败及缺参原文。已修正为每个并发消费者复用一个 slot、样本递增 sequence，避免每条输入新增永久槽记录。20 项回归通过（/tmp/flowork-local-slot-fixed.log），覆盖真实引擎整图/节点、批量、样本失败、取消、审批通过/驳回/超时以及数据库审批接口；额外断言批量 20 条下槽和完成记录不超过并发 2。Ruff 通过。

修复的 local_workflow.py 已同步至 /opt/flowork。正在原对话中以 Terra 继续原业务任务，尚未将实际部署交付标记通过。原两次服务端中断的因果分析仍独立保留，不能用此次正常对话证明已全部消除。

实机续验已启动同一对话，run t_f3b90d9ccce5408f8212e2451399cfe4。最小诊断执行 0daedc176e03413d99f711acf09d9c4a 为 completed、1/1、0 failed，证明线上沙盒已加载槽参数修复。业务整图恢复节点输出，正常订单 subtotal/total=290，CSV 写读和 healthz 成功；SubAgent 先报未调用 set_output，随后模型服务返回 Nvidia ResourceExhausted HTTP502（Worker local total request limit reached），均为结果文件内明确错误，不是本地 CLI 提前退出。Terra 自行修改并继续验证，截至事件 261 仍 running；不将业务验收标记完成。当前无并行浏览器测试或前端构建。

### 槽机制职责复核（用户要求从简，尚未发布拆分）

订单续验 t_f3b90d9ccce5408f8212e2451399cfe4 于 14:40:02 UTC completed，无服务端错误；空订单实际通过。有效订单仍受模型工具调用/上游容量影响，未创建部署，完整业务交付未通过。

用户指出 CLI 不应为查询状态引入槽。本地 CLI 的槽参数已在工作树移除，WorkflowRuntime 不再保留完成去重记录；临时将原远程保护移到 RemoteExecutionDispatch。49 项本地、审批、真实 RPC 子进程和重复派发回归通过（/tmp/flowork-local-dispatch-separation.log），隔离测试库 teardown 后 DBOS 监听线程另有退出日志，不据此声称生命周期完全通过。拆分尚未同步生产。

继续核对必要性：WorkflowExecutionDriver._run 仅在首次 invoke 传输失败后查询同一 generation、同一执行 ID；查到状态就继续观察，只有 not_found 才再次 invoke。数据库终态在 release/ACK 前写入，release 后正常路径不会重新 invoke。因此“Agent 重试新建资源”不是这套保护的用途；实际路径已有先查状态机制，不能以笼统网络风险证明还必须增加槽。当前传输是进程内 Unix socket 的一次请求连接，而不是无界消息队列；也没有重复派发发生率数据。

下一步应围绕这条真实路径决定是否取消自动重发、只保留执行 ID 查询和明确失败，并验证迟到请求/取消竞争、ACK 丢失后的结果与释放。不能只删除参数就宣布安全，也不再以防御假设扩展 worker 职责。远程槽拆分暂不发布；线上仍是上一轮已实测的版本及本地缺参修复。


### 去除重新派发与槽：工作树进展

WorkflowExecutionDriver 现在只有一次 invoke，派发错误原样交给既有错误处理，不再先查询后重发。WorkflowInvocationSlot 删除 slot_id/slot_sequence；引擎删除完成去重字典；临时拆出的 RemoteExecutionDispatch 已删除，避免只换地方保留复杂性。未修改已经在线的代码。

上一轮删除槽、保留只读查询版本的 52 项回归通过（/tmp/flowork-no-resubmit.log）；进一步移除派发失败后的查询分支，正在补验错误直接传播和超时处理。测试数据库销毁后 DBOS 监听线程报错仍需在测试清理阶段单独修正，不把它视为线上执行错误。尚未完成线上部署及本轮实机验收。

最新验证：去槽后的组合回归 52 passed（44.77 秒）。用户进一步明确同步错误直接返回、异步保存错误，由 Agent 决定后续操作；已移除首次派发错误后的状态查询分支，直接传播原异常。新增 3 项测试验证 ConnectionError、TimeoutError、RPC runtime_error 均只调用一次 invoke，不查询、不重新派发，保留原异常对象。

/tmp/flowork-dispatch-single.log：新增 3 项及另外 2 项数据库状态测试通过；3 项真实 bubblewrap 超时用例在启动前失败，原因是当前 root 测试环境映射后无法访问 /home/flowork 的 Python 解释器路径（Permission denied），尚未进入业务引擎。不得将该组三项记为通过；需用可访问的隔离测试部署运行，不修改生产 home 权限掩盖环境问题。线上尚未发布本轮去槽改动。

真实沙盒超时补验已完成：将必要源码复制到 /tmp/flowork-no-retry-qa，由 flowork 服务账号执行，不改变生产 home 权限。/tmp/flowork-no-retry-live.log：5 passed in 16.29s，覆盖 worker 整体超时、审批超时、已请求超时和数据库观察结果。该证据是本机隔离环境真实 bubblewrap，不等同线上服务已更新。

权限查询隐式重试清理：check/list_objects 的一次额外请求及 batch_check 内部错误后的自动重试均移除，失败直接抛授权不可用；不返回部分授权结果。保留不含凭据/tuple/服务商原文的失败日志。12 项回归通过（/tmp/flowork-auth-single.log，12.16 秒）。这些改动仍待部署。

历史说明更正：自动重新派发代码在本轮 Git 基线中已存在，不能称为本轮新增；slot_id/slot_sequence 是本轮新增，现在已撤销。前面将两者一并归为本轮引入的口头说明不准确。

### 去槽与隐式重试清理：后端已更新，平台回归进行中

宿主机派发相关 4 项测试通过（/tmp/flowork-invocation-single.log，7.80 秒）。部署前数据库确认本租户无 running/queued/waiting_approval 的 Deployment 调用及 Agent 回合。仅更新六个后端源码文件，顺序重启 API、worker、sandboxd，未构建前端或插件。sandboxd health 和 API healthz 均通过，六个线上文件与工作树 SHA256 一致（/tmp/flowork-no-retry-deployed.json）。发布执行记录 /tmp/flowork-publish-no-retry.log。

Terra 真实页面回归启动于原对话 3e792fb8-ff5c-4641-a3d2-f1933edec609，run t_52e01ad2f01e43b9b385af529ef95298：要求按顺序验证空订单单次、两条批量、状态/完整结果，再调用已有 Deployment 空订单路径；不新建或修改资源、不调用模型、不自动重试。当前 running，未报验收通过。启动浏览器已关闭，无其它并行测试。

### 去槽回归结果与 Deployment 启动环境问题

- Terra 回合 `t_52e01ad2f01e43b9b385af529ef95298` 已结束，未自动重试。单次本地执行 `a9baec155d9545029d557f67cfdcfd0e` 成功；批量 `b9fd3695e47848ba86662296ea220796` 两条均成功，状态及完整结果查询通过，无模型调用。
- Deployment 调用 `95f9318e-a510-43f9-b6bd-af1fa5522a3d` 失败。sandboxd 日志确认异常为 `deployment_resource_delegation_unavailable`，发生在资源准备阶段，尚未进入图推理。不是模型 API 错误。
- 原因是本轮临时局部重启脚本未继承 systemd 入口设置的 `SANDBOX_CGROUP_ROOT`，且 daemon 被启动到操作会话的 cgroup；正式 systemd 入口 `with_cgroup_delegation.py` 已有该设置。
- 已仅重启 sandboxd，恢复 `flowork.service/supervisor` 成员归属及 `flowork.service/instances` 资源配置，进程环境和 daemon 健康检查通过。没有重发失败调用、没有修改其失败历史。
- 尚需修复准备阶段异常被完成记录泛化为 `execution_failed` 的问题，并以新的一次明确验收调用验证 Deployment。现阶段不能标记部署调用验收通过。

### 准备阶段诊断保存与新一次实机验收

- Deployment runtime 捕获准备/执行异常后仍原样抛出，不重跑；脱敏后的异常类型及内容存入执行历史的加密结果，CLI `deployment result` 返回 `errors`。API-key 外部响应仍不暴露宿主机内部诊断。
- 实际 PostgreSQL 回归验证准备仅调用一次、原异常抛出、失败终态保存、密钥脱敏、CLI 查询可见及外部响应隔离：1 项通过（8.41 秒）；相关 Ruff、diff 检查通过。
- 三个后端文件已更新，API、worker、sandboxd 重启时继承原运行环境并恢复 systemd supervisor cgroup；资源目录配置保留，`/healthz` 返回 ok。
- Terra 新验收回合 `t_56ffdca0416c451c8cb9ac820dc76435` 已实际启动，只允许对已有 Deployment 发起一次新调用、查询状态和结果，不自动重试。仍待业务终态证据。

### Deployment 去槽后实机调用通过

- Terra 回合 `t_56ffdca0416c451c8cb9ac820dc76435` 已终止运行；新调用 `0c03953c-075e-4a1e-bf16-1eb20b1629b4` 成功。
- Agent 调用、状态查询、完整结果查询均指向同一 ID，`result_available=true`；独立通过执行详情 API 复核持久化状态 `succeeded`、`error_dict={}`。空订单返回业务状态 `invalid`，属于预期输出，不是执行失败。
- 本次不覆盖外部 API-key 鉴权、网络接入或模型推理质量；未重发前一次失败调用。
- 转入原文档 F2 的真实账号切换/引用边界复核，其余测试不并发运行。

### F2 账号切换复现与局部修复（未部署）

- 单独运行真实扩展账号测试 `/tmp/flowork-quote-account.cjs` 再次失败于 `Second account did not reach panel`；第一次原生引用成功，AUTH_CLEAR 后旧引用权限为空，test2 登录成功，但已打开的侧边栏仍保留旧账号身份。证据 `/tmp/flowork-quote-account-recheck.log`。浏览器已退出。
- 代码核实：AUTH_SYNC 只更新存储，未通知已打开侧边栏；AUTH_CLEAR 同样没有清除嵌入页面的身份显示。不能把清除引用权限等同于整个账号同步完成。
- 主应用账号变化监听还遗漏同 tenant 下 user_id 变化，已补充监听及回归：1 项通过（5.04 秒）。这项仅是局部修复，未部署；尚须补齐打开中侧边栏的账号同步及一次性 exchange code 在多窗口下的消费规则，不能广播同一码后假设所有窗口均可成功兑换。

### F2 多窗口登录交接方案核实

- 尝试在真实扩展 Service Worker 中兑换一次性 code，然后让 iframe 读取共享会话；主应用页面可正常签发 code，但 SW 兑换返回 403，iframe 仍为原身份。证据 `/tmp/flowork-exchange-probe.log`。不为这个方案放宽既有来源校验。
- 探针发现浏览器自动化的 APIRequestContext 与真实页面请求对分区 Cookie 的行为不同，code 签发改用主应用页面内 fetch 后成功；后续账号切换验收应使用真实页面登录/退出，避免将测试工具 Cookie 行为当成产品缺陷。
- 正式代码尚未改变登录码交接方式。需在保留 iframe 兑换与现有来源校验的前提下补齐账号变化通知；一次性 code 只能交给一个窗口兑换，其余窗口在兑换完成后刷新共享 Session。

### F2 真实页面账号切换通过：撤销此前缺陷推断

- `/tmp/flowork-quote-real-ui.cjs` 使用全新浏览器配置，通过主应用登录表单登录 test；原生右键引用建立 1 条权限；点击主应用用户菜单退出后权限为 0；通过登录表单登录 test2，已打开侧边栏在核对期间切换到新账号；新建对话并原生引用后仅有新账号新对话的 1 条权限。
- 账号与对话 ID 均不同，证据 `/tmp/flowork-quote-real-ui.log`、`/tmp/flowork-quote-real-ui-result.json`。本次没有 APIRequestContext 登录，也没有伪造 AUTH_CLEAR；旧脚本输出中的 method 固定文本已在独立结果文件更正。
- 此结果否定了前文把旧测试失败直接判为“产品账号不能切换”的结论。SW 不主动通知本身不足以证明整个链路失败；现有恢复逻辑在真实操作中完成了账号同步。不因此重写登录码消费机制。临时交接模块草稿已撤销，未部署。
- 同一租户下 user_id 改变未触发主应用同步属于独立静态缺口，保留已通过测试的两处源码/测试小改动，待下一次前端发布。

### 对话失败历史与待发布前端验收准备

- 失败消息展示、账号同步、同/跨身份 Session 恢复共 3 文件 7 项回归通过（10.95 秒）。
- 真实原对话 `7319957c-d41c-494b-9c2f-5529325f0349` 的完整历史 API 已返回 `engine_error: Deadline Exceeded` 与 `worker_lost` 两条持久错误；普通尾页未含对应早期用户消息，扩大历史页后可见，不是错误记录丢失。
- 全量 TypeScript 检查在 448 MiB、768 MiB 堆限制下均因 Node 堆不足退出，不能记为通过；不是类型报错，也未修改线上产物。需后续安排足够内存完成此检查。
- 正在串行构建前端，Vite 限制 Node 堆 448 MiB、原生线程数 1；临时增加 `/tmp/flowork-ui-build.swap` 192 MiB，构建/检查结束后必须关闭并删除。此时尚未发布新前端。

- Vite 正式构建及 deployment-path 检查已通过；临时 192 MiB swap 已关闭并删除。前端产物尚未发布，待补足全量类型检查的内存条件后再继续页面验收；不得把构建成功当作类型检查通过。

### 待发布前端已完成检查并上线

- 清理 3 份已不再使用的旧临时测试源码副本，未删除业务文件或数据库；临时增加 256 MiB swap 后，1024 MiB Node 堆下全量 `tsc -b` 检查通过。Vite 构建、部署路径检查及 7 项回归先前已通过。
- 新前端产物已发布至正式两处静态目录，保留旧哈希资源支持已打开页面；源码同步包含失败消息展示、沙盒电源图标、同 tenant 用户切换监听和中英文本。API 健康正常，无后端重启。证据 `/tmp/flowork-error-ui-published.json`。
- 临时 swap 已关闭并删除。真实历史页面脚本首轮因自动载入历史导致按钮 DOM 替换、Playwright 等待点击稳定失败；该轮不能记为页面失败展示通过。调整历史载入操作后重新验证，不触发 Agent 或 Workflow 执行。

- 正式页面实测通过：原对话加载出两条失败标记，刷新再载入历史后内容与数量一致，没有重复；截图已人工查看，错误显示在对应用户消息下方，编辑框保持在底部。证据 `/tmp/flowork-failed-history-ui.json`、`/tmp/flowork-failed-history-ui.png`。该证据覆盖历史失败展示，不替代 P3 尚待完成的服务异常/租约失效终态验收。

### P3 独立 Workflow 执行进程异常退出：实机通过

- 创建单条无模型审批批量任务 `122952c6-b3aa-4b11-a965-791f935281b2`，执行 `c2495374-3b7a-46f0-b75e-8b410f6b75c4` 确认进入 waiting_approval 后，读取其持久化进程身份并核对 PID、启动时间、进程组、任务归属，只对该执行进程组发送 SIGKILL。
- 平台自动收敛为执行 `failed / execution_lost`、审批 `execution_lost`、批量任务 `finished_with_errors`。再次查询该任务仍只有一个执行，无自动重派发。未手改数据库终态，未调用维护函数制造完成。
- 正式任务页面显示 Finished with errors、总行数 1、成功 0、失败 1；截图已查看。证据 `/tmp/flowork-worker-loss-live.json`、`/tmp/flowork-worker-loss-live.log`、`/tmp/flowork-worker-loss-ui.png`。
- 该异常路径没有引擎 result 事件（进程已终止），终态依据平台落库状态；不将正常完成的“恰好一个引擎 result”断言用于该场景。API、结果及任务页面已一致结束。
- 覆盖范围是独立推理进程异常退出；共享服务整体退出和租约失效仍不据此冒充完成，继续单独核对剩余范围。

### P3 租约测试复核与旧 QA 遗留清理

- `test_deployment_expiry.py` 5 项实际 PostgreSQL 回归通过（10.77 秒），覆盖过期执行确认退出前不释放容量、取消优先、其他 worker 不被停止及重启后 cgroup 退出确认。部分进程控制为模拟，不能替代共享服务故障实测。
- 全局活跃记录检查发现唯一遗留为旧 QA local run `9bac1b143b9b49f4a88d718b832fd325` / execution `e1336033-e1df-5542-98e0-bd598411fe4c`。本文早期 P4 已记录其专用 Project 沙盒释放及原进程不可用；创建时尚缺宿主机进程身份。
- 已核对固定执行 ID、Workflow、发起用户和 local run 身份后，将这条旧测试记录明确清理为 execution_lost。此为测试遗留清理，不算当前版本自动故障收敛证据，不为它加入历史兼容代码。

### P3 共享 sandboxd 故障实测启动

- 首个专用 Deployment `cedbd2f3-3635-4639-b1ea-e071867d4faf` 使用旧审批图，因未指定审批人而在调用准入时被拒绝；未开始推理或故障注入，已关闭该测试部署。
- 创建独立副本 `4e9ab6a208d6`，仅明确审批邮箱；新测试 Deployment `30060258-9437-4231-8c31-22ac609b2c8b` 的调用 `0f9e0afb-6cf8-4b0c-aed5-339352bfc0c7` 已进入 waiting_approval。
- 故障前全局确认无 Agent 活跃回合，活跃 Workflow/Deployment 均只有本测试 ID；随后终止 sandboxd 进程组，API 保持健康，等待原 60 秒租约自然失效。重启命令与完整进程环境已保留，测试脚本 finally 负责恢复并关闭专用部署。
- 此时验收尚未完成；终态、恢复后独立新调用及清理结果待核对。证据 `/tmp/flowork-daemon-loss-live.log`、`/tmp/flowork-daemon-loss-live.json`。

### P3 共享服务退出恢复结果与租约证据纠偏

- 首次故障脚本误把 sandboxd PID 文件中的 supervisor 当成实际 daemon；终止 supervisor 后 daemon 子进程仍存活并续租。因此原 70 秒等待不能作为租约自然失效证据，原脚本 stage=passed 也不能据此证明完整租约测试。
- 核对实际旧 daemon `3670292` 后终止其进程组，恢复服务自动将原执行标为 `failed / execution_lost`、审批 execution_lost；随后明确发起的新调用 `4a33d274-5669-4591-9a4e-e9f45a5a3a4e` 成功。该结果证明服务退出后的故障状态处理及新调用恢复，不证明此前等待期间无续租。测试部署已关闭。
- 单独启动修正后的租约测试：复用专用测试部署，仅发起一个新的待审批调用；通过父 PID 和完整模块名核实 supervisor 的 daemon 子进程，一起终止后再等待自然租约过期。新证据独立写入 `/tmp/flowork-daemon-lease-live.json`，不覆盖上一轮证据。


### P3 共享服务与自然租约失效：修正后的实机验收通过

- 新调用 `8a817925-9911-42b4-b9f1-bee43f3f3cd6` 进入 waiting_approval 后，核对并同时终止 supervisor `3688365` 与实际 sandboxd `3688366`；等待 70 秒，超过该执行的 60 秒租约，再恢复服务。没有主动修改数据库终态。
- 原调用自动收敛为 `failed / execution_lost`，关联审批也为 execution_lost。仅随后明确发起一个新调用 `1007455c-86a1-4fb9-88ee-dda2cbbee4d9`，结果 succeeded；未重发原调用。
- 测试部署 `30060258-9437-4231-8c31-22ac609b2c8b` 已关闭；测试脚本正常退出，API `/healthz` 返回 ok。证据 `/tmp/flowork-daemon-lease-live.json`、`/tmp/flowork-daemon-lease-live.log`。
- 该证据证明服务中断超过租约后恢复时的失败收敛及新请求可用；不声称服务停机期间仍能完成沙盒执行，也不证明所有基础设施故障。


### F2 双窗口原生引用写入重叠补验通过

- 真实 Chromium、部署插件、主应用登录表单 test 登录，两个窗口选择同一已有 QA 对话。分别通过原生右键菜单引用 `/one` 与 `/three`，未直接注入附件或 grant。
- 为确定性覆盖重叠，在测试 Service Worker 中仅暂缓实际 `chrome.storage.session.set`，等两个原生处理器均抵达写入后一起释放；实际 Chrome 存储写入、附件发送和后端草稿保存未替换。浏览器关闭后测试包装消失，无生产源码改动。
- 检查两个独立 tab/window/target grant 均保存，侧边栏草稿 DOM 同时含两份新引用的完整文本。草稿已有旧 QA 附件，故未用总附件数冒充新引用数；截图已查看。证据 `/tmp/flowork-quote-overlap.json`、`/tmp/flowork-quote-overlap.log`、`/tmp/flowork-quote-overlap.png`。
- 本次未发送 Agent 回合；跨窗口引用后的 Terra 实际读取、账号切换、标签页关闭/移动由前述独立实机证据覆盖。本项是受控竞态验收，不是自然点击时序或压力测试。


### 最终审查补充：部署一致性与旧文件操作重试

- 对比工作树与正式目录的已改运行源码和迁移文件：77 个一致；差异仅测试文件。证据 `/tmp/flowork-final-source-parity.json`，该比较发生在本节后续文件操作改动之前。
- 发现旧 `SandboxSession._submit_fileop_inner` 对带 resubmit 的特定基础设施错误自动再次提交同一操作；已删除判断函数与二次提交分支，原错误直接返回。9 项参数化回归通过（12.17 秒），覆盖 read/write/run_command 与三种错误，并断言底层仅提交一次；证据 `/tmp/flowork-fileop-single-final.log`。此项尚未部署。
- 另发现旧批量排队 reconciler 会重新投递 queued 任务，需继续核对首次派发失败的返回/持久化与排队恢复契约，不能声称全应用已无自动重投递。


### 编辑框附件压缩及横向滚动：已发布

- 共用 ChatComposer 默认使用 32px 高紧凑附件标签，最大宽度 176px；完整内容点击查看。附件行限制为容器宽度、单行横向滚动，删除按钮禁止压缩，支持键盘聚焦。主对话、画布与插件复用该组件。
- 64 项现有组件回归、Vite 正式构建及部署路径检查通过，前端已发布。390px 触屏 Chromium 在真实服务中验证 8 个测试附件，附件行宽 299px、内容宽 1450px，手势滚动到 1150px 后删除最右附件，界面与草稿 API 均剩 7 个。证据 `/tmp/flowork-compact-attachments-ui.json`、`/tmp/flowork-compact-attachments-mobile.png`。未声称 iOS Safari 实机通过。
- 首轮额外刷新断言失败，代码核对发现现有 discardReloadedDraft 明确在硬刷新时丢弃草稿；该行为不属于本次样式改动，不修改此契约。删除持久化改用独立草稿 API 核对，不能声称刷新保留草稿。


### 文件操作与批量入队隐式补投：已清理并部署

- 文件操作提交只调用一次，原始失败结果直接返回。批量创建不再吞掉入队异常；未被 worker 领取的任务保存 failed、脱敏错误及结束时间，HTTP 503 返回任务 ID 和查询提示。已领取任务保留真实运行状态。授权投影失败也明确返回，不能留下永远排队且依赖补投的任务。
- 删除 queued reconciler 实现、注册和权限声明；沿用既有 RETIRED_SCHEDULES 清理正式 DBOS 旧定时项。普通已入队任务仍由 worker 正常领取，不修改队列并发或样本并发。授权投影和资源清理的重试与业务执行不同，本项不声称全项目没有任何重试。
- 6 项批量/维护调度回归通过（14.24 秒），59 项 Task CLI 回归通过（39.29 秒），额外提交失败 CLI 输出回归 1 项通过（7.33 秒）。文件操作 9 项回归先前通过。错误分支为故障注入与实际 PostgreSQL 测试，未声称复现自然发生的线上入队故障。
- 五个运行文件及删除项已发布，重启保留原环境及 cgroup；API 健康正常。发布前无活跃 Agent/Workflow/Deployment 调用。证据 `/tmp/flowork-publish-single-submission.log`。
- 正式 API 新建无模型单样本批量任务 `b408c525-e4c3-453d-975c-b37cce11fa82`，待人工后提交第二项 `ae62ae93-598b-46ae-afbe-c9906d797969`：第二项 queued、执行历史为空。批准第一项后两项均 finished，各只有一个 succeeded 执行，验证正常排队未被移除。审批通过 API 提交，本轮不声称重新测试画布按钮。证据 `/tmp/flowork-queue-single-live.json`。


### 提交前整体检查

- 本轮所有已改 Python 源码、迁移和测试的 Ruff 检查通过；保留了授权测试所需的 autouse fixture 引入，没有为了消除 unused 警告删除 fixture。`git diff --check` 通过。
- 78 个运行源码/迁移与生产文件字节一致，已删除模块生产也不存在；前端与插件测试文件无需发布。证据 `/tmp/flowork-final-source-parity.json`、`/tmp/flowork-final-ruff.log`。
- 本轮提交排除 AutoWorkflow 实验文档、实验 Skill 包及 extension/node_modules 本地链接。不合并 main。原订单模型分支仍有服务商/结构化工具输出失败，不标为通过，整体目标仍保留该待验收项。


### 当前验收范围调整：简单同步／审批样例

用户明确以“创建自动化”第一个样例替换复杂订单样例。新的工作流仅覆盖开始、成对并行、Prompt、条件、Human、结束，Prompt 使用 Nemotron 3 Ultra (free)，Agent 使用 Terra。旧订单验收结果保留为历史，不再作为当前验收门槛。

- 先检查单条及批量 Workflow CLI：到达 Human 后返回异步执行 ID、可查询状态与结果、审批链接；Agent 应将链接告知人工，不得自行审批。
- 验收人员通过真实画布分别点击 Approve／Reject，确认原执行恢复及结果一致，不能以 API 代替此项交互验收。
- 同一固定版本覆盖批量任务、一次性／固定间隔／cron 定时任务，以及部署连续 10 分钟波动调用；核对同步和异步结果、详情页与指标。
- 顺序执行以限制资源占用，执行失败原样记录，不自动重投。当前仅样例组件 4 项测试通过，完整实机验收尚未完成。

- 用户补充：构建并校验 Workflow 后，必须先 render preview，给用户可点击的固定版本画布预览，再执行测试；中英文样例均已补充，不增加平台强制续跑规则。
- 当前实测：Workflow ad1e78c9b3eb/v1.sv1，单次 CLI run 5258517128a64708b869499ca6ccf795 在 Human 返回 async=true 和审批链接，Terra 主动展示链接；真实画布驳回后返回 approved=false / approval_path=rejected。批次 a83513956e8746dab988b8cfa44c3d90 同样返回异步及索引3审批链接，画布已驳回，等待 Agent 查询完整结果。此前模型临时过载导致的失败仍保留，不能统计成审批通过。证据 /tmp/flowork-mixed-current-20261007。

- 样例进一步收敛为一条方向性需求：保留主要节点、模型、render preview、人工点击、CLI/任务/部署覆盖、10分钟波动调用与证据要求；去掉预设字段、样本数、并发/超时值、流量表和详细命令，让 Agent 自行探索。不再通过多轮补充构造最终验收需求。

- 精简后的中英文样例已重新构建并发布，部署路径检查通过。当前对话已实际调用 render_preview。新增 approve 批次受 authorization_unavailable 阻断；对应 OpenFGA 记录为访问本机 PostgreSQL 5433 的 read tcp i/o timeout，并非模型错误；尚未定位连接超时根因，未标为修复。当前完整任务／部署／10分钟验收仍未完成。

- 用户明确当前验收的 Task 和 Deployment 应由验收人员在真实对应页面手动创建、执行、审批和核对，不能仅以 Agent CLI 返回替代页面验收；Agent 负责 Workflow 构建及相关 CLI 能力观察。
- 授权读超时的新对照：当前已知授权请求串行各150次、4路并发各150次，context propagation true/false 均未复现（各0错误）；不足以修改生产配置。新证据 /tmp/flowork-fga-context-current-serial.json 和 /tmp/flowork-fga-context-current-concurrent.json。临时 OpenFGA 进程已退出。

### 当前页面验收进展

- 用户明确授权异常以清晰报错为准，由 Agent 判断是否重试；不因偶发授权读取错误暂停整体验收，也不增加平台隐式重试。
- Task 页面真实上传 JSONL、选择 Workflow ad1e78c9b3eb/v1.sv1、映射三个输入并选择三个输出列，点击提交创建批次 423a7e7c-2d98-47eb-8374-fa9d4de88d93。日志页进入执行详情画布，依次点击 Approve 和 Reject（202），原执行均结束；最终任务 Finished，4/4成功。结果表逐条输出 not_required/approved/rejected/not_required 与动作一致。证据 /tmp/flowork-mixed-current-20261007/task-*。
- 结果页采用固定列（input/output为JSON）而非用户选择的导出列；原脚本等待 approval_path 列超时，源码确认属当前展示方式，不是结果缺失。完整输出和Trace列实际存在，尚需核对导出文件及搜索/链接交互。
- 一次性任务首次验收脚本误以用户显示时区填入日历，但调度表单为 UTC，导致时间晚8小时；这是测试设置错误，原任务 b6ac4116-7755-492b-8201-e77f40c7d5a6 将从页面暂停，另按表单UTC创建新任务；不作为产品调度故障。

- 批量结果搜索 ui-batch-approve-1 返回1条，并点击 Trace ID 进入12节点执行画布。CSV成功下载4行。发现验收脚本在延迟筛选尚未更新时误选同一个request_id字段，导致三个命名输出列都指向request_id；已核对payload证实为测试选择错误，修正脚本精确匹配字段后待补验。
- 一次性UTC任务6420044e-6d41-4da3-a650-9b54660584d5于16:30:52到期、16:30:53开始，Succeeded，只有1次执行，next_run_at=null，页面Finished。
- 固定间隔任务1b4c73ca-b05f-4a28-a75b-c7ab21ae3403在16:31:32/16:32:32/16:33:32各触发一次。首条Human等待期间后两条Queued（本机任务worker并发1），没有跳过。真实页面暂停计划，再从日志执行详情分别Approve/Reject/Reject，3条均Succeeded，任务Paused，next_run_at=null。不能声称多worker同时运行已验证。证据schedule-current-summary.json、schedule-ui-decisions.jsonl及对应截图。

- cron任务63fce1c3-97fe-42f7-a4b5-d8b7b35bf3d1从Task页面创建，* * * * * UTC，16:37触发；页面暂停计划，执行Succeeded，最终Paused。证据schedule-cron-summary.json、schedule-cron-final.txt/png。Deployment页面与连续10分钟验收仍待完成；批量导出字段选择补验仍待完成。

- Deployment 70a947ec-bd28-4811-aa1a-45c74e3bfc02 已通过页面创建；设置页显式切换v1.sv1、超时60秒、worker1/并发4，二确保存200。首次页面Test返回422 approval_email_required，尚未执行推理；原Human未指定审批人，节点spec已有“部署必须显式邮箱”说明。已将实际错误反馈Terra，让其修改并render preview；没有绕过校验或将422算成功。
- 两项脚本问题：创建成功后Close选择器同时命中两个按钮；设置二确最末按钮实际是关闭按钮。均通过精确按钮名修正，原创建只发生一次，错误设置未提交；已确认成功PATCH的配置。

- 修正请求t_213cb2aec6d34a1a9316d9170fa85708在启动阶段因resident sandbox capacity full失败，Workflow尚未执行。检查发现旧订单QA部署b439d3df-4192-49ae-a135-fdab0bf0ed1a仍启用；已在真实设置页关闭Accept requests并二确，PATCH200，保留历史。之后显式重新发出未启动的修正需求，不是平台自动重投。

### 本轮验收续记：资源与部署版本（2026-10-07）

- 依用户要求，仅将线上 `/opt/flowork/.env.launch.local` 的 `SANDBOX_MAX_RESIDENT` 从 4 调至 6，供下次启动读取；未为此打断服务。复核 test 账号 32 个 Project 沙盒均为 released。旧订单验收 Deployment 已经通过页面停用；保留当前验收 Deployment。
- Terra 修复人工审批指定用户并发布 `ad1e78c9b3eb@v1.sv2`，已静态校验并生成 preview。页面 Settings 固定至 v1.sv2，PATCH 200。切换完成前立即测试仍命中旧实例并返回 approval_email_required；数据库 revision 显示新版于 16:51:05 UTC 激活，之后页面测试返回 200，结果 approval_path=not_required。不可把保存设置等同于新实例已经就绪。
- 同步成功证据：execution ed564f23-220e-48c6-ab26-26f622b1639c；耗时约 10.9 秒，包含 request_id、prompt_result、approval_path。
- 16:54:01 UTC 开始当前 Deployment 的 600 秒波动流量验收，单次间隔在 5/6/10/15/20 秒间变化；输入混合同步、人工通过、人工驳回，审批通过真实只读画布按钮完成。尚未完成，不提前判通过。
- 首次失败 e152cb76-e855-4c05-8ec2-66069c8eb772 的持久化节点错误为 NVIDIA Service temporarily overloaded；外部 API 保留通用 execution_failed，完整诊断在鉴权后的执行详情中，不应直接暴露可能含敏感信息的节点错误到公开调用方。

### 当前 Deployment 与导出验收结果

- 连续发送窗口 16:54:01–17:04:01 UTC，最后同步返回17:04:10；70次外部调用，56次成功、12次失败、1次60秒同步超时、1次并发上限429。69次实际入场执行，29次进入Human并返回202；画布实际点击16次通过和13次驳回，全部最终成功，凭API key查询原result_url与数据库终态、业务输出一致，无路由结果不匹配。保留原始失败，无重发。
- 12次失败中11次节点明确记录NVIDIA Service temporarily overloaded，1次runtime_model_upstream_unavailable（执行1248b8c8-c4ff-4db4-a85c-760a5e8a7462）。后一条日志异常被统一脱敏为REDACTED，仅能定位上游转发异常，不能声称已证实具体网络/服务商根因。同步超时de989ebd-d102-4ef9-af40-4d8b44a53a8f的并行Code已成功，Prompt启动后到达60秒期限，返回504并持久化timed_out。
- 数据库独立逐行重算11个分钟窗口的calls、errors、QPS、error_rate、P50/P95，与metrics API逐项一致；真实页面切换5m/10m/30m/1h/3h/6h/24h/7d/14d/30d，均200且bucket=minute。已查看截图，版本v1.sv2、worker1×4、unfinished0及窗口说明正确。证据traffic-window.json、traffic-verified.json、deployment-approval-clicks.jsonl、metrics-verified.json、deployment-metrics-ui.json，目录/tmp/flowork-mixed-current-20261007。
- 导出字段补验：页面创建单条批次f92f293b-42a4-4433-862c-d84091c1301b，固定v1.sv2，精确选择EndNode的request_id/prompt_result/approval_path三个不同字段。页面Download→CSV下载，实际1行success，三列分别为ui-export-sync、模型摘要、not_required，导出映射正确。

### CLI 通过路径补验

- Terra 在本地 CLI 单条执行进入 Human 后主动给出 Run ID 85b9cf3c72724cf2977845fddb4c22cb、审批 ID 和执行详情链接。真实画布通过 execution 806114de-abf1-5046-a3da-04bc6e9bd23c，HTTP202；原执行Succeeded，最终approval_path=approved。告知审批完成后Terra查询原ID确认total=1，无重提。
- 随后Terra顺序发起本地单行批量 run 29a650dbc4c44b8bae48482dd67fed2d，同样主动返回索引0和审批链接；真实画布通过 execution d28bb691-0fbc-5eab-b00a-a93c22036c99，原执行Succeeded，approval_path=approved。与此前混合四行批量/驳回证据合并覆盖，不声称本次单行批量等同多样本混合。
- 已完成的Deployment通过页面停用（PATCH200），无剩余审批；导出补验任务已Finished。证据 cli-approve-clicks.jsonl、Agent对话980bc592-7c03-4106-ae5f-5c75040790b6。
- 版本边界：最初任务与驳回CLI使用v1.sv1；Terra为外部Deployment补显式审批邮箱后，部署、导出补验和通过CLI使用v1.sv2。未将这些分批证据冒充一个完全无需反馈的首轮端到端执行。

### 本轮收尾

- Terra最终查询原批次completed、1/1成功、results_complete=true，并保存单条/批量验收文件。运行完成后确认active-runs为空，释放本轮Project沙盒；返回closed，文件卷已卸载、执行进程停止，历史与持久文件保留。
- 样例最终仅调整中英文末尾说明：由Agent判断是否重试，不增加平台隐式重投。EmptyChatExamples现有4项测试通过（7.54秒），Vite正式构建与deployment-path guard通过。前端资源及说明已发布；本次无需后端重启。
- 当前简单样例的功能路径验收已覆盖；不宣称70次全部成功、不宣称免费模型具备生产SLA、不宣称上游异常具体根因已定位。旧订单实验不再是用户当前验收范围。

## 2026年10月8日全仓复查与行业方案对照

基线：`main@dc60797`。范围包括主应用、侧边栏插件、API、Workflow 引擎、存储与授权、任务和部署链路。依据为全仓结构扫描、主要调用链阅读、官方方案对照，以及下述隔离复现；不等同于逐行安全审计或线上容量测试。

当前优先处理浏览器恢复和资源释放缺陷，再优化重复查询、连接开销和渲染。首次扫描时所有新增项均为待修复、待验证或待评估；后续代码与验证进度见末尾实施记录。只有标明实机通过和部署版本的项目才算交付完成。

### 当前进度索引（2026-10-08）

本次清单共 14 项：浏览器连接 B1–B6，资源及效率 O1–O8。下文问题表中的复现描述与“待修复”是扫描基线；当前进度以本索引及末尾实施记录为准，避免将已做局部修复与尚未实机验收混为一谈。

| 范围 | 当前进度 | 提交与剩余工作 |
| --- | --- | --- |
| B1、B2、O1 | 已有代码修复及局部回归 | `4d23504`；待部署及真实浏览器断网、恢复验收 |
| B3 | 已收窄断线处理范围，受控端点回归通过 | `1ade4a9`；待两个独立浏览器实测，确认互不影响 |
| O2 | 已统一子进程调用截止时间，本地真实子进程测试通过 | `557a64f`；待发布后 Workflow、Task、Deployment 超时及取消验收 |
| B4、B5、B6 | 待生命周期、路由和保活收益验证 | 不提前删掉保活机制，不宣称已支持多 API worker 浏览器路由 |
| O3、O4、O5 | 待测量和逐项优化 | 分别记录连接与事件循环开销、历史请求量、授权请求量 |
| O7 | 已复用未变化的节点及边，32 项测试、类型检查及本地 Chromium 画布交互通过 | 待部署页面验收及渲染耗时对比，详见末尾记录 |
| O6、O8 | 待策略确认或随相关修改收敛 | 不默认清理历史，不为拆分模块新增抽象层 |

上述三个提交仅表示本地代码进展，不代表已合并 main、推送 GitHub 或更新服务。每项交付仍需补齐相应实机证据。

### 实施约束与边界

- 保持方案简单，优先局部修正，不为事件化新增通用消息平台、复杂调度协议或自动重投机制。
- 浏览器传输重连与业务重试分开：允许恢复连接；已发送的点击、导航、提交等操作不得自动重放。结果不确定时返回明确诊断，由用户或 Agent 检查后决定下一步。
- 权限校验、账号及空间隔离、用户主动释放控制的边界必须保留，不能用恢复机制绕过。
- 性能优化需记录前后请求数、耗时、内存或渲染数据；没有实测收益的改造不以“业界先进”为理由推进。
- 浏览器本地对话缓存继续延期。POSIX 存储、Code/Bash 子进程执行方式和现有 Agent 框架不因本次审查而迁移。

### 浏览器连接修复和优化

链路为侧边栏 iframe → 插件 offscreen WebSocket → API 浏览器连接注册表；沙盒内 Playwright 经 CDP relay 使用该连接，由扩展 service worker 调用 Chrome debugger。消息传输在线、控制权有效、debugger 已附加、Playwright 可用是不同条件，不能由一个“已连接”状态替代。

| 编号 | 优先级 | 项目 | 证据及当前状态 |
| --- | --- | --- | --- |
| B1 | 高 | 旧连接发送失败误删新连接 | 调用实际注册表的隔离脚本已复现；待修复 |
| B2 | 高 | Playwright 断开后复用存活但失效的 Runtime | 模拟断开返回的受控测试已复现；待修复及真实断网验证 |
| B3 | 高 | 同账号另一浏览器断线可能误标记当前控制状态 | 静态调用链风险；待双浏览器实测 |
| B4 | 中 | 关闭侧边栏、休眠及令牌过期后的恢复 | 存在 iframe 续期依赖；待生命周期验收 |
| B5 | 扩容前必须处理 | 多 API worker 的浏览器连接归属 | 进程内注册表的明确部署限制；待部署约束及路由验证 |
| B6 | 低 | 冗余保活和旧生命周期假设 | 与最低 Chrome 版本不完全匹配；待收益评估 |

#### B1 旧连接清理误删新连接

[TransportRegistry.send_to](../api/src/vibecanvas_api/browser/registry.py) 在发送失败时调用 `unregister(transport_id)`，未传入原 sender。若旧发送尚未结束，新连接先注册到相同 ID，旧发送随后失败会删除新记录。

复现顺序：暂停旧 sender → 注册新 sender → 令旧 sender 抛出异常 → 检查 `is_connected`。实际结果为 `False`，新连接被移除。

修复方向：复用现有 sender 身份核对能力，只移除失败的那个连接，不增加业务重试。

验收：旧发送失败和旧连接退出均不删除替代连接；新命令经新连接正常返回；只有旧连接时仍能正确清理。补真实断网重连验证，不以隔离脚本代替实机证据。

#### B2 失效 Runtime 未被回收

[BrowserCliRuntime.execute](../api/src/vibecanvas_api/services/agent_runtime/browser_cli_runtime.py) 根据进程退出状态和控制身份变化决定重启。[BrowserRuntime.tab](../api/playwright-runtime/browser-runtime.cjs) 能检测 CDP 断开并返回 `browser_disconnected`，但普通错误结果返回路径未据此清理 Runtime。

受控测试保持进程存活、控制身份不变，模拟 Node 连续返回 `browser_disconnected`。两次调用均失败，`_start` 和 `_stop` 调用次数都为 0，旧进程仍被保留。这证明该返回路径缺少恢复衔接，不代表所有真实断线都会保持相同控制身份。

修复方向：区分动作超时、目标标签页失效和连接失效；仅明确连接失效时清理 Runtime，下一条明确调用重新鉴权并初始化。不能因普通 `action_timeout` 重启连接或重复导航。

验收：进程活着但 CDP 已断、进程已退出、控制身份变化分别处理正确；下一条观察命令可恢复；用户主动停止后不得后台重新接管；写操作结果不确定时保留 `result_unknown`，无自动重放。

#### B3 浏览器断线影响范围过宽

[ws_hub 断开处理](../api/src/vibecanvas_api/routes/browser.py) 通过 [get_active_browser_binding_for_user](../api/src/vibecanvas_api/storage/chat_repo.py) 获取用户的活动控制记录后标记 `lost`，未先证明该记录属于断开的具体浏览器连接。同账号多个浏览器在线时，存在误伤其他控制记录的路径。

修复方向：断开处理绑定到具体 transport、认证 Session 及控制身份；不得仅按用户查找后修改另一浏览器的租约。

验收：浏览器 A 控制中，浏览器 B 断开，不影响 A；A 自身断开状态正确变化；账号切换、过期连接、迟到断线通知均不能修改新控制身份。使用两个独立浏览器上下文，不能只用同一浏览器的两个标签页代替。

#### B4 认证续期与休眠恢复

[插件令牌](../api/src/vibecanvas_api/browser/scoped_token.py) 最长有效期为 900 秒；[WsClient](../extension/src/shared/ws-client.ts) 提前请求续期，经 [sidepanel](../extension/src/sidepanel.ts) 转给 [EmbedChatPage](../web/src/pages/embed/EmbedChatPage.tsx) 获取新令牌。关闭侧边栏或系统休眠时，此链路可能暂停。拒绝过期凭据是正确行为，不能以延长或绕过认证代替恢复。

验收：侧边栏关闭超过 15 分钟后重开、电脑休眠恢复、弱网下续期请求失败、账号切换、服务重启。分别核对新令牌、控制身份、CLI 错误及恢复后的观察结果；避免过期令牌无限重连或显示已恢复却仍不可执行。现有 15 秒心跳、45 秒无响应判定及最长 30 秒退避也需与恢复耗时一起记录。

#### B5 多 worker 路由约束

[PlaywrightControllerRegistry](../api/src/vibecanvas_api/browser/playwright_registry.py) 和浏览器传输注册表均为进程内对象，扩展连接与沙盒 CDP 连接必须进入同一控制 worker。普通客户端 IP 粘性路由不能保证这一点，因为两条连接来自不同客户端。

近期方案：明确并验证浏览器控制相关入口固定到一个 worker，覆盖依赖注册表的授权调用。真正扩容时再评估按浏览器身份路由或独立 relay，不能把数据库权限支持多 worker 等同于浏览器数据通道已经支持多 worker。

验收：在多 API worker 环境有意将请求分配到不同进程，确认部署约束可避免注册表不可见；控制 worker 重启后返回清晰错误，新调用可重新建立连接，不重放旧动作。

#### B6 保活机制精简

[manifest](../extension/manifest.json) 最低 Chrome 版本为 125；[offscreen](../extension/src/offscreen.ts) 仍有每 20 秒保活、每 240 秒重建端口的逻辑。Chrome 118 起活动 debugger 会话能维持 service worker 生命周期，官方也建议避免不必要的永久保活。[Chrome 生命周期文档](https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle)

评估方向：保留必要传输心跳和持久化恢复，减少无控制任务时的唤醒；先验证 Chrome 自然休眠后恢复，再决定是否删除端口轮换，不能直接撤掉 offscreen。

验收：有 debugger 控制、仅普通对话、所有面板关闭、扩展 worker 被回收四种状态；比较后台唤醒次数及连接恢复可靠性。真实 Agent 验证使用 Terra。

### 其他资源和效率优化

| 编号 | 优先级 | 项目 | 证据及状态 | 方向和验收要求 |
| --- | --- | --- | --- | --- |
| O1 | 高 | 子进程创建失败泄漏管道 | [subprocess_pool.py](../engine/src/vibecanvas_engine/subprocess_pool.py) 创建四个管道端后，Popen 失败路径只关闭其中两个；隔离进程连续失败 5 次，文件描述符增加 10 个；待修复 | 完整释放创建失败时的管道；循环注入失败后 FD 数稳定，正常执行及取消仍通过 |
| O2 | 高 | 节点超时未覆盖调用全过程 | 同一文件中先获取 worker、启动并发送，再开始 read_result 超时；受控测试配置 0.05 秒，因等待单 worker 总耗时约 0.542 秒仍成功；待明确语义及修复 | 若保持节点“单次执行 wall-clock”契约，传递统一截止时间到排队、启动、发送和读取；测满池、堵塞发送、慢启动，外层 Workflow 超时与节点超时分别验证，不自动重跑 |
| O3 | 中 | 同步数据库桥接反复建连接 | [sync_session.py](../api/src/vibecanvas_api/storage/sync_session.py) 使用每次新建 NullPool engine；在运行中的事件循环调用时创建线程池并同步等待 result；静态确认，热点成本待测 | 热点优先原生 async、复用同事件循环连接池；保留租户上下文及事务隔离；测连接数、事件循环延迟和请求耗时，不能直接跨事件循环共享连接池 |
| O4 | 中 | 无限历史查询周期性刷新成本随页数增长 | [workflow-history.ts](../web/src/lib/api/queries/workflow-history.ts) 的 useInfiniteQuery 每 3 秒刷新；成本待实测 | 保留已加载历史，更新最新页和活动记录，结合可见性及操作后刷新；测 1/10/50 页的请求数和滚动位置，无历史消失、缺口或重复 |
| O5 | 中 | 多窗口重复授权复核 | [state_notifications.py](../api/src/vibecanvas_api/services/state_notifications.py) 默认每 5 秒授权复核，[preview_workspace_events.py](../api/src/vibecanvas_api/services/preview_workspace_events.py) 每 2 秒检查；成本待测 | 先测数据库及 OpenFGA 请求量，再评估同身份、资源、权限检查的并发合并；不得取消撤权检查或引入长期允许缓存，验收登出、空间切换和撤权传播 |
| O6 | 中 | 执行事件与 Trace 保留策略不统一 | 已找到账号删除及知识库清理，本轮扫描未找到统一执行历史保留配置；待进一步盘点和产品规则确认 | 分清结果、Trace、事件日志、附件的保留和归档；仅处理符合策略的终态数据，保护运行中及审批中执行；验证导出、查询和断线恢复，不自动删除用户历史 |
| O7 | 中 | 大画布全图对象重建 | [Canvas.tsx](../web/src/pages/canvas/Canvas.tsx) 随 draft 变化转换整张图并创建节点及边对象；静态确认，渲染影响待测 | 保留未变化对象，缩小订阅范围；测大图中编辑单节点、拖动、撤销及 Agent 更新的渲染次数，保持只读、选择和布局行为 |
| O8 | 低 | 大模块职责混合 | 基线 sandbox/manager.py 约 4600 行、routes/chats.py 约 4200 行；属于维护性观察，不是性能结论 | 按存储挂载、生命周期、历史读取、运行控制拆分，删除已证实无调用的旧逻辑；避免机械拆文件或新增通用框架，现有公开契约和回归保持一致 |

O1/O2 的数字来自小型隔离脚本，不是线上内存或吞吐基准。O3 至 O7 不承诺未经测量的性能收益。

### 行业对照与保留方案

以下是结合当前实现的选择，不代表同类产品对 Flowork 的性能背书。

| 组件 | 对照来源 | 当前选择 |
| --- | --- | --- |
| API、worker、沙盒 | [n8n Queue mode](https://docs.n8n.io/hosting/scaling/queue-mode/)、[Dify Compose](https://github.com/langgenius/dify/blob/main/docker/docker-compose.yaml) | 保留职责分离；不为减少组件数合并执行隔离边界，也不照搬更重的部署拓扑 |
| 持久任务调度 | [DBOS 队列与并发](https://docs.dbos.dev/python/tutorials/queue-tutorial) | 复用现有队列；业务失败返回错误，区分基础设施恢复与业务重新执行，不新增隐式重投 |
| 状态通知 | [PostgreSQL NOTIFY](https://www.postgresql.org/docs/current/sql-notify.html) | 数据库保存状态，通知只负责唤醒；保留按进程、事件循环及 channel 共享监听和重连补读 |
| 文本事件 | [本地合并实现](../api/src/vibecanvas_api/streaming/text_event_batches.py) | 已有 40 毫秒、8 KiB 合并目标及有界队列，合并后先落库再发布；不把现状误报成每个 token 单独落库，不丢弃终态或工具边界 |
| 权限查询 | [OpenFGA 查询能力](https://openfga.dev/docs/interacting/relationship-queries)、[现有批量授权](../api/src/vibecanvas_api/authorization/service.py) | 列表已有 batch check，保留；优化重点是重复的长连接授权复核，而非重复建设批量能力 |
| 历史分页 | [TanStack Infinite Queries](https://tanstack.com/query/latest/docs/framework/react/guides/infinite-queries) | 重取会顺序处理已加载页，针对 O4 缩小刷新范围；不为降低请求数删掉用户已读历史 |
| 文件预览 | [POSIX 预览实现](../api/src/vibecanvas_api/services/preview_workspace_events.py) | 保留文件元数据定期扫描和打开时刷新，不新增数据库文件变更记录 |
| 执行数据保留 | [n8n Execution data](https://docs.n8n.io/hosting/scaling/execution-data/) | 借鉴显式保存和清理策略，由自身保留规则决定，不照搬其他产品的默认删除期限 |
| 画布 | [React Flow Performance](https://reactflow.dev/learn/advanced-use/performance) | 借鉴稳定引用和细粒度订阅，先测量 O7，保留现有画布框架 |
| 数据库 async 边界 | [SQLAlchemy asyncio](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html) | 优化 O3 的循环和连接生命周期，避免跨任务共享 AsyncSession 或跨事件循环误用连接池 |

### 实施顺序与完成条件

1. 先修 B1、B2，复现并收窄 B3，修 O1；这些直接影响浏览器可用性和资源释放。
2. 明确 O2 超时口径并修正；串行验证 B4 的长时运行、关闭面板和休眠恢复。B5 在增加 API worker 前完成约束或路由方案。
3. 测量 O3、O4、O5 的热点开销后逐项优化；O6 先明确保留规则，O7 用大图测量。O8 随相关模块修改进行，B6 只有在恢复验证通过后再精简。
4. 每项记录代码提交、回归结果、实机结果、是否部署及剩余限制。测试串行进行以控制内存；浏览器实测使用 Terra，包含观察与有副作用操作的区别。

浏览器共同验收矩阵：正常调用、慢页面、动作超时但连接仍有效、WebSocket 断网、CDP 单独断开、Node 退出、用户取消 debugger、关闭或移动标签页、多窗口及多浏览器、令牌续期失败、休眠恢复、账号切换、API 重启与多 worker 路由。操作计数用于确认没有隐式重放；仅健康检查、单元测试通过或“插件已连接”都不代表浏览器命令链路验收完成。


### 新增清单实施记录

2026-10-08，分支 `fix/review-browser-resource-efficiency`，第一批局部修复：

- B1：发送异常只注销原 sender，保留期间注册的新连接。回归同时确认旧动作不重放、后续观察经新连接返回。
- B2：Node 在命令回执中提供私有连接失效标记；宿主机先保存原动作结果及附件回执，再回收失效 Runtime。下一条明确调用才重新鉴权和初始化。普通动作超时不重启；未知写入结果仍保持 unknown；自动观察失败不推翻已成功动作。安装版本同步为 Browser Runtime 0.4.2，防止版本检查跳过更新。
- O1：Popen 失败释放全部管道；第二次 pipe 创建失败亦释放第一对管道。真实系统调用失败的循环测试确认文件描述符不增长。
- 验证：注册表 5 项、Runtime 生命周期 24 项、Node Runtime 14 项、子进程池 9 项通过。API 测试首次因 root 无法启动 PostgreSQL 测试进程出现 5 项 setup error；改用已有隔离测试数据库重跑注册表全部通过，没有修改生产数据库。
- 以上为局部自动化验证，尚未部署或完成真实浏览器断网验收；B3–B6、O2–O8 仍按原清单推进。不能将该批结果作为整个目标完成的证明。


2026-10-08，B3 浏览器断线隔离：

- 删除扩展 WebSocket 退出时按用户查询活动租约并标记 lost 的逻辑。扩展传输退出只注销自己的连接；CDP 控制器发现其对应传输消失后，使用已鉴权的 chat_id、browser_session_id、generation 更新自身记录。
- 已被新控制器替代的旧控制器不能标记 lost 或发送 close；扩展仍在线的正常 CDP 关闭也不标记传输丢失。延续现有控制器身份核对，不增加独立租约索引或业务重试。
- 32 项连接诊断及认证续期回归通过，包含另一个浏览器存在活动记录时退出不修改它、控制传输丢失仅修改自身、旧控制器不影响替代控制器。Ruff 和差异格式检查通过。
- 该批仍是受控端点测试；真实双浏览器、断网和长时运行验收未完成，尚未部署。O2 等其余清单项继续保留。


2026-10-08，O2 子进程调用截止时间：

- 每次 run 入口创建一个 monotonic deadline，获取 worker、发送输入及读取结果共用该截止时间；满池等待不再无限占用线程，获得 worker 后不重新发放完整预算。
- 输入管道采用非阻塞写入与可写等待，子进程不读取输入时仍可超时或取消；取消检查沿用已有 kill_path，不引入调度重试或额外监控服务。
- 同步 Popen 和 JSON 序列化无法由本层即时抢占；其耗时计入预算，返回后先检查截止时间，超时则不继续派发并回收进程。清理也可能有额外耗时，因此不把响应时间宣称为严格零误差的硬实时上限。
- 真实 OS 子进程池 13 项测试通过，包括满池调用超时但原 owner 正常完成、1 MiB 输入无人读取时超时、慢启动不继续派发、无 deadline 时堵塞发送仍可取消，以及进程释放与并发回归。Code worker、异步集成和池生命周期额外 26 项通过；Ruff 与差异格式检查通过。
- 这是本地真实子进程验证，尚未更新线上引擎；仍需发布后的 Workflow/Task/Deployment 超时、取消及共享 worker 隔离验收。

2026-10-08，O7 大画布对象引用初步优化：

- 草稿仍完整转换，以正确刷新跨节点校验警告；转换后按节点/边 ID 复用内容未变化的对象，使用已有 TanStack 的结构共享函数，不新增依赖。保留 React Flow 的选择、测量和拖动状态；干净快照的自动布局规则不变。
- 新增 1,000 节点克隆草稿测试：单节点编辑及撤销均只改变一个节点引用；覆盖删除、重排、警告更新、位置更新、边标签和配对元数据。首次测试发现补充 undefined 字段导致全部引用变化，修正后相关 4 个文件共 32 项测试通过。
- 此结果仅证明对象复用和相关回归，不代表已量得真实浏览器渲染耗时收益。真实大图、Agent 更新和线上验收仍待完成。
- 全量 TypeScript 检查因本机内存压力主动终止（exit 143），未取得通过结果；后续需在独立资源窗口补跑。尚未部署、合并或推送。


2026-10-08，O7 浏览器补验与类型检查：

- 全量 `tsc -b --pretty false` 独立运行通过（Node 堆上限 1152 MiB）。此前限制为 768 MiB 的尝试因 V8 堆上限退出 134，不是 TypeScript 错误；记录保留，不把中止的检查算通过。
- 本地 Vite 测试页加载真实 Canvas、React Flow、CustomNode 和编辑 store，在 Chromium 中展示 200 节点、199 条边。单节点编辑仅该节点 data 引用变化；选中状态保留，undo 恢复原标题，位置修改同步到画布。
- Playwright 实际鼠标拖动将节点从 (150,0) 移到 (230,40)，草稿保存坐标一致；只读画布尝试同样拖动后仍为 (0,0)，dirty=false。无页面 JavaScript 错误。证据 `/tmp/qa-canvas-review.json`、`/tmp/qa-canvas-review.png`；测试脚本及临时页面保存到 `/tmp/qa-canvas-review.cjs`、`.html`、`.tsx`。
- 一次补测因临时 Vite 进程退出 143 导致连接拒绝，确认进程终止后重启并完成测试；未把该次失败误计为业务故障。测试服务器已停止，临时页面不进入产品构建。
- 这是本地真实浏览器组件集成验证，未操作线上已保存 Workflow、Agent 更新或量化渲染耗时，因此不宣称完整生产验收或性能提升比例。O4 已确认历史查询会周期重取全部页面；只刷新首页会漏掉旧审批状态，暂未仓促改动。
