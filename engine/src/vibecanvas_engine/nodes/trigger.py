# -*- coding: utf-8 -*-
"""
The core execution dispatcher for BaseNode.trigger().

The dispatcher is an ``async def`` coroutine
attached to BaseNode as an instance method by ``nodes/__init__.py``.

Concurrency model:
  - Sequential linear paths still progress via a ``while`` loop (no extra
    stack frames per node).
  - Parallel branches spawn ``asyncio.Task``s instead of thread-pool jobs.
  - The pending-counter pattern (``_on_trigger_submit`` /
    ``_on_trigger_done``) is retained and counts active coroutines instead
    of active threads; _execute() awaits the done_event before declaring
    the workflow finished.
  - ``thread_lock`` (a plain ``threading.Lock``) is kept for ABI parity
    with the dispatch body. asyncio is single-threaded, so the lock is
    effectively a no-op guard with zero contention — keeping it avoids a
    sprawling diff and preserves the option of running parts of the
    dispatcher in a worker thread later if we ever need to.
"""

import asyncio
import uuid
from copy import deepcopy

from ..utils import scoped_recursive_get, walk_to_scope
from ..resume import CONTROL_NODES, visit_key
from .exec import dispatch_node_call


async def trigger(self, previous_outputs: dict, extra: dict, workflow_inputs: dict = None, *, loop_back: bool = False, parent_span_id: str = None):
    """
    Implement the recursive call logic for a single node and control the logic for jumping between nodes.

    Args mirror the legacy sync trigger; see the original docstring.
    """
    thread_lock = extra.get("thread_lock")

    # Advance a single sequential branch with a loop instead of recursive
    # trigger calls. LoopEnd-to-LoopBegin jumps therefore do not consume stack
    # frames, and long or nested loops cannot hit Python's recursion limit.
    # Parallel and multi-child paths remain recursive; their depth is bounded by
    # the static graph structure.
    current = self
    current_loop_back = loop_back
    # Carry the previous node's span ID forward as the next node's parent. The
    # initial value comes from the caller and may identify a root or join span.
    last_span_id = parent_span_id

    while current is not None:
        # =================================================================
        # 0. Global circuit breaker: cancellation or an existing fatal error.
        # =================================================================
        stop_event = extra.get("stop_event")
        if stop_event is not None and stop_event.is_set():
            return

        with thread_lock:
            if extra.get("error_dict"):
                return

        # Current loop-stack snapshot.
        loop_stack = extra.setdefault("loop_signal", {}).setdefault("loop_stack", [])

        current_span_id = uuid.uuid4().hex
        parent_for_current = last_span_id
        trace_id = extra.get("trace_id")

        def _emit(payload: dict):
            q = extra.get("info_queue")
            if q is None:
                return
            enriched = dict(payload)
            enriched.pop("traceback", None)
            orig_args = enriched.get("args")
            if isinstance(orig_args, (tuple, list)) and len(orig_args) >= 1:
                enriched["args"] = (orig_args[0],)
            enriched.pop("kwargs", None)
            enriched.setdefault("node_id", current.node_id)
            enriched.setdefault("node_name", current.node_name)
            enriched.setdefault("node_type", current.node_type)
            # Snapshot loop_stack WITHOUT each frame's "scope" (the per-iteration
            # scratch dict holds full body outputs — telemetry only needs the
            # begin id/name + iter index, never the payload).
            enriched.setdefault(
                "loop_stack",
                [{k: v for k, v in f.items() if k != "scope"} for f in loop_stack],
            )
            enriched.setdefault("trace_id", trace_id)
            enriched.setdefault("span_id", current_span_id)
            enriched.setdefault("parent_span_id", parent_for_current)
            # info_queue is an asyncio.Queue (non-awaitable put_nowait keeps
            # _emit synchronous so we can call it from inside `with thread_lock`).
            q.put_nowait(enriched)

        # =================================================================
        # Emit the node-started event before input resolution.
        # =================================================================
        # Emitted at the TOP of the while-body — AFTER the stop/error
        # circuit-breaker (so a cancelled / already-errored run does NOT
        # light up a node) but BEFORE input resolution. Placing it here
        # (rather than just before dispatch_node_call) is load-bearing:
        #   * LoopBeginNode / LoopEndNode skip dispatch_node_call entirely,
        #   * an input-resolution error returns before dispatch,
        # and BOTH must still surface a "running" frame so every node lights
        # up in the live stream. The event is non-terminal: ``_trigger_inner``
        # (workflow.py) and ``sandbox_entry._drive`` only branch on
        # ``finished`` / ``error``, so a ``running`` frame is IGNORED by the
        # accumulators and cannot corrupt the final outputs / error_dict.
        _emit({"status": "running", "output": None, "error_message": ""})

        # =================================================================
        # Step 1: resolve the current node's inputs.
        # =================================================================
        inputs = {}
        if current.node_type == "StartNode":
            inputs = workflow_inputs or {}
        else:
            try:
                with thread_lock:
                    for field_name, field_config in current.input_fields.items():
                        ref = field_config.get("reference")
                        if ref:
                            inputs[field_name] = scoped_recursive_get(previous_outputs, loop_stack, ref)
                        else:
                            inputs[field_name] = field_config.get("value")
            except Exception as e:
                err_info = {
                    "status": "error",
                    "output": None,
                    # Preserve the values resolved before the failure.  The
                    # host persists this envelope under __exec__/nodes so the
                    # single-node debugger can reproduce a failed workflow
                    # node without making the user reconstruct its inputs.
                    "inputs": deepcopy(inputs),
                    "error_message": f"[NodeId: {current.node_id}][Input Resolution Error]: {str(e)}",
                    "args": [],
                    "kwargs": {},
                }
                with thread_lock:
                    extra["error_dict"][current.node_id] = err_info
                _emit(err_info)
                return

        # =================================================================
        # Step 2: execute the current node.
        # =================================================================
        if current.node_type == "LoopBeginNode":
            try:
                def _resolve_cfg(obj):
                    ref = obj.get("reference", "").strip() if isinstance(obj, dict) else ""
                    if ref:
                        return scoped_recursive_get(previous_outputs, loop_stack, ref)
                    return obj.get("value", 0)

                if current_loop_back:
                    with thread_lock:
                        outer_scope = walk_to_scope(previous_outputs, loop_stack)
                        begin_state = outer_scope[current.node_name]
                        step_value = int(current.node_config.get("step_value", 1))
                        begin_state["i"] += step_value
                    output_dict = begin_state
                else:
                    init_i = int(_resolve_cfg(current.node_config["init_value"]))
                    output_dict = {"i": init_i, "loop_output": []}
            except Exception as e:
                err_info = {
                    "status": "error",
                    "output": None,
                    "inputs": deepcopy(inputs),
                    "error_message": f"[NodeId: {current.node_id}][LoopBegin State Error]: {str(e)}",
                    "args": [],
                    "kwargs": {},
                }
                with thread_lock:
                    extra["error_dict"][current.node_id] = err_info
                _emit(err_info)
                return
            call_result = {
                "status": "success",
                "output": output_dict,
                "error_message": "",
                "traceback": "",
                "args": (),
                "kwargs": {},
                "execution_time": 0.0,
            }
        else:
            # Per-node execution dispatch (CodeNode async / thread-bridge /
            # plain sync) — extracted to nodes/exec.py so the agent's run_node
            # shares the exact same dispatch logic.
            cached = extra.get("workflow_resume_visits", {}).get(visit_key(current.node_id, loop_stack))
            if cached is not None and current.node_type not in CONTROL_NODES:
                # A graph/input mismatch must fail closed, never silently repeat
                # side effects from a successful visit.
                if cached["inputs"] != inputs:
                    raise ValueError("workflow_resume_inputs_mismatch")
                call_result = {"status": "success", "output": deepcopy(cached["output"]),
                               "error_message": "", "execution_time": 0.0, "reused": True}
            else:
                call_result = await dispatch_node_call(current, inputs, previous_outputs, extra=extra)
            if call_result.get("status") == "error":
                call_result.pop("traceback")
                call_result["inputs"] = deepcopy(inputs)
                call_result["error_message"] = f"[NodeId: {current.node_id}]" + call_result["error_message"]
                with thread_lock:
                    extra["error_dict"][current.node_id] = call_result
                _emit(call_result)
                return
            output_dict = call_result.get("output", {})

        # Consume loop_back immediately; it applies only to this LoopBegin entry.
        current_loop_back = False

        # =================================================================
        # Step 3: write output into the current scope.
        # =================================================================
        if current.node_type != "LoopEndNode":
            with thread_lock:
                write_scope = walk_to_scope(previous_outputs, loop_stack)
                write_scope[current.node_name] = output_dict

        call_result_with_inputs = dict(call_result)
        call_result_with_inputs["output"] = deepcopy(output_dict)
        call_result_with_inputs["inputs"] = deepcopy(inputs)
        _emit(call_result_with_inputs)

        # =================================================================
        # Step 4: determine the next node or nodes.
        # =================================================================
        next_nodes = []
        loop_back_to = None

        if current.node_type == "ConditionNode":
            cond_name = output_dict.get("condition")
            for c in current.node_config.get("conditions", []):
                if c["condition_name"] == cond_name:
                    next_nodes.append(c["next_node_id"])
                    break

        elif current.node_type == "LoopBeginNode":
            end_val_config = current.node_config["end_value"]
            try:
                with thread_lock:
                    ref = end_val_config.get("reference", "").strip() if isinstance(end_val_config, dict) else ""
                    if ref:
                        end_val = int(scoped_recursive_get(previous_outputs, loop_stack, ref))
                    else:
                        end_val = int(end_val_config.get("value", 0))
            except Exception as e:
                err_info = {
                    "status": "error",
                    "output": None,
                    "inputs": deepcopy(inputs),
                    "error_message": f"[NodeId: {current.node_id}][LoopBegin end_value Error]: {str(e)}",
                    "args": [],
                    "kwargs": {},
                }
                with thread_lock:
                    extra["error_dict"][current.node_id] = err_info
                _emit(err_info)
                return

            if output_dict["i"] >= end_val:
                end_node_id = current.node_config["loop_end_node_id"]
                end_node = extra["id2node"].get(end_node_id)
                if end_node:
                    next_nodes = end_node.children
            else:
                with thread_lock:
                    # Start a new iteration: hand the body a FRESH scratch dict to
                    # write into, carried on the loop_stack frame. We do NOT append
                    # it to loop_output yet — that happens at this iteration's
                    # LoopEnd (spec: append "at each end of the iteration step").
                    # So a body node reading `<begin>.loop_output` sees only the
                    # iterations that have already FINISHED — `[-1]` is the
                    # immediately-previous iteration, never the in-progress one.
                    loop_stack.append({
                        "begin_node_id": current.node_id,
                        "begin_name": current.node_name,
                        # index this iteration will occupy once committed (telemetry)
                        "iter_index": len(output_dict["loop_output"]),
                        "scope": {},
                    })
                next_nodes = current.children

        elif current.node_type == "LoopEndNode":
            begin_node_id = current.node_config["loop_begin_node_id"]
            begin_node = extra["id2node"].get(begin_node_id)

            with thread_lock:
                if loop_stack and loop_stack[-1]["begin_node_id"] == begin_node_id:
                    frame = loop_stack.pop()
                    # Commit this iteration's scratch to the begin node's
                    # loop_output — AFTER the pop, so walk_to_scope resolves the
                    # begin node's OUTER scope (where its {i, loop_output} state
                    # lives). This is the single "append at iteration END" point.
                    outer_scope = walk_to_scope(previous_outputs, loop_stack)
                    begin_state = outer_scope.get(frame["begin_name"])
                    if isinstance(begin_state, dict) and isinstance(
                        begin_state.get("loop_output"), list
                    ):
                        begin_state["loop_output"].append(frame.get("scope", {}))

            loop_back_to = begin_node

        elif current.node_type == "ParallelStartNode":
            next_nodes = current.children
            with thread_lock:
                if "parallel_signal" not in extra:
                    extra["parallel_signal"] = {}
                extra["parallel_signal"][current.node_id] = {
                    "status": "running",
                    "total_tasks": len(next_nodes),
                    "completed_tasks": 0
                }

        else:
            next_nodes = current.children

        # =================================================================
        # Step 5: enter the next node.
        # =================================================================
        # 5.a LoopEnd jump back.
        if loop_back_to is not None:
            current = loop_back_to
            current_loop_back = True
            last_span_id = current_span_id
            continue

        # 5.b Fast path for one sequential child.
        if current.node_type != "ParallelStartNode" and len(next_nodes) <= 1:
            if not next_nodes:
                return
            next_id = next_nodes[0]
            next_node = extra["id2node"].get(next_id) if next_id else None
            if next_node is None:
                return

            if next_node.node_type == "ParallelEndNode":
                start_node_id = next_node.node_config["parallel_start_node_id"]
                with thread_lock:
                    p_sig = extra.get("parallel_signal", {}).get(start_node_id)
                    if p_sig:
                        p_sig["completed_tasks"] += 1
                        if p_sig["completed_tasks"] == p_sig["total_tasks"]:
                            p_sig["status"] = "finished"
                            current = next_node
                            last_span_id = current_span_id
                            continue
                return

            current = next_node
            last_span_id = current_span_id
            continue

        # 5.c Slow path for ParallelStart or multiple children.
        for next_id in next_nodes:
            if not next_id:
                continue
            next_node = extra["id2node"].get(next_id)
            if not next_node:
                continue

            if next_node.node_type == "ParallelEndNode":
                start_node_id = next_node.node_config["parallel_start_node_id"]
                with thread_lock:
                    p_sig = extra.get("parallel_signal", {}).get(start_node_id)
                    if p_sig:
                        p_sig["completed_tasks"] += 1
                        if p_sig["completed_tasks"] == p_sig["total_tasks"]:
                            p_sig["status"] = "finished"
                            await next_node.trigger(previous_outputs, extra, workflow_inputs, parent_span_id=current_span_id)
                continue

            if current.node_type == "ParallelStartNode":
                branch_extra = {
                    **extra,
                    "loop_signal": {
                        "loop_stack": list(extra.get("loop_signal", {}).get("loop_stack", []))
                    },
                }
                on_submit = extra.get("_on_trigger_submit")
                on_done = extra.get("_on_trigger_done")
                if on_submit:
                    on_submit(1)

                async def _branch_wrapper(_node=next_node, _po=previous_outputs,
                                          _be=branch_extra, _wi=workflow_inputs,
                                          _psid=current_span_id, _cb=on_done):
                    try:
                        await _node.trigger(_po, _be, _wi, parent_span_id=_psid)
                    finally:
                        if _cb:
                            _cb()

                try:
                    branch_task = asyncio.create_task(_branch_wrapper())
                    tasks_list = extra.get("_spawned_tasks")
                    if tasks_list is not None:
                        tasks_list.append(branch_task)
                except RuntimeError:
                    # No running loop / loop is closing — drop the branch and
                    # decrement the pending counter so the workflow can drain.
                    if on_done:
                        on_done()
            else:
                await next_node.trigger(previous_outputs, extra, workflow_inputs, parent_span_id=current_span_id)

        return
