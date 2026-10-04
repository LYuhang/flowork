"""One fresh process per framework/scenario/repetition; fake local model only."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
import gc
import importlib.metadata
import json
import os
from pathlib import Path
import resource
import statistics
import sys
import time

START = time.perf_counter()
PACKAGES = ["langchain", "langgraph", "langchain-openai", "openai", "pydantic",
            "pydantic-ai-slim", "pydantic-graph", "openai-agents", "smolagents", "mcp", "httpx", "httpx2"]


def memory():
    values = {}
    for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            if key in {"Rss", "Pss", "Private_Clean", "Private_Dirty", "SwapPss"}:
                values[key + "_mib"] = round(int(value.split()[0]) / 1024, 3)
    values["threads"] = len(list(Path("/proc/self/task").iterdir()))
    values["fds"] = len(list(Path("/proc/self/fd").iterdir()))
    return values


def cpu():
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def percentile(values, p):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(len(ordered) * p + .999999) - 1))]


async def main(args):
    output = {"framework": args.framework, "scenario": args.scenario,
              "python": sys.version.split()[0], "pid": os.getpid(), "baseline": memory(),
              "versions": {}, "phases": [], "concurrency_mode": "asyncio"}
    for name in PACKAGES:
        try:
            output["versions"][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    import httpx
    from pydantic import BaseModel

    class Output(BaseModel):
        answer: int
        case_id: int

    calls = []
    loop = asyncio.get_running_loop()
    async with AsyncExitStack() as stack:
        tool_client = await stack.enter_async_context(httpx.AsyncClient(base_url=args.url, trust_env=False))

        async def call_tool(name, value):
            calls.append((name, value))
            if args.scenario == "broker_http":
                response = await tool_client.post("/tool", json={"name": name, "value": value})
                response.raise_for_status()
                return response.json()["value"]
            await asyncio.sleep(args.tool_delay)
            return value + 1 if name == "lookup" else value * 2

        async def lookup(value: int) -> int:
            """Look up a value and add one."""
            return await call_tool("lookup", value)

        async def scale(value: int) -> int:
            """Multiply the value by two."""
            return await call_tool("scale", value)

        async def set_output(answer: int, case_id: int) -> str:
            """Submit the final structured result and end execution."""
            return Output(answer=answer, case_id=case_id).model_dump_json()

        prompt = "Call lookup, then scale, then submit the structured final result."
        if args.framework in {"langchain", "flowork"}:
            from langchain_openai import ChatOpenAI
            from langchain_core.tools import StructuredTool
            # Both transports are explicitly owned, as in Flowork's lifecycle fix.
            sync = stack.enter_context(httpx.Client(trust_env=False))
            transport = await stack.enter_async_context(httpx.AsyncClient(trust_env=False))
            model = ChatOpenAI(model="benchmark-model", api_key="benchmark-local-only",
                               base_url=args.url + "/v1", max_retries=0, timeout=30,
                               http_client=sync, http_async_client=transport,
                               use_responses_api=False)
            tools = [StructuredTool.from_function(coroutine=lookup), StructuredTool.from_function(coroutine=scale)]
            if args.framework == "langchain":
                from langchain.agents import create_agent
                final = StructuredTool.from_function(coroutine=set_output, return_direct=True)

                async def invoke(case_id):
                    agent = create_agent(model, tools=[*tools, final], system_prompt=prompt)
                    result = await agent.ainvoke({"messages": [{"role": "user", "content": f"case_id={case_id}"}]})
                    return json.loads(result["messages"][-1].content)
            else:
                from vibecanvas_api.agents.tools.subagent.core import run_bounded_agent

                async def invoke(case_id):
                    result = await run_bounded_agent(model=model, tools=tools, system_prompt=prompt,
                        user_input=f"case_id={case_id}", output_fields={"answer": {"type": "integer"}, "case_id": {"type": "integer"}},
                        max_iterations=8)
                    if result.status != "done":
                        raise RuntimeError(result.error)
                    return result.output
        elif args.framework in {"pydantic", "agents"}:
            from openai import AsyncOpenAI
            client = await stack.enter_async_context(AsyncOpenAI(api_key="benchmark-local-only", base_url=args.url + "/v1", max_retries=0, timeout=30))
            if args.framework == "pydantic":
                from pydantic_ai import Agent, ToolOutput
                from pydantic_ai.models.openai import OpenAIChatModel
                from pydantic_ai.providers.openai import OpenAIProvider
                model = OpenAIChatModel("benchmark-model", provider=OpenAIProvider(openai_client=client))

                async def invoke(case_id):
                    agent = Agent(model, tools=[lookup, scale], instructions=prompt,
                                  output_type=ToolOutput(Output, name="set_output", max_retries=0), retries=0)
                    result = await agent.run(f"case_id={case_id}")
                    return result.output.model_dump()
            else:
                from agents import Agent, Runner, OpenAIChatCompletionsModel, function_tool, ModelSettings, set_tracing_disabled
                set_tracing_disabled(True)  # No remote telemetry; measure local execution only.
                model = OpenAIChatCompletionsModel("benchmark-model", client)
                tools = [function_tool(f) for f in (lookup, scale, set_output)]

                async def invoke(case_id):
                    agent = Agent(name="benchmark", model=model, instructions=prompt, tools=tools,
                                  model_settings=ModelSettings(parallel_tool_calls=False),
                                  tool_use_behavior={"stop_at_tool_names": ["set_output"]})
                    result = await Runner.run(agent, f"case_id={case_id}", max_turns=8)
                    value = result.final_output
                    return json.loads(value) if isinstance(value, str) else value
        else:
            from smolagents import ToolCallingAgent, OpenAIServerModel, Tool
            output["concurrency_mode"] = "asyncio.to_thread (synchronous agent runner)"
            executor = None
            if args.framework == "smolagents16":
                from concurrent.futures import ThreadPoolExecutor
                size = max(c for c, _ in args.phases)
                executor = stack.enter_context(ThreadPoolExecutor(max_workers=size))
                output["concurrency_mode"] = f"dedicated ThreadPoolExecutor({size}); synchronous agent runner"
            model = OpenAIServerModel(model_id="benchmark-model", api_base=args.url + "/v1",
                                      api_key="benchmark-local-only", client_kwargs={"max_retries": 0, "timeout": 30})
            stack.callback(model.client.close)

            class Lookup(Tool):
                name = "lookup"
                description = "Look up a value and add one."
                inputs = {"value": {"type": "integer", "description": "Input value"}}
                output_type = "integer"

                def forward(self, value: int) -> int:
                    return asyncio.run_coroutine_threadsafe(call_tool(self.name, value), loop).result(timeout=30)

            class Scale(Lookup):
                name = "scale"
                description = "Multiply the value by two."

            def sync_invoke(case_id):
                agent = ToolCallingAgent(tools=[Lookup(), Scale()], model=model, max_steps=8, verbosity_level=-1)
                return agent.run(f"{prompt} case_id={case_id}")

            async def invoke(case_id):
                if executor is not None:
                    return await loop.run_in_executor(executor, sync_invoke, case_id)
                return await asyncio.to_thread(sync_invoke, case_id)

        output["startup_seconds"] = round(time.perf_counter() - START, 4)
        output["initialized"] = memory()
        for i in range(args.warmup):
            result = await invoke(100000 + i)
            assert result == {"answer": 2 * (100001 + i), "case_id": 100000 + i}, result
        output["warm"] = memory()

        for concurrency, count in args.phases:
            await tool_client.delete("/stats")
            calls.clear()
            sem = asyncio.Semaphore(concurrency)
            latencies, errors = [], []
            started, cpu_before = time.perf_counter(), cpu()
            phase = {"concurrency": concurrency, "count": count, "before": memory()}

            async def one(index):
                async with sem:
                    t = time.perf_counter()
                    try:
                        result = await invoke(index)
                        assert result == {"answer": 2 * (index + 1), "case_id": index}, result
                    except Exception as exc:
                        errors.append({"index": index, "type": type(exc).__name__, "message": str(exc)[:600]})
                    latencies.append(time.perf_counter() - t)

            await asyncio.gather(*(one(i) for i in range(count)))
            seconds = time.perf_counter() - started
            cpu_seconds = cpu() - cpu_before
            stats = (await tool_client.get("/stats")).json()
            phase.update(seconds=round(seconds, 4), success=count-len(errors), errors=errors,
                         successful_qps=round((count-len(errors))/seconds, 3),
                         p50_ms=round(statistics.median(latencies)*1000, 3),
                         p95_ms=round(percentile(latencies, .95)*1000, 3),
                         cpu_seconds=round(cpu_seconds, 4), cpu_ms_per_call=round(cpu_seconds/count*1000, 3),
                         tool_calls=len(calls), server_stats=stats, after=memory())
            phase["contract_passed"] = (not errors and len(calls) == count*2 and stats["model"] == count*3
                                        and stats["invalid"] == 0 and stats["tool"] == (count*2 if args.scenario == "broker_http" else 0))
            output["phases"].append(phase)
            print(json.dumps({"event": "phase", "framework": args.framework, **phase}), flush=True)
            if not phase["contract_passed"]:
                break

        # Cancel while the first model request is waiting. Do not mistake
        # cancelling an asyncio wrapper for actually stopping a sync worker.
        calls.clear()
        task = asyncio.create_task(invoke(999999))
        await asyncio.sleep(.05)
        t = time.perf_counter()
        task.cancel()
        try:
            await task
            cancelled = False
        except asyncio.CancelledError:
            cancelled = True
        await asyncio.sleep(.7)
        output["cancellation"] = {"cancelled_await": cancelled, "observation_seconds": round(time.perf_counter()-t, 3),
                                  "tools_executed_after_cancel": len(calls)}
        output["before_gc"] = memory()
        gc.collect()
        output["after_gc"] = memory()
    gc.collect()
    output["after_close"] = memory()
    output["lifetime_peak_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    output["total_seconds"] = round(time.perf_counter() - START, 4)
    Path(args.result).write_text(json.dumps(output, indent=2))
    print(json.dumps({"event": "done", "framework": args.framework, "result": args.result}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--framework", choices=["langchain", "flowork", "pydantic", "agents", "smolagents", "smolagents16"], required=True)
    parser.add_argument("--scenario", choices=["local", "broker_http"], required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--tool-delay", type=float, default=.01)
    parser.add_argument("--phases", default="1:16,4:32,16:64")
    args = parser.parse_args()
    args.phases = [tuple(map(int, item.split(":"))) for item in args.phases.split(",")]
    asyncio.run(main(args))
