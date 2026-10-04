"""Behavioral tests for in-memory workflow ownership and human decisions."""

import asyncio
from copy import deepcopy
import uuid

import pytest

from vibecanvas_engine.runtime.executions import WorkflowRuntime, ExecutionConflict, ExecutionCapacityError
from vibecanvas_engine.workflow import Workflow


def approval_workflow():
    def node(id, name, kind, inputs, outputs, config, children):
        return {
            "node_id": id,
            "node_name": name,
            "node_type": kind,
            "node_description": name,
            "input_fields": inputs,
            "output_fields": outputs,
            "node_config": config,
            "children": children,
        }

    return {
        "node_1": node("node_1", "__start__", "StartNode", {}, {}, {}, ["node_2"]),
        "node_2": node(
            "node_2",
            "review",
            "HumanApprovalNode",
            {},
            {"approved": {"type": "boolean", "description": "Decision"}},
            {"instruction": "Review", "timeout_seconds": 1},
            ["node_3"],
        ),
        "node_3": node(
            "node_3",
            "__end__",
            "EndNode",
            {"approved": {"type": "boolean", "value": False, "reference": "review.approved"}},
            {"approved": {"type": "boolean", "description": "Decision"}},
            {},
            [],
        ),
    }


async def wait_for(runtime, run, predicate):
    async def poll():
        while True:
            state = await runtime.events(run, after=0, wait_seconds=0)
            if predicate(state):
                return state
            await asyncio.sleep(0.005)

    return await asyncio.wait_for(poll(), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [True, False, "timeout", "cancel"])
async def test_standalone_approval_runs_without_upstream_or_downstream(decision):
    rt = WorkflowRuntime(capacity=1)
    # Keep a downstream child reference: standalone execution must ignore it.
    node = approval_workflow()["node_2"]
    rt.install("node", {"node_2": node}, node_id="node_2")
    run = str(uuid.uuid4())
    try:
        rt.invoke(run, "node", {"evidence": "only this input"})
        state = await wait_for(rt, run, lambda s: s["status"] == "waiting_approval")
        if decision == "cancel":
            await rt.cancel(run)
        elif decision != "timeout":
            await rt.decide(run, state["approvals"][0]["approval_id"], decision)
        state = await wait_for(rt, run, lambda s: s["status"] in rt.TERMINAL)
        assert state["status"] == ("cancelled" if decision == "cancel" else "timed_out" if decision == "timeout" else "succeeded")
        frames = state["events"]
        assert {e["node_id"] for e in frames if e["type"] == "node_event"} == {"node_2"}
        assert frames[0]["inputs"] == {"evidence": "only this input"}
        if decision == "timeout":
            assert frames[-1]["error_code"] == "approval_timeout"
            assert frames[-1]["final_outputs"] == {}
        elif decision != "cancel":
            assert frames[-1]["final_outputs"] == {"node_2": {"approved": decision is True}}
            assert not frames[-1]["error_dict"]
    finally:
        await rt.close()


def test_standalone_runtime_rejects_extra_nodes_and_revision_mode_changes():
    rt = WorkflowRuntime(capacity=1)
    with pytest.raises(ValueError, match="exactly the selected node"):
        rt.install("node", approval_workflow(), node_id="node_2")
    graph = {"node_2": approval_workflow()["node_2"]}
    rt.install("node", graph, node_id="node_2")
    with pytest.raises(ExecutionConflict):
        rt.install("node", graph)


@pytest.mark.asyncio
async def test_standalone_node_preserves_defaults_and_ignores_references():
    from vibecanvas_engine.nodes.template import TemplateNode

    node = deepcopy(TemplateNode.AGENT_SPEC["examples"][0]["node_dict"])
    node.update(node_id="node_2", children=["node_3"])
    node["input_fields"] = {
        "name": {"type": "string", "value": "default", "reference": "missing.output"},
        "suffix": {"type": "string", "value": "!", "reference": ""},
    }
    node["node_config"] = {"template": "{{ name }}{{ suffix }}", "output_format": "text"}
    rt = WorkflowRuntime(capacity=1)
    rt.install("node", {"node_2": node}, node_id="node_2")
    try:
        for inputs, expected in [({}, "default!"), ({"name": "override"}, "override!")]:
            run = str(uuid.uuid4())
            rt.invoke(run, "node", inputs)
            state = await wait_for(rt, run, lambda s: s["status"] in rt.TERMINAL)
            assert state["status"] == "succeeded", state
            assert state["events"][-1]["final_outputs"]["node_2"]["rendered"] == expected
            rt.acknowledge(run, through=state["seq"])
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_standalone_code_timeout_stops_worker_before_releasing_capacity():
    from vibecanvas_engine.nodes.code import CodeNode

    node = deepcopy(CodeNode.AGENT_SPEC["examples"][0]["node_dict"])
    node.update(node_id="node_2", children=[], input_fields={})
    node["node_config"] = {
        "programming_language": "python",
        "process_fn": "def process_fn(inputs):\n    import time\n    time.sleep(10)\n    return {}",
    }
    rt = WorkflowRuntime(capacity=1)
    rt.install(
        "node",
        {
            "node_2": node,
            "__meta__": {"settings": {"timeouts": {"workflow": 0.1}}},
        },
        node_id="node_2",
    )
    run = str(uuid.uuid4())
    try:
        rt.invoke(run, "node", {})
        state = await wait_for(rt, run, lambda s: s["status"] in rt.TERMINAL)
        assert state["status"] == "timed_out"
        assert state["events"][-1]["error_dict"]["__engine__"] == "execution_timed_out"
        assert rt.executions[run].task.done()
        rt.acknowledge(run, through=state["seq"])
        next_run = str(uuid.uuid4())
        rt.invoke(next_run, "node", {})
        await rt.cancel(next_run)
        assert rt.status(next_run)["status"] == "cancelled"
    finally:
        await rt.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [True, False])
async def test_decision_continues_without_repeating_start(decision):
    rt = WorkflowRuntime(capacity=1)
    rt.install("v1", approval_workflow())
    run = str(uuid.uuid4())
    try:
        rt.invoke(run, "v1", {})
        state = await wait_for(rt, run, lambda s: s["status"] == "waiting_approval")
        approval = state["approvals"][0]["approval_id"]
        await rt.decide(run, approval, decision)
        state = await wait_for(rt, run, lambda s: s["status"] == "succeeded")
        result = state["events"][-1]
        assert result["final_outputs"]["__end__"] == {"approved": decision}
        assert (
            len([ev for ev in state["events"] if ev.get("node_id") == "node_1" and ev.get("status") == "running"]) == 1
        )
        assert not result["error_dict"]
    finally:
        await rt.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("require_resume", [False, True])
async def test_timeout_stops_without_a_decision_or_downstream(require_resume):
    rt = WorkflowRuntime(capacity=1)
    rt.install("v1", approval_workflow())
    run = str(uuid.uuid4())
    try:
        rt.invoke(run, "v1", {}, require_approval_resume=require_resume)
        state = await wait_for(rt, run, lambda s: s["status"] in rt.TERMINAL)
        assert state["status"] == "timed_out"
        assert state["events"][-1]["error_code"] == "approval_timeout"
        assert state["events"][-1]["final_outputs"] == {}
        resolution = next(e for e in state["events"] if e["type"] == "approval_resolved")
        assert resolution["reason"] == "timeout" and resolution["approved"] is None
        assert not any(e["type"] == "approval_ready" or e.get("node_id") == "node_3" for e in state["events"])
        from vibecanvas_engine.runtime.approvals import ApprovalConflict
        with pytest.raises(ApprovalConflict):
            await rt.executions[run].approvals.decide(resolution["approval_id"], True)
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_approval_wait_does_not_consume_the_execution_budget():
    rt = WorkflowRuntime(capacity=1)
    wf = approval_workflow()
    wf["__meta__"] = {"settings": {"timeouts": {"workflow": 0.1}}}
    rt.install("v1", wf)
    run = str(uuid.uuid4())
    try:
        rt.invoke(run, "v1", {})
        await wait_for(rt, run, lambda s: s["status"] == "waiting_approval")
        await asyncio.sleep(0.2)
        assert rt.status(run)["status"] == "waiting_approval"
        state = await wait_for(rt, run, lambda s: s["status"] in rt.TERMINAL)
        assert state["status"] == "timed_out"
        assert state["events"][-1]["error_code"] == "approval_timeout"
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_waiting_consumes_capacity_cancel_does_not_reject():
    rt = WorkflowRuntime(capacity=1)
    rt.install("v1", approval_workflow())
    run = str(uuid.uuid4())
    try:
        rt.invoke(run, "v1", {})
        await wait_for(rt, run, lambda s: s["status"] == "waiting_approval")
        with pytest.raises(ExecutionCapacityError):
            rt.invoke(str(uuid.uuid4()), "v1", {})
        assert (await asyncio.wait_for(rt.cancel(run), 1))["status"] == "cancelled"
        state = await rt.events(run)
        assert not any(e.get("type") == "approval_resolved" for e in state["events"])
        assert "__end__" not in state["events"][-1]["final_outputs"]
        assert rt.invoke(str(uuid.uuid4()), "v1", {})["status"] == "running"
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_replay_and_ack_never_restart_execution():
    rt = WorkflowRuntime(capacity=1)
    wf = approval_workflow()
    rt.install("v1", wf)
    run = str(uuid.uuid4())
    try:
        rt.invoke(run, "v1", {})
        state = await wait_for(rt, run, lambda s: s["status"] == "waiting_approval")
        first_approval = state["approvals"][0]["approval_id"]
        assert rt.invoke(run, "v1", {})["approvals"][0]["approval_id"] == first_approval
        with pytest.raises(ExecutionConflict):
            rt.invoke(run, "v1", {"different": True})
        await rt.decide(run, first_approval, True)
        state = await wait_for(rt, run, lambda s: s["status"] == "succeeded")
        rt.acknowledge(run, state["seq"])
        assert run not in rt.executions
        assert rt.invoke(run, "v1", {})["status"] == "succeeded"
        changed = deepcopy(wf)
        changed["node_2"]["node_config"]["instruction"] = "different"
        with pytest.raises(ExecutionConflict):
            rt.install("v1", changed)
    finally:
        await rt.close()


def test_schema_requires_single_boolean_output():
    wf = approval_workflow()
    assert Workflow.check(wf)["status"] == "success"
    wf["node_2"]["output_fields"]["other"] = {"type": "string"}
    assert Workflow.check(wf)["status"] == "error"


@pytest.mark.asyncio
async def test_two_executions_have_independent_approvals_and_outputs():
    rt = WorkflowRuntime(capacity=2)
    rt.install("v1", approval_workflow())
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    try:
        for run in ids:
            rt.invoke(run, "v1", {})
        waiting = [await wait_for(rt, run, lambda s: s["status"] == "waiting_approval") for run in ids]
        approval_ids = [s["approvals"][0]["approval_id"] for s in waiting]
        assert approval_ids[0] != approval_ids[1]
        await rt.decide(ids[0], approval_ids[0], True)
        assert rt.status(ids[1])["status"] == "waiting_approval"
        await rt.decide(ids[1], approval_ids[1], False)
        for run, expected in zip(ids, [True, False]):
            state = await wait_for(rt, run, lambda s: s["status"] == "succeeded")
            assert state["events"][-1]["final_outputs"]["__end__"]["approved"] is expected
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_rpc_authentication_generation_and_disconnected_request(tmp_path):
    from vibecanvas_engine.runtime.rpc import RuntimeServer
    from vibecanvas_engine.sandbox_bus import encode_frame, read_frame

    rt = WorkflowRuntime(capacity=1)
    token = "test-control-token-" * 3
    rpc = RuntimeServer(rt, token)
    path = str(tmp_path / "rpc.sock")
    server = await asyncio.start_unix_server(rpc.handle, path)

    async def call(method, args=None, auth=token, generation=None):
        reader, writer = await asyncio.open_unix_connection(path)
        try:
            writer.write(encode_frame({"token": auth, "method": method, "args": args or {}, "generation": generation}))
            await writer.drain()
            return await read_frame(reader)
        finally:
            writer.close()
            await writer.wait_closed()

    run = str(uuid.uuid4())
    try:
        assert (await call("hello", auth="bad"))["error"] == "unauthorized"
        assert (await call("hello", generation="old"))["error"] == "execution_lost"
        assert (await call("install", {"revision": "v1", "workflow": approval_workflow()}))["ok"]
        assert (await call("invoke", {"invocation_id": run, "revision": "v1", "inputs": {}}))["ok"]
        # All HTTP/RPC connections have closed, but the execution is still alive.
        state = await wait_for(rt, run, lambda s: s["status"] == "waiting_approval")
        assert (
            await call(
                "decide", {"invocation_id": run, "approval_id": state["approvals"][0]["approval_id"], "approved": True}
            )
        )["ok"]
        await wait_for(rt, run, lambda s: s["status"] == "succeeded")
        assert not list(tmp_path.glob("*.json"))
        assert not list(tmp_path.glob("**/__exec__"))
    finally:
        server.close()
        await server.wait_closed()
        await rpc.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [True, False])
async def test_approval_gate_waits_for_host_refresh_before_downstream(decision, monkeypatch):
    import importlib

    node_exec = importlib.import_module("vibecanvas_engine.nodes.trigger")
    downstream_contexts = []
    dispatch = node_exec.dispatch_node_call

    async def observe(node, inputs, previous_outputs, extra=None):
        if node.node_type == "EndNode":
            downstream_contexts.append(deepcopy(extra["llm_credentials"]))
        return await dispatch(node, inputs, previous_outputs, extra)

    monkeypatch.setattr(node_exec, "dispatch_node_call", observe)
    runtime = WorkflowRuntime(capacity=1)
    runtime.install("gated", approval_workflow())
    run = str(uuid.uuid4())
    try:
        runtime.invoke(run, "gated", {}, {"llm_credentials": {"model": "expired"}}, require_approval_resume=True)
        waiting = await wait_for(runtime, run, lambda state: state["status"] == "waiting_approval")
        approval_id = waiting["approvals"][0]["approval_id"]
        if decision != "timeout":
            await asyncio.wait_for(runtime.decide(run, approval_id, decision), 1)
        ready = await wait_for(runtime, run, lambda state: any(e["type"] == "approval_ready" for e in state["events"]))
        assert ready["status"] == "waiting_approval"
        assert not downstream_contexts
        assert not any(e["type"] == "approval_resolved" for e in ready["events"])
        with pytest.raises(ExecutionCapacityError):
            runtime.invoke(str(uuid.uuid4()), "gated", {})
        credentials = runtime.executions[run].context["llm_credentials"]
        runtime.resume(run, approval_id, {"llm_credentials": {"model": "fresh"}, "workflow_resources": {}})
        # A duplicate control request cannot change the context after opening the gate.
        runtime.resume(run, approval_id, {"llm_credentials": {"model": "stale-retry"}, "workflow_resources": {}})
        assert credentials == {"model": "fresh"}
        state = await wait_for(runtime, run, lambda state: state["status"] in runtime.TERMINAL)
        assert state["status"] == "succeeded"
        assert downstream_contexts == [{"model": "fresh"}]
        assert state["events"][-1]["final_outputs"]["__end__"]["approved"] is (decision is True)
        assert not runtime.executions[run].context
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_approval_gate_cancel_does_not_run_downstream():
    from vibecanvas_engine.runtime.approvals import ApprovalConflict

    runtime = WorkflowRuntime(capacity=1)
    runtime.install("gated", approval_workflow())
    run = str(uuid.uuid4())
    try:
        runtime.invoke(run, "gated", {}, require_approval_resume=True)
        waiting = await wait_for(runtime, run, lambda state: state["status"] == "waiting_approval")
        approval_id = waiting["approvals"][0]["approval_id"]
        with pytest.raises(ApprovalConflict):
            runtime.resume(run, approval_id, {"llm_credentials": {}, "workflow_resources": {}})
        await runtime.decide(run, approval_id, True)
        await runtime.cancel(run)
        final = await wait_for(runtime, run, lambda state: state["status"] in runtime.TERMINAL)
        assert final["status"] == "cancelled"
        assert not any(e.get("node_id") == "node_3" for e in final["events"])
        with pytest.raises(ExecutionConflict):
            runtime.resume(run, approval_id, {"llm_credentials": {}, "workflow_resources": {}})
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("single_node", [False, True])
async def test_error_telemetry_omits_private_context_and_handles_circular_diagnostics(single_node, monkeypatch):
    import importlib
    import json

    target = importlib.import_module(
        "vibecanvas_engine.runtime.single_node" if single_node else "vibecanvas_engine.nodes.trigger"
    )
    dispatch = target.dispatch_node_call
    credential = "secret-runtime-model-capability"

    async def fail(node, inputs, previous_outputs, extra=None):
        if node.node_type == "HumanApprovalNode":
            diagnostic = {
                "status": "error",
                "error_message": f"broker rejected {credential}",
                "args": [inputs, previous_outputs],
                "kwargs": {"extra": extra},
                "traceback": "private stack",
            }
            diagnostic["kwargs"]["cycle"] = diagnostic
            return diagnostic
        return await dispatch(node, inputs, previous_outputs, extra)

    monkeypatch.setattr(target, "dispatch_node_call", fail)
    runtime = WorkflowRuntime(capacity=1)
    workflow = approval_workflow()
    runtime.install(
        "errors", {"node_2": workflow["node_2"]} if single_node else workflow, node_id="node_2" if single_node else None
    )
    run = str(uuid.uuid4())
    try:
        runtime.invoke(run, "errors", {}, {"llm_credentials": {"model": {"api_key": credential}}})
        state = await wait_for(runtime, run, lambda s: any(e["type"] == "result" for e in s["events"]))
        assert state["status"] == "failed"
        encoded = json.dumps(state)
        assert credential not in encoded
        assert "private stack" not in encoded
        assert '"kwargs"' not in encoded and '"args"' not in encoded
        assert "[REDACTED]" in encoded
        assert isinstance(state["events"][-1]["error_dict"]["node_2"], str)
        assert not runtime.executions[run].context
        assert not runtime.executions[run].private_tokens
    finally:
        await runtime.close()


def parallel_review_workflow():
    from vibecanvas_engine.nodes.parallel import ParallelStartNode, ParallelEndNode
    from vibecanvas_engine.nodes.template import TemplateNode

    graph = approval_workflow()
    split = deepcopy(ParallelStartNode.AGENT_SPEC["examples"][0]["node_dict"])
    join = deepcopy(ParallelEndNode.AGENT_SPEC["examples"][0]["node_dict"])
    slow = deepcopy(TemplateNode.AGENT_SPEC["examples"][0]["node_dict"])
    split.update(node_id="node_4", children=["node_2", "node_5"])
    split["node_config"] = {
        "parallel_end_node_id": "node_6",
        "branches": {
            "review": {"branch_description": "review", "next_node_id": "node_2"},
            "work": {"branch_description": "work", "next_node_id": "node_5"},
        },
    }
    join.update(node_id="node_6", children=["node_3"], input_fields={}, output_fields={})
    join["node_config"] = {"parallel_start_node_id": "node_4"}
    slow.update(
        node_id="node_5",
        node_name="slow",
        children=["node_6"],
        input_fields={"name": {"type": "string", "value": "done", "reference": ""}},
        node_config={"template": "{{ name }}", "output_format": "text"},
    )
    graph["node_1"]["children"] = ["node_4"]
    graph["node_2"]["children"] = ["node_6"]
    graph["node_2"]["node_config"]["timeout_seconds"] = 5
    graph.update(node_4=split, node_5=slow, node_6=join)
    graph["__meta__"] = {"settings": {"timeouts": {"workflow": 0.15}}}
    return graph


@pytest.mark.asyncio
@pytest.mark.parametrize("slow_seconds", [0.02, 0.4])
async def test_parallel_work_consumes_budget_while_another_branch_waits(slow_seconds, monkeypatch):
    import importlib

    target = importlib.import_module("vibecanvas_engine.nodes.trigger")
    dispatch = target.dispatch_node_call

    async def slowed(node, inputs, previous_outputs, extra=None):
        if node.node_id == "node_5":
            await asyncio.sleep(slow_seconds)
        return await dispatch(node, inputs, previous_outputs, extra)

    monkeypatch.setattr(target, "dispatch_node_call", slowed)
    runtime = WorkflowRuntime(capacity=1)
    graph = parallel_review_workflow()
    runtime.install("parallel", graph)
    run = str(uuid.uuid4())
    try:
        runtime.invoke(run, "parallel", {})
        state = await wait_for(runtime, run, lambda state: state["status"] == "waiting_approval")
        if slow_seconds > 0.15:
            state = await wait_for(runtime, run, lambda state: state["status"] in runtime.TERMINAL)
            assert state["status"] == "timed_out"
            assert state["events"][-1]["error_dict"]["__engine__"] == "execution_timed_out"
        else:
            await asyncio.sleep(0.3)
            assert runtime.status(run)["status"] == "waiting_approval"
            await runtime.decide(run, state["approvals"][0]["approval_id"], True)
            state = await wait_for(runtime, run, lambda state: state["status"] in runtime.TERMINAL)
            assert state["status"] == "succeeded"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_unlimited_runtime_has_no_hidden_default_or_backlog_concurrency_cap():
    runtime = WorkflowRuntime(capacity=-1)
    graph = approval_workflow()
    graph['node_2']['node_config']['timeout_seconds'] = 60
    runtime.install('unlimited', graph)
    runs = [str(uuid.uuid4()) for _ in range(40)]
    try:
        for run in runs:
            runtime.invoke(run, 'unlimited', {}, {})
        assert len(runtime.executions) == 40
        await wait_for(runtime, runs[-1], lambda state: state['status'] == 'waiting_approval')
        await runtime.cancel(runs[0])
        assert runtime.status(runs[0])['status'] == 'cancelled'
        assert runtime.status(runs[-1])['status'] == 'waiting_approval'
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_approval_timeout_stops_parallel_work_but_not_sibling_invocation(monkeypatch):
    import importlib
    target = importlib.import_module("vibecanvas_engine.nodes.trigger")
    dispatch = target.dispatch_node_call
    branch_started = asyncio.Event()
    branch_stopped = asyncio.Event()
    release_sibling = asyncio.Event()

    async def blocked(node, inputs, previous_outputs, extra=None):
        if node.node_id == "node_5":
            if extra.get("run_id") == b:
                await release_sibling.wait()
            else:
                branch_started.set()
                try:
                    await asyncio.sleep(20)
                finally:
                    branch_stopped.set()
        return await dispatch(node, inputs, previous_outputs, extra)

    monkeypatch.setattr(target, "dispatch_node_call", blocked)
    runtime = WorkflowRuntime(capacity=2)
    graph = parallel_review_workflow()
    graph["node_2"]["node_config"]["timeout_seconds"] = 1
    graph["__meta__"]["settings"]["timeouts"]["workflow"] = 30
    runtime.install("parallel", graph)
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        runtime.invoke(a, "parallel", {}, require_approval_resume=True)
        runtime.invoke(b, "parallel", {})
        waiting = await wait_for(runtime, b, lambda s: s["status"] == "waiting_approval")
        await runtime.decide(b, waiting["approvals"][0]["approval_id"], True)
        await asyncio.wait_for(branch_started.wait(), 2)
        failed = await wait_for(runtime, a, lambda s: s["status"] in runtime.TERMINAL)
        assert failed["status"] == "timed_out" and branch_stopped.is_set()
        assert failed["events"][-1]["error_code"] == "approval_timeout"
        assert not any(e.get("node_id") == "node_3" for e in failed["events"])
        assert runtime.status(b)["status"] == "running"
        assert not runtime.executions[b].stop.is_set()
        release_sibling.set()
        succeeded = await wait_for(runtime, b, lambda s: s["status"] in runtime.TERMINAL)
        assert succeeded["status"] == "succeeded"
    finally:
        await runtime.close()
