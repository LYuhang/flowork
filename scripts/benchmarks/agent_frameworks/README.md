# Agent framework microbenchmark

These standalone scripts never invoke a real model, read Flowork credentials,
connect to the database, or change the production runtime. They compare:

- `langchain`: vanilla LangChain `create_agent` / LangGraph.
- `flowork`: the repository's actual `run_bounded_agent`, with synthetic tools.
- `pydantic`: Pydantic AI slim with `ToolOutput(name="set_output")`.
- `agents`: OpenAI Agents SDK, stopping on the `set_output` function tool.
- `smolagents`: `ToolCallingAgent` / `OpenAIServerModel` in `asyncio.to_thread`.
- `smolagents16`: the same synchronous runner with a dedicated pool sized to the
  maximum tested concurrency (16 by default). This separates framework overhead
  from Python's default executor size (6 on the tested 2-core host).

## Reproduce

Use Python 3.11 on Linux (`/proc` provides RSS/PSS). The baseline interpreter must
have the repository's runtime dependencies. Create a **separate** virtualenv for
the candidate requirements. The tested candidate stack uses OpenAI SDK 3, while
the Flowork baseline uses SDK 2; they cannot share one dependency environment.

```bash
python3.11 -m venv /tmp/flowork-agent-benchmark
/tmp/flowork-agent-benchmark/bin/python -m pip install -r scripts/benchmarks/agent_frameworks/requirements-candidates.txt

/path/to/flowork/venv/bin/python scripts/benchmarks/agent_frameworks/run.py \
  --baseline-python /path/to/flowork/venv/bin/python \
  --candidate-python /tmp/flowork-agent-benchmark/bin/python \
  --output /tmp/agent-benchmark-results

python3 scripts/benchmarks/agent_frameworks/report.py \
  /tmp/agent-benchmark-results /tmp/agent-benchmark-results/aggregate.json
```

The output directory must not exist. A failed worker is retained, not retried or
excluded silently. The runner launches one benchmark worker at a time, randomizes
case order with a fixed seed, samples its RSS/PSS every 25 ms, and stops admission
when available host RAM is below 250 MiB or free disk below 64 MiB. Individual
workers have a 350 MiB sampled RSS guard and 180 second timeout. These are benchmark
guards, not production concurrency controls.

For a smoke test add `--repetitions 1 --phases 1:1,4:4 --warmup 1`.

## Workload and fairness

- Three independent processes per framework/scenario; three warmup calls each.
- Each measured call: model → `lookup(n)` → model → `scale(n+1)` → model → final
  output. Exactly three model requests, two business tool executions and the
  expected `{answer: 2*(n+1), case_id: n}` are checked.
- The local Chat Completions fixture waits 100 ms per response. Each tool waits
  10 ms. No streaming, planning, output correction, provider retry or hosted
  telemetry is enabled. `smolagents` uses its `final_answer` name and observation
  message format; the fixture accepts this encoding without skipping tool work.
- `local` uses Python tools. `broker_http` uses real loopback HTTP JSON requests,
  approximating Flowork's worker-to-host MCP broker boundary. **This is not a
  native MCP protocol/client-adapter benchmark**, and excludes broker auth,
  discovery, upstream MCP service latency, image payloads and external networks.
- Every framework constructs a fresh agent/run state per invocation but shares
  a model client per worker. Tools and mutable run state do not cross executions.
  This is a potential resident-worker design, **not today's complete Flowork
  sandbox architecture**, which also owns Code processes, files and persistence.
- Concurrency phases are 1/4/16 with 16/32/64 calls. P95 is service latency after
  semaphore admission (queue wait excluded); QPS uses total phase wall time.
- CPU time and RSS/PSS cover the worker, excluding the fixture server, supervisor
  and production services. The server still shares the same host CPU. Three
  repetitions and QPS ranges expose, but do not eliminate, host scheduling noise.
- Warm memory is sampled after warmup; phase-end, post-GC, post-close and lifetime
  peak are separate. Do not interpret allocator high-water marks as proof of a
  leak, or compare these process RSS numbers directly to sandbox cgroup memory.
- Initialization time starts after the harness's standard-library imports and
  ends after framework/model/tool setup. It excludes interpreter bootstrap and
  the first agent invocation; it is not end-to-end cold request latency.
- Cancellation interrupts the first model wait, then observes tools for 700 ms.
  A cancelled Python await is not proof that a wrapped synchronous runner stopped.
  This does not test every cancellation point or hard-kill behavior.
- Package versions are recorded in each result. Candidate runtime differences
  include their SDK versions, so results compare usable stacks, not a perfectly
  isolated change to one framework package. Installed but unimported packages do
  not count as resident runtime dependencies.

Results characterize small structured tool loops. They do not establish model
quality, full provider/MCP compatibility, long-running memory stability, or
production deployment QPS. A migration also needs Flowork trace, image, Skill,
MCP capability, output-validation and cancellation acceptance.

## Official references

- https://pydantic.dev/docs/ai/overview/install/
- https://pydantic.dev/docs/ai/core-concepts/output/
- https://developers.openai.com/api/docs/guides/agents/sdk
- https://developers.openai.com/api/docs/guides/agents/models
- https://huggingface.co/docs/smolagents/reference/agents

## 2026-10-04 实测结果

环境：当前 2 核、约 2 GiB RAM 的服务器；每次只运行一个基准 worker，生产服务仍运行。主批次随机顺序执行 30 个进程；发现默认线程池容量限制后，追加 6 个 smolagents 16 线程进程，追加批次内同样随机排序。不同批次的宿主机调度差异仍可能影响吞吐。

36 个进程、每种配置 3 次重复，测量调用共 **4,032/4,032 成功**，每次均核对 3 次模型请求、2 次业务工具和最终结果；预热及取消探测不计入此成功率。smolagents 的“取消 await 后仍继续工具执行”是独立发现，不能被成功率掩盖。

下表选取模拟 HTTP broker 场景，数值为三次重复的中位数；完整的本地工具、各并发档位、版本和逐轮结果保存在 [原始结果](results/2026-10-04.json)。RSS 是独立基准进程内存，不是整个沙盒用量。

| 实现 | 预热 RSS MiB | 进程峰值 RSS MiB | 冷初始化秒 | 16 并发 QPS | QPS 范围 | P95 毫秒 |
|---|---:|---:|---:|---:|---:|---:|
| Flowork 当前 SubAgent 核心 | 110.3 | 114.7 | 3.07 | 16.95 | 16.78–17.30 | 1190 |
| 原生 LangChain / LangGraph | 107.7 | 111.3 | 3.48 | 17.35 | 15.88–17.85 | 1118 |
| Pydantic AI slim | 81.5 | 86.7 | 1.93 | 19.33 | 18.93–20.66 | 1001 |
| OpenAI Agents SDK | 94.7 | 96.6 | 2.84 | 23.03 | 19.40–24.32 | 902 |
| smolagents 默认线程池（6） | 78.8 | 81.9 | 1.57 | 12.78 | 12.67–12.94 | 1363 |
| smolagents 独立线程池（16） | 78.8 | 85.8 | 1.62 | 16.20 | 15.96–16.35 | 1173 |

版本：LangChain 1.3.14、LangGraph 1.2.10、langchain-openai 1.3.3、基线 OpenAI SDK 2.36.0；Pydantic AI slim 2.54.0、Agents SDK 0.23.1、smolagents 1.26.0、候选 OpenAI SDK 3.24.0。生产依赖未更新。为减少服务器磁盘占用，本次候选 virtualenv 通过只读 site-packages 引用复用已有公共依赖，并在自己的目录安装新增/升级包；没有在候选解释器运行与 SDK 3 不兼容的 LangChain 基线。复现可使用上面的独立完整虚拟环境。

判断与限制：

- 相对 Flowork 当前核心，Pydantic AI 预热 RSS 低约 26%，该场景 16 并发吞吐中位数高约 14%；适合作为内存优先的下一轮候选。
- Agents SDK 预热 RSS 低约 14%，该场景吞吐中位数高约 36%；本轮 CPU 开销也较低，但与其它框架的 QPS 范围有重叠，不能推广为所有模型和任务都最快。
- smolagents 内存较低。默认线程池限制吞吐；补至 16 线程后约 16.20 QPS。两种线程接法在取消探测中均继续执行了两次业务工具。这里只证明该接法不能用取消 await 停止同步执行，不声称框架无法增加协作取消机制。
- 当前 Flowork 核心在同一进程运行 16 个异步 Agent 任务时，进程峰值约 114.7 MiB，没有按调用数复制整份框架依赖。因此减少执行进程数量、复用依赖仍是重要优化方向；这不等于完整 Workflow 的文件目录、超时和 Code 节点已经支持安全共享 worker。
- 候选框架本次都没有接入真实 Workflow 节点。未完成真实 MCP 协议适配、图片、Skills、复杂输出校验、供应商兼容和所有取消时机的验证。暂不据此替换生产框架。

汇总本次主批次及追加批次的命令：

```bash
python3 scripts/benchmarks/agent_frameworks/report.py /path/to/full-1 /tmp/aggregate.json \
  --additional-root /path/to/smolagents16-full
```
