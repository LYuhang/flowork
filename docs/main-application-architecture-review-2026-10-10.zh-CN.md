# 主应用架构、效率与冗余代码复审（2026-10-10）

基线：`main`，`0e6f3c4`。本轮是组件级静态审查、关键路径代码追踪和官方方案对照，不是逐行安全审计或生产负载验收。没有把旧审查文档中的问题直接当成当前缺陷；没有改动线上配置、重启服务或重跑业务任务。

结论：保留当前“API 控制层 + 沙盒执行 + 数据库持久化 + 通知唤醒”的主干。优先修复读取失败后的状态收敛、发布停机风险，然后做局部性能和代码清理。没有证据支持更换整个框架、全量事件化，或增加新的消息中间件。

## 审查范围与证据等级

覆盖 Web 路由/状态/画布、Chat 与 Agent Runtime、Workflow 引擎、Task/Deployment、CLI、沙盒、数据库、通知、POSIX/对象存储、知识库、模型代理、MCP/Skills、预览、权限、可观测性、安装发布与 CI。浏览器部分只覆盖主应用网关与运行时边界，不把本轮称为侧边栏实机复验。

源码规模扫描约为 API 533 文件/13.65 万行、Engine 43 文件/0.95 万行、Web 477 文件/9.99 万行。扫描仅按扩展名排除测试文件，Web 包含约 1.97 万行生成的 API schema；这些数字用于理解规模，不能当作冗余量。

- **已确认**：能在当前代码直接定位到的行为或调用关系。
- **风险/待测**：有触发条件的设计风险，未宣称线上已经发生。
- **建议**：基于当前架构的优化判断，不是“业界要求必须这样做”。
- **P1**：正确性/发布可靠性优先；**P2**：效率与维护性；**P3**：规模达到条件后再做。集群阻塞项在集群上线前必须完成，并不等于当前单机故障。

## 组件对照与处理方向

| 组件 | 当前实现与证据入口 | 对照的成熟做法 | 本轮建议 |
| --- | --- | --- | --- |
| Web 框架、路由 | React/Vite，`web/src/app/router.tsx` 已有懒加载，`web/package.json` 有构建体积检查 | 按路由拆包，测量后优化重模块 | 保留；不要再把“缺少代码拆分”列为问题 |
| 服务端状态与 UI 状态 | TanStack Query + Zustand，`chat-stream.ts` 和 SSE 投影协调 | 服务器状态和临时 UI 状态职责分明 | 保留双层用途，收敛重复的刷新调度；不全量换状态库，见 A1 |
| Chat 历史与流式消息 | `agent_run_stream.py` 持久化事件、连续序号、分页补读、通知唤醒 | 数据库事件是事实，通知只提示变化；断线从游标补齐 | 主干保留；先补读失败测试，不能用纯 Pub/Sub 替代历史，见 A1 |
| Agent Runtime | `services/agent_runtime/codex.py` 约 3130 行，适配层已有协议边界 | 控制层适配运行时，生命周期和消息投影分开 | 按会话生命周期/历史转换/能力发现拆分内部模块，不增加服务 |
| Workflow 画布 | React Flow，多个 inspector 通过 `useNodes()` 获取完整节点数组 | React Flow 建议按所需状态订阅，减少拖动引发的无关渲染 | 优先优化选中节点选择器；先做 100/500 节点基准，见 A6 |
| Workflow 引擎 | `engine/.../workflow.py`：分支共用单次执行拥有的 Code 池，finally 回收 | 执行隔离和资源归属明确，取消能终止实际工作 | 保留子进程；不要因外层超时就改成进程内执行 |
| Task/调度/批量 | DBOS 入队；`task_recovery.py` 对失联执行标记未知结果并清理，不重跑 | 持久化排队与业务重试分开，失败语义明确 | 保留；核对旧注释，明确“不自动重放有副作用执行”，见 A9 |
| Deployment | 执行与控制层分离，状态持久化；UI 指标按窗口展示 | n8n 也分离接入与执行，但队列本身不解决文件共享和所有权 | 保留；扩容前闭合沙盒路由、资源预算和发布入口，见 A3 |
| CLI | API 侧 CLI 入口与 Engine 分离，支持执行查询的设计 | 同步返回结果/错误，人工节点转异步并提供 ID/入口 | 保留异步查询契约；不要为 CLI 引入常驻事件监听或自动重发 |
| 沙盒管理 | `sandbox/manager.py` 约 4480 行；sandboxd 持有 scope 生命周期 | 有状态执行明确 owner；控制层可横向扩展 | 内部按生命周期/工作区/资源准备拆分；多 sandboxd 需显式路由，见 A3/A8 |
| PostgreSQL/SQLAlchemy | 主池 + DBOS 等独立使用方 + 通知专用连接 | 按进程计算总连接预算，LISTEN 保持会话连接 | 保留池化；记录容量公式和连接指标，见 A7 |
| 通知与 Valkey | `state_notifications.py` 每事件循环/频道共享监听；Valkey 用于其他瞬时通信/协调 | PostgreSQL LISTEN 有订阅与读取竞态要求；Redis Pub/Sub 非持久化 | 当前先订阅再补读正确；避免扩大成统一重型事件平台 |
| POSIX 与对象存储 | `workspace_storage.py` 使用相对目录描述符、防符号链接、原子替换；持久化对象另有用途 | 可变工作区与不可变产物分开；多实例必须访问一致存储 | 保留 POSIX；验证共享挂载、备份恢复、同路径并发写，见 A3 |
| Knowledge | `kb_search.py`：授权后加载最多 20001 块，解密文本再词法评分 | 对检索成本设预算、分页候选、Top-K；加密约束会影响索引选择 | 先减少重复计算和内存，不直接上向量数据库，见 A4 |
| 模型代理 | `runtime_model_broker.py` 每请求建 HTTPX Client，带出站地址约束 | HTTPX 推荐复用 Client 获取连接池收益 | 测量握手成本后做有界、安全隔离的复用，见 A5 |
| MCP/Skills | MCP hub 有 adapter/desired-state 边界；manifest probe 在沙盒内；Skills 有独立仓储 | 隔离用户命令，版本/能力发现与实际调用分开 | 保留隔离；避免为了省开销把 stdio MCP 搬回 API；缓存变化以版本为准 |
| 文件/Office/图表预览 | 文件元信息定期扫描；Office 有磁盘缓存、转换槽和超时 | 小规模文件预览可用 stat 轮询；昂贵转换有并发预算 | 不新增文件变更数据库；转换同内容并发可合并，见 A10 |
| 授权与租户隔离 | OpenFGA 客户端、租户数据库/RLS、流式授权复查 | 权限校验是持续边界，缓存须考虑撤权 | 不把权限校验当冗余删除；先度量调用成本再讨论短缓存 |
| 监控与诊断 | Prometheus、结构化日志；部分 Agent 指标 helper 未发现生产调用 | 指标要覆盖真实入口，测试 helper 不等于生产接入 | 核实并补齐实际调用链，避免空指标误导，见 A11 |
| 安装、发布、CI | 锁定依赖、镜像发布、SBOM/测试已有；原生启动仍现场构建 | 预构建制品、启动验证与可回退发布 | 最优先隔离构建和停服，见 A2；无需为了这个引入 Kubernetes |

这里的“成熟做法”分别参考 [n8n 的接入/执行分离与共享数据要求](https://docs.n8n.io/hosting/scaling/queue-mode/)、[Dify 的 Compose 部署入口](https://docs.dify.ai/en/self-host/deploy/quick-start/docker-compose)、[React Flow 的性能建议](https://reactflow.dev/learn/advanced-use/performance)、[PostgreSQL LISTEN 语义](https://www.postgresql.org/docs/current/sql-listen.html)。并非建议照搬这些产品的全部组件；尤其 n8n 的文件存储限制不能直接等同于本项目必须放弃 POSIX。

## 优先优化项

### A1 · P1 · 对话变化通知与补读失败未形成可靠闭环

**代码证据：** `web/src/lib/api/sse/chat-activity.ts:12` 在读取前清除 dirty；`server-active-turn.ts:48` 将网络/HTTP 失败转换成 null；`chat-reconcile.ts` 使用 `Promise.allSettled`，query invalidation 也没有要求抛出读取失败。即使外层加 catch，仍不能完整区分“已读到最新状态”和“补读失败”。

**风险：** 最后一条 changed 已到达，但 active-runs 或历史读取失败；SSE 本身没有断开、没有后续 changed 时，调用可能正常结束但页面仍旧。Query 自身重试、切换焦点等路径可能补救，故不能断言每次都会丢失；本轮未将其认定为用户历史消失截图的唯一根因。

**方案：** 返回明确的补读成功/失败结果。只读补读失败时保留 dirty，使用可取消、有退避的刷新调度；401/403/404 退出相应订阅。借鉴已有 `resource-activity.ts` 的合并逻辑，并保留 Chat 特有的 active-run/终态恢复。查询重试不等于重发用户消息或重跑 Workflow。

**验收：** 最后一个 changed 后让快照读取连续失败，随后恢复且不再发新事件；两个窗口都应最终显示同一终态。另测读取期间再变更、切换 Chat、停止、缺少最后一帧、历史不缩短、不重复显示。

### A2 · P1 · 原生启动把构建失败变成服务中断

**代码证据：** `launch.sh:203` 的 `start_stack` 先 `stop_stack`，随后 `build_extension`，再启动原生服务。解释器预检已经提前，但不能覆盖依赖、类型检查、构建目录权限等失败。

**方案：** 先在独立 release 目录构建并验证 Web/插件/API 版本及资源，再停止/切换服务；启动失败回退上一制品。启动命令只启动已验证的制品。数据库迁移作为显式部署阶段，避免把不可逆迁移当成普通文件回滚。

**验收：** 注入构建失败，旧服务仍健康；注入新服务启动失败，能回退；插件包与 Web 对应同一版本。保留现有 systemd/原生部署即可。

### A3 · 集群上线前必做 · 分布式配置、沙盒 owner 与 POSIX 访问尚未完全闭合

**证据：** `DEPLOY.md:26` 明确 release overlay 未完成外部 PostgreSQL/Valkey 配置；`DEPLOY.md:78` 明确多 sandboxd 分布式归属未实现。同 scope 不能轮询路由到独立 daemon。

**方案：** 分阶段区分 API 多进程、API 多机器、多个 sandboxd。先保持单 sandboxd，验证多个 API 访问同一服务；需要多 sandboxd 时，用明确的 scope→owner 路由，owner 丢失返回可诊断失败，不自动换机重放执行。POSIX 继续使用，但 API/worker/sandboxd 的共享挂载、UID/GID、容量、备份和恢复必须一致。

**验收：** 两 API 交替处理同一 Chat/执行；两个 sandboxd 不产生同 scope 双实例；owner 故障时只失败当前执行；恢复工作区能读到原文件；Compose 渲染结果确实使用外部服务地址。不要仅凭“有网关/有 Redis”就宣称集群就绪。

### A4 · P2 · 加密知识检索的 CPU/内存成本集中在请求路径

**证据：** `api/src/vibecanvas_api/services/kb_search.py:130` 一次读取候选后逐条同步 `_rank`，每条重复规范化/分词 query，收集并排序全部匹配后截取 Top-K。上限限制块数，并不直接限制解密后字节数。

**方案：** 每请求只计算一次 query 特征；按文件/知识库缩小候选；分批读取并用有界 Top-K；设置解密字节及并发预算。将长 CPU 段移出事件循环需测量，线程不是 CPU 加速保证；不要未经基准直接引入进程池或新检索服务。

**验收：** 1000/10000/20000 块中英文语料比较结果一致性、P95、RSS 和同时进行 Chat 请求的延迟。全文明文索引会改变加密数据边界，不能作为默认优化。

### A5 · P2、先测量 · 模型代理重复建立 HTTP 连接

**证据：** `runtime_model_broker.py:1380` 附近按请求创建 `PinnedAsyncHTTPTransport` 和 `httpx.AsyncClient`，多条返回/流结束路径关闭 client，跨请求不能复用该池。

**方案：** 先拆解 DNS/TLS/首字节耗时。只有收益明确才做有界复用；key 必须包含目标、代理、TLS 与地址校验版本，支持失效与关闭。凭据按请求传入，并处理 cookie 隔离，不能让共享 client 将上游 Set-Cookie 带入另一租户请求。不能为复用连接削弱 SSRF/DNS 固定校验。

HTTPX 明确建议避免在高频循环里反复创建 AsyncClient，以获得连接池收益；如何兼容本项目的租户与出站安全约束是本项目自己的设计工作。[HTTPX 官方说明](https://www.python-httpx.org/async/)

### A6 · P2 · 画布与列表可降低无关更新

**证据：** `web/src/pages/canvas/inspector/config-editors/node-graph.ts:45` 的 `useSelectedNodeId` 订阅全部 nodes；RightInspector 等也存在同类订阅。`web/src/lib/api/queries/tasks.ts` 的列表在 enabled 时固定 5 秒读取，不仅有运行中任务才读。

**方案：** 只需选中 ID 的地方订阅稳定标量；确实展示全部节点的地方保留完整订阅。列表保持轮询，但按页面可见性、运行状态设置快/慢频率，并保留手动刷新和本地 mutation invalidation。不能在“当前无运行项”时彻底停止发现其他窗口创建的新任务。

**验收：** React Profiler 比较拖动时 inspector render 次数；列表后台隐藏时请求下降，重新可见能发现外部新任务。遵循 [React Flow 官方性能指南](https://reactflow.dev/learn/advanced-use/performance)，不凭 hook 名字认定所有 `useNodes` 都应删除。

### A7 · P2 · 按进程和频道计算数据库连接预算

**证据：** `storage/db.py:41` 主池配置；DBOS 与其他 session 使用方另有连接；`state_notifications.py` 是每事件循环/频道一个监听连接，不是每个 SSE 一个。当前默认主池上限为 pool_size + max_overflow，不能当成全应用连接上限。

**方案：** 文档化 `各进程数据库池上限之和 + 实际监听频道连接 + DBOS/维护连接 + 余量`。用真实部署进程数验证；连接等待、超时、占用数进入监控。只有监听连接成为明显负担才合并多个频道到同一连接。LISTEN 连接继续使用直连或 session pooling，不走 transaction pooling。

依据 [SQLAlchemy 连接池生命周期](https://docs.sqlalchemy.org/en/20/core/pooling.html) 和 [PostgreSQL LISTEN 会话语义](https://www.postgresql.org/docs/current/sql-listen.html)。本轮没有发现或证明生产已耗尽连接。

### A8 · P2 · 大模块按职责拆分，避免再加一套框架

`routes/chats.py` 约 4132 行、`sandbox/manager.py` 约 4480 行、`ChatComposer.tsx` 约 1829 行、Task/Deployment 详情页各约 1250 行。文件长度只作为维护风险信号，不代表这些代码全是冗余。

建议按实际变更边界抽取：Chat 读投影/提交/附件；沙盒生命周期/工作区同步/资源准备；Composer 命令选择/附件条/发送状态。Task/Deployment 共享的属性展示、日志行可共用小组件，执行状态和操作权限继续保持各自业务逻辑。每次拆分保持 API/输出契约不变，用现有集成测试验证，不能通过增加更多转发层“拆小”。

### A9 · P2 · 更新旧注释，区分排队、恢复清理与业务重试

`services/background_queue.py:116` 仍描述 queued-row reconciler 在投递不确定时重复入队；当前 `background_tasks/task_recovery.py` 的真实职责是隔离失联 worker、标记 outcome_unknown、清理资源，不重跑外部副作用。

前者注释不足以证明仍存在自动再次投递，后者也不能证明所有生产者都已排除重发。逐个核对 enqueue 调用方后，删除失效描述并把真实规则写入 CLI/API 文档。验收用入队失败、失联、超时证明：错误清晰返回，业务没有被偷偷再执行。DBOS 的持久化执行与重试设置是不同概念，应明确配置及业务语义。[DBOS Steps 文档](https://docs.dbos.dev/python/tutorials/step-tutorial)

### A10 · P2/P3 · Office 转换缓存可合并相同请求，资源预算需按进程计算

**证据：** `office_preview.py:92` 在获取转换槽之前读缓存，随后直接转换；同内容并发请求可同时 miss。`BoundedSemaphore(2)` 是进程内限制，不能视为多 API 实例全局最多两个转换。

**方案：** 最小改动是在获得槽后再查缓存；必要时对相同内容做进程内 single-flight。配置每进程转换配额，并按实例数计算总内存。只有多机器转换成为瓶颈才考虑独立转换服务。

**验收：** 相同文档并发 5 次和不同文档并发 5 次分别测转换次数/RSS/响应时间；失败必须释放槽和临时目录。文件变化通知继续用元信息扫描，无需变更记录表。

### A11 · P2 · Agent 指标有“定义了但没接到生产”的候选

**证据：** `observability/agent.py` 的 `record_llm_usage`、`record_tool_call` 在仓库扫描中只有定义及测试直接调用，未发现 API/Engine 生产调用；`AGENT_TURNS_TOTAL` 也未发现生产更新点。这不同于已存在的 Workflow instrumentation，不能笼统说所有指标失效。

**方案：** 沿当前 Codex Runtime 的使用量/工具结果入口核对是否存在替代统计。需要的指标接到真实入口，不需要的旧指标与 helper 一并退役；避免两套重复计数。当前 model/tool label 接收字符串，启用前明确取值集合，详细 ID 放日志而非指标 label。

**验收：** 完成一次真实 Agent 回合，确认计数只增加一次；失败/取消也有正确终态，重连补读不重复计数。仅调用 helper 的单元测试不能替代此项。

## 冗余代码清理清单

方法：AST 枚举私有顶层函数，扫描 API/Engine 源码引用，再检查测试、脚本及仓库文本引用。静态搜索不能证明外部插件/动态导入永远不存在，因此只直接清理无注册、无调用的局部私有函数。

| 项目 | 当前证据 | 处理 |
| --- | --- | --- |
| `capabilities.py` 的 `_provider_from_model`、`_model_name` | 没有仓库调用、注册或测试引用；当前能力识别走其他方法 | **本轮已删除**这两个私有 helper；不改变模型目录和 CLI 契约 |
| `agents/tools/_common.py` | `_current_version`、`_err`、`_human_size` 未发现生产/测试调用，模块名也未发现调用方 | 高置信候选；确认打包和外部适配器边界后可删除整个模块及独有 import |
| `_envelope.py::_inline_or_omit` | 生产无调用，只在 `test_vfs_2b2b.py` 测试；当前还有 `fill_output_data` | 候选；清理旧 helper 和只验证旧 helper 的测试，保留当前输出大小契约 |
| `agent_resources/batch_results.py::_jsonl_record` | 仅 `test_batch_execute.py` 引用，源码生产调用未找到 | 候选；先核对当前 CLI batch JSONL 输出契约，再退役旧文件/测试 |
| `workflow_sandbox_runner.py::_merge_stage_extra` | 只剩直接测试引用 | 候选；确认依赖注入新路径的回归测试覆盖后移除旧逻辑 |
| `sandbox/mcp_probe_entry.py::_append_dependency_paths` | 定义存在，源码无调用 | 候选；先确认是旧代码还是漏掉必要初始化，不能只删定义掩盖 MCP 依赖问题 |
| `sandbox/gvisor.py::_assert_pure_engine` | 未发现调用 | 候选；先确认引擎节点校验由哪里承担，安全校验不能按未引用直接删 |
| 旧队列注释、前端“仅运行中轮询”注释 | 与现有路径/实际条件不完全一致 | 核对后更新，减少以后误改 |
| `observability/agent.py` 的旧指标 helper | 只有测试引用 | 按 A11 选择接通或退役，不能只保留看似通过的 helper 测试 |

明确排除误报：`_bubblewrap_runnable` 被环境门禁测试调用；`_peak_for_test` 等是测试观测入口；生成的 API schema、迁移脚本、动态节点注册、入口点与兼容历史数据读取不能按低引用计数删除。未跟踪的实验文档、`docs/skills/`、`extension/node_modules` 本轮未动。

## 保留、不推进的方案

- Agent/Task 的持久化事件与游标补读保留。Redis Pub/Sub 是 at-most-once，不能保证断线期间消息重放。[Redis 官方语义](https://redis.io/docs/latest/develop/pubsub/)
- 预览文件 stat、少量列表轮询、失联资源清理保留；是否事件化由实际资源收益决定。
- 业务调用失败直接返回可诊断错误；不以“容错”为名自动重新执行。连接恢复和只读查询补读可以单独设计。
- POSIX、Code/Bash 子进程隔离、MCP 沙盒探测、权限复查保留。
- 不立即引入 Kafka、向量数据库、Kubernetes、全局 worker 轮换、浏览器消息缓存。后者仍按此前要求延期。
- 不为所有列表统一改成游标分页；先测深历史与大表，稳定顺序使用时间加 ID。

## 建议实施顺序与验收门槛

1. **正确性批次：A1、A2。** 补读故障注入、双窗口一致性、构建失败不断服。每次只修一个边界。
2. **清理批次：无调用 helper、失效注释。** 确认调用图、删除旧测试而非增加重复测试，跑受影响模块检查。记录行为保持不变。
3. **性能批次：A4、A6、A10。** 先记录基线，再做单点改动；报告请求数、RSS、P95、渲染次数，不只说“更快”。
4. **运维批次：A7、A11。** 连接预算、真实指标、读失败诊断；不增加高基数标签。
5. **有明确需求再做：A3、A5、A8。** 集群上线前 A3 必须闭合；A5 以握手收益为依据；A8 跟随业务修改逐块拆分。

所有运行测试按单项顺序进行，避免并行沙盒和大构建耗尽内存。涉及文件持久化要覆盖“释放沙盒→恢复→重新读取”的完整链路。涉及业务执行必须验证没有隐式重试。

审查阶段的直接代码变更仅为两个无调用 helper 的删除。用户随后授权的实施结果记录在下方；未标为完成的建议仍是待实施方案。

## 本轮验证记录

- 删除前完成函数名、模块及测试引用扫描；两个删除的私有函数只有定义，无注册和调用。
- `PYTHONPATH=api/src:engine/src /opt/flowork/.venv/bin/python -m pytest --noconftest -q api/tests/services/test_agent_runtime_capabilities.py --tb=short`：**7 passed**。
- 这些测试使用独立对象和 monkeypatch，不依赖数据库。首次按默认 fixture 执行时，测试 PostgreSQL 因 root 身份不能初始化而在 setup 阶段失败；独立运行明确使用 `--noconftest`，没有把它表述为数据库集成测试通过。
- 对修改的 `capabilities.py` 运行 Ruff：通过；`git diff --check`：通过。
- 未运行全量测试、生产压测、浏览器多窗口故障注入或集群验收。这些属于各优化项实施后的验收要求。


## 授权后的优化与实机验收（2026-10-10）

本批优先实施有明确代码证据、无需新增服务的改动。保留 POSIX、数据库事件和合理轮询，不增加业务自动重试。

| 审查项 | 本批完成内容 | 边界/剩余工作 |
| --- | --- | --- |
| A1 对话恢复 | Chat 复用资源变化刷新器；读取失败保留待刷新状态；只读退避补读；权限终止错误停止；等待旧请求后获取新快照；不主动拉取缓存中的非活跃分页 | 后台执行没有重试；不把这项当作对所有历史 UI 故障的统一修复 |
| A2 原生发布 | 插件构建、前端依赖/构建准备、包发布全部移到 stop 之前；新增 native `prepare-web` 入口；up 复用已准备资源 | 这是构建失败隔离，不是完整蓝绿发布；启动失败自动回退、数据库迁移回退没有新增 |
| A4 检索 | 每次搜索复用 query 分词；每 128 块裁剪 Top-K 并让出事件循环；同一文件元信息每次搜索只解密一次 | 最多 20001 块候选仍一次读入；未改变加密存储，未新增明文缓存；分页解密/字节预算可继续单独优化 |
| A6 前端更新 | 选中节点 ID 使用标量订阅；任务列表运行中 5 秒、空闲 30 秒、后台不轮询、回到窗口刷新 | 只优化已定位的订阅和列表，不声称整个画布的所有大图性能问题已解决 |
| A9 文档冗余 | 修正入队 helper 的旧补投递描述；修正性能测试残留的 HNSW/200ms 注释 | 不改 DBOS 和业务执行语义 |
| A10 Office 预览 | 固定 32 个进程内分片锁合并同内容并发；等待后重查缓存；发布缓存后才释放锁 | 保留每进程两个转换槽；不是多实例全局转换锁 |
| 冗余清理 | 删除无调用的 `_common.py` 三个 helper，及 capabilities 两个无调用解析 helper | 未删除仍被旧测试引用或职责尚不明确的校验/兼容代码 |

A3 多 sandboxd、A5 模型连接池、A8 大模块拆分、A11 真实指标接入本批未扩大实施。它们分别需要集群运行条件、连接耗时与租户隔离验证、独立回归边界、真实统计入口确认；不能通过一次批量重构宣称完成。A7 先遵循下述容量约束。

### 连接与转换容量约束

数据库预算按所有进程累计：主 SQLAlchemy 池默认每进程 `5 + 5`，再加 DBOS 客户端/worker 自己的池、实际使用的 LISTEN 频道连接、维护任务和运维余量。监听连接按事件循环/频道共享，不能按浏览器窗口数直接相乘。扩 API 实例前核对 PostgreSQL 连接上限和实测池等待；LISTEN 仍需直连或 session pooling。Office 的两个转换槽同样按进程累加，多实例前要按内存预算配置实例数。上述是容量计算规则，本批没有声称完成多实例压力测试。

### 测试证据

- 后端定向测试：36 passed（原生启动编排失败隔离、Office 并发/失败释放、Top-K 一致性与事件循环让出、模型能力目录）。
- 真实临时 PostgreSQL：10 passed，包括加密知识库读取/软删除、同文件元信息只解密一次和 10000 块性能用例。测试以非 root 用户运行，不访问线上数据库。
- 画布/编辑器与 active-turn 相关测试：81 passed。
- Chat/SSE 定向回归：31 passed；包含没有后续通知的补读失败、旧请求、非活跃分页、权限拒绝、停止订阅。
- 完整 TypeScript 检查通过。第一次默认 Node 约 1 GB 堆上限触发 OOM；单独用 1536 MB 堆完成检查。未将机器内存错误当作代码通过。
- Vite 构建、部署路径检查、Ruff 检查通过。Vite 仍报告部分 chunk 超过 500 kB 的提示，未把它隐藏或声称已经解决。

### Chromium + 真实 Terra 双窗口

使用本地构建的真实 Web UI，浏览器仅替换静态资源，API/数据库/Agent 来自 `flowork.top`。因此这证明新 UI 对真实服务的兼容与恢复行为，**不代表线上已部署新 UI**。

项目：`QA Architecture review 2026-10-10`；首轮验收 Chat：`chat_5916f4a4cdd04667abe0fb5874f2d059`。

1. 两个独立浏览器 context 打开同一新 Chat，使用 GPT-5.6-Terra。
2. 观察窗口注入 active-runs/历史 GET 的 503，共 7 次失败；发送窗口完成后解除故障，期间没有发送新消息。
3. 观察窗口补齐第一轮回复并回到完成状态。
4. 第二轮让 Terra 先回复、调用 shell 等待 8 秒、再回复；观察窗口在执行中刷新。
5. 两个窗口最终都显示两条用户消息和完整两轮回复，没有重复用户消息、历史消失或残留停止按钮。截图目视检查通过。
6. 只释放本次 QA 项目的沙盒，返回 HTTP 200；对话保留用于复查。

随后用仓库内可重复脚本再次独立验收：Chat `chat_3fddd80f86ab45119b03c7d0d7a6dd38`，观察窗口 5 次读取失败后恢复；两窗口均完整保留两轮用户消息与 Assistant 回复，执行中刷新通过，沙盒释放返回 200。权限撤销后停止租约定时器由定向回归测试覆盖；这项没有宣称在真实账号上撤销授权实测。

可重复入口：[scripts/acceptance/chat-read-recovery.cjs](../scripts/acceptance/chat-read-recovery.cjs)。先构建 Web，再设置 `FLOWORK_BASE_URL`、`FLOWORK_STORAGE_STATE`（测试账号 Playwright storageState 文件）、`FLOWORK_REVIEW_OUTPUT_DIR` 后运行 `node scripts/acceptance/chat-read-recovery.cjs`；可通过 `FLOWORK_CHROMIUM_EXECUTABLE` 指定本机 Chromium。会创建 QA 项目、调用两轮真实 Terra 并释放它的沙盒，保留对话证据；不要提交认证文件。

### 真实 Office 与检索基准

真实 LibreOffice：同时请求相同 DOCX 五次，只启动一次转换，总耗时约 3.04 秒，五份 PDF 字节一致（11780 字节）。这项使用新 Python 源码和实际 LibreOffice 进程，不是替代进程的 mock。

检索排序微基准使用相同确定性中英文语料，比对基线 `0e6f3c4` 与新代码；开启 tracemalloc 会显著增加耗时，数据不代表线上端到端吞吐：

| 块数 | 旧排序时间 / 新排序时间 | 旧额外分配峰值 / 新额外分配峰值 | 输出 |
| --- | --- | --- | --- |
| 1000 | 0.799s / 0.669s | 1.94 MB / 0.187 MB | 一致 |
| 10000 | 7.345s / 6.792s | 19.38 MB / 0.187 MB | 一致 |
| 20000 | 15.110s / 13.146s | 38.76 MB / 0.187 MB | 一致 |

上表不包括候选语料本身、SQL 和解密所需内存；不能表述为总 RSS 下降到 0.187 MB。同期协程在旧同步排序期间未获得执行机会，新版本分别获得 8/79/157 次执行机会。另一个真实加密 PostgreSQL 10000 块端到端用例耗时 1819.9 ms，包含数据库读取、解密和排序。
