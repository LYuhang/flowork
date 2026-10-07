import asyncio
import time
import uuid
import threading

from . import node as _node_registry_init  # noqa: F401 — populate node_registry
from .register import node_registry


# Sentinel object marking the end of an astream() event stream.
_ASTREAM_END = object()
DEFAULT_WORKFLOW_TIMEOUT = 3600.0


class Workflow:
    INPUT_EFFECT_WARNING_NODE_TYPES = {"PromptNode", "CodeNode", "ConditionNode"}

    def __init__(self, workflow_dict: dict = None, *, max_workers: int = 16,
                 execution_timeout: float = DEFAULT_WORKFLOW_TIMEOUT):
        workflow_dict = workflow_dict or {}
        self.max_workers = max_workers
        self._execution_timeout = execution_timeout
        # Keep the original workflow dictionary for caller introspection and
        # debugging. Execution uses the instantiated node graph below.
        self._workflow_dict = workflow_dict
        # 1. Obtain meta information
        self.meta = workflow_dict.get("__meta__", {
            "workflow_id": 'test',
            "workflow_name": 'test',
            "workflow_description": 'test',
            "workflow_version": 0,
            "workflow_subversion": 0,
            "workflow_creator": 'test',
            "workflow_create_time": '20260401000000',
            "workflow_domain": 'test',
            "workflow_status": '',
            "workflow_save": 'True'
        })

        # 2. Instantiate nodes once when loading the workflow.
        self.id2node = self._build_id2node(workflow_dict)

        # 3. Read per-workflow settings from
        #    ``__meta__.settings`` and apply them to the freshly-built node
        #    instances + the workflow execution timeout. Absent/empty settings
        #    → today's behavior, byte-identical (default libs + default
        #    timeouts). A per-node ``node_config`` value still WINS over any
        #    settings-supplied default (the node reads node_config first,
        #    falling back to the instance default we set here).
        self._apply_settings(workflow_dict)

    def _apply_settings(self, workflow_dict: dict) -> None:
        """Apply ``__meta__.settings`` timeouts to nodes + self.

        Resolution order (highest first): per-node ``node_config`` >
        ``__meta__.settings`` > engine hard-coded default. This method only
        fills the per-instance *default* (the settings tier); node_config is
        consulted at call time and therefore always wins.

        CodeNode third-party libraries are NOT applied here: they come from the
        content-addressed dependency overlay (built from
        ``__meta__.settings.code_requirements`` by the env service, bound into
        the sandbox and put on the worker's PYTHONPATH). ``settings``
        keys this method does not own (``code_requirements`` / ``code_libraries``
        / ``code_index_url`` / ``egress`` / ...) are simply ignored here.
        """
        settings = (workflow_dict.get("__meta__") or {}).get("settings") or {}
        if not settings:
            return  # back-compat: no settings → leave every engine default in place

        # --- timeouts ------------------------------------------------------
        timeouts = settings.get("timeouts") or {}
        code_t = timeouts.get("code")
        if code_t is not None and float(code_t) > 0:
            code_t = float(code_t)
            for inst in self.id2node.values():
                if inst.node_type == "CodeNode":
                    inst._default_timeout = code_t

        http_t = timeouts.get("http")
        if http_t is not None and float(http_t) > 0:
            http_t = float(http_t)
            for inst in self.id2node.values():
                if inst.node_type == "HTTPRequestNode":
                    inst._default_timeout = http_t

        wf_t = timeouts.get("workflow")
        if wf_t is not None and float(wf_t) > 0:
            self._execution_timeout = float(wf_t)

    @staticmethod
    def check(workflow_dict: dict) -> dict:
        """
        Check whether the given `workflow_dict` is a valid workflow structure.

        Return ``{"status": "success"}`` or a structured error. This method
        intentionally bypasses ``safe_call_with_args`` so validation failures
        do not copy the complete workflow document into logs or UI responses.
        """
        try:
            Workflow._check_impl(workflow_dict)
        except AssertionError as e:
            return {"status": "error", "error_message": f"[Workflow Check]: {e}"}
        except Exception as e:
            return {"status": "error", "error_message": f"[Workflow Check]: {type(e).__name__}: {e}"}
        warnings = Workflow.collect_warnings(workflow_dict)
        if warnings:
            return {"status": "success", "warnings": warnings}
        return {"status": "success"}

    @staticmethod
    def collect_warnings(workflow_dict: dict) -> list[dict]:
        """Return non-blocking workflow quality warnings.

        Warnings are intentionally separate from validation errors: a workflow
        with warnings can still be imported and executed, but the agent should
        normally clean them up before delivery.
        """
        warnings: list[dict] = []
        if not isinstance(workflow_dict, dict):
            return warnings

        missing_input_sources: list[str] = []
        for n_id, node in workflow_dict.items():
            if n_id == "__meta__" or not isinstance(node, dict):
                continue
            n_type = node.get("node_type")
            if n_type not in Workflow.INPUT_EFFECT_WARNING_NODE_TYPES:
                continue
            input_fields = node.get("input_fields") or {}
            if not isinstance(input_fields, dict):
                continue
            for field_name, field_info in input_fields.items():
                if not isinstance(field_info, dict):
                    continue
                value = field_info.get("value")
                reference = field_info.get("reference")
                has_value = value is not None and value != ""
                has_reference = isinstance(reference, str) and reference.strip() != ""
                if has_value or has_reference:
                    continue
                missing_input_sources.append(f"{n_id}.{field_name}")
        if missing_input_sources:
            field_list = ", ".join(missing_input_sources)
            warnings.append({
                "node_id": "global",
                "kind": "empty_input_sources",
                "fields": missing_input_sources,
                "message": (
                    "[Workflow Warning]: Some PromptNode, CodeNode, or "
                    "ConditionNode input fields have neither value nor "
                    "reference, so they are not effective. Set a preset value, "
                    "reference a previous node output, or remove unused input "
                    f"fields to keep the workflow concise. Fields: {field_list}"
                ),
            })
        return warnings

    @staticmethod
    def _check_impl(workflow_dict: dict):
        """Validate a workflow, raising assertions for ``check`` to structure."""
        # 1. Require exactly one StartNode.
        start_nodes = [n_id for n_id, n in workflow_dict.items() if n.get("node_type") == "StartNode"]
        assert len(start_nodes) == 1, f"Workflow must contain exactly one StartNode, found {len(start_nodes)}."
        start_node_id = start_nodes[0]

        # 2. Check reachability with iterative DFS to support long chains.
        visited = set()
        stack = [start_node_id]
        while stack:
            nid = stack.pop()
            if nid in visited:
                continue
            visited.add(nid)
            for child_id in workflow_dict.get(nid, {}).get("children", []):
                if child_id in workflow_dict and child_id not in visited:
                    stack.append(child_id)

        all_node_ids = {k for k in workflow_dict.keys() if k != "__meta__"}
        isolated_nodes = all_node_ids - visited
        assert not isolated_nodes, f"Found isolated nodes unreachable from StartNode: {isolated_nodes}"

        # 3. Require children edges to form a DAG. Loop-back is represented by
        # loop_begin_node_id rather than a children edge. Use iterative
        # three-color DFS to avoid recursion limits.
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {start_node_id: GRAY}
        dfs_stack = [(start_node_id, iter(workflow_dict.get(start_node_id, {}).get("children", [])))]

        while dfs_stack:
            nid, children_iter = dfs_stack[-1]
            next_child = next(children_iter, None)
            if next_child is None:
                color[nid] = BLACK
                dfs_stack.pop()
                continue
            if next_child not in workflow_dict:
                continue
            c = color.get(next_child, WHITE)
            assert c != GRAY, f"Workflow graph must be a Directed Acyclic Graph (DAG). Cycle detected at node '{next_child}'."
            if c == WHITE:
                color[next_child] = GRAY
                dfs_stack.append((next_child, iter(workflow_dict.get(next_child, {}).get("children", []))))

        # 4. Validate paired control nodes and references.
        parallel_starts = []
        parallel_ends = []
        loop_begins = []
        loop_ends = []

        # Prebuild the node-name to output-fields index.
        name_to_outputs = {
            n["node_name"]: n.get("output_fields") or {}
            for n_id, n in workflow_dict.items()
            if n_id != "__meta__" and isinstance(n, dict) and n.get("node_name")
        }

        for n_id, node in workflow_dict.items():
            if n_id == "__meta__":
                continue
            n_type = node.get("node_type")
            n_config = node.get("node_config", {})

            # Record paired control nodes for final consistency checks.
            if n_type == "ParallelStartNode":
                parallel_starts.append(n_id)
                assert n_config.get("parallel_end_node_id") in workflow_dict, f"ParallelStartNode {n_id} points to invalid end node."
            elif n_type == "ParallelEndNode":
                parallel_ends.append(n_id)
                assert n_config.get("parallel_start_node_id") in workflow_dict, f"ParallelEndNode {n_id} points to invalid start node."
            elif n_type == "LoopBeginNode":
                loop_begins.append(n_id)
                end_id = n_config.get("loop_end_node_id")
                assert end_id in workflow_dict, f"LoopBeginNode '{n_id}' points to an invalid end node '{end_id}'."
                assert workflow_dict[end_id].get("node_type") == "LoopEndNode", f"Node '{end_id}' is not a LoopEndNode."
            elif n_type == "LoopEndNode":
                loop_ends.append(n_id)
                begin_id = n_config.get("loop_begin_node_id")
                assert begin_id in workflow_dict, f"LoopEndNode '{n_id}' points to an invalid begin node '{begin_id}'."
                assert workflow_dict[begin_id].get("node_type") == "LoopBeginNode", f"Node '{begin_id}' is not a LoopBeginNode."

            # Validate the referenced node, top-level output field, and
            # top-level input/output type. Dynamic nested paths are not inferred.
            for field_name, field_info in node.get("input_fields", {}).items():
                ref = field_info.get("reference")
                if not ref:
                    continue

                # Parse the first two segments of node_name.field_name[idx].sub.
                head, sep, rest = ref.partition(".")
                ref_node_name = head
                ref_field_name = None
                if sep and rest:
                    # Stop the second segment at the next dot or bracket.
                    first_field = rest.replace("[", ".", 1).split(".", 1)[0]
                    ref_field_name = first_field or None

                assert ref_node_name in name_to_outputs, \
                    f"Node '{n_id}' input field '{field_name}' references an invalid node name: '{ref_node_name}'."

                assert ref_field_name is not None, \
                    f"Node '{n_id}' input field '{field_name}' reference '{ref}' must include both node name and output field name, for example 'node_name.output_field'."

                assert ref_field_name in name_to_outputs[ref_node_name], \
                    f"Node '{n_id}' input field '{field_name}' references an invalid output field: '{ref_node_name}.{ref_field_name}'."

                input_type = field_info.get("type")
                output_type = name_to_outputs[ref_node_name][ref_field_name].get("type")
                assert input_type == output_type, (
                    f"Node '{n_id}' input field '{field_name}' type mismatch for "
                    f"reference '{ref_node_name}.{ref_field_name}': input field type "
                    f"is '{input_type}', but referenced output field type is "
                    f"'{output_type}'. Make the input field type match the referenced "
                    f"output, or insert a CodeNode to convert the value first."
                )

        assert len(parallel_starts) == len(parallel_ends), "ParallelStartNode and ParallelEndNode must appear in pairs."
        assert len(loop_begins) == len(loop_ends), "LoopBeginNode and LoopEndNode must appear in pairs."

        # 5. Parent-count constraints per node type.
        #    Most nodes accept exactly one parent (a single upstream source for
        #    input_fields references). The two explicit join points
        #    (ParallelEndNode, LoopEndNode) accept many parents — that's the
        #    entire purpose of those nodes. StartNode is the unique entry and
        #    must have zero parents. The loop-back from LoopEndNode to
        #    LoopBeginNode is carried by `loop_begin_node_id` (config), NOT
        #    via `children`, so it does not count as a parent here.
        parent_count = {n_id: 0 for n_id in workflow_dict.keys() if n_id != "__meta__"}
        for n_id, node in workflow_dict.items():
            if n_id == "__meta__" or not isinstance(node, dict):
                continue
            for child_id in node.get("children", []) or []:
                if child_id in parent_count:
                    parent_count[child_id] += 1

        # (min, max). max=None means unbounded.
        PARENT_RULES = {
            "StartNode":         (0, 0),
            "EndNode":           (1, 1),
            "ConditionNode":     (1, 1),
            "CodeNode":          (1, 1),
            "PromptNode":        (1, 1),
            "TemplateNode":      (1, 1),
            "TransformNode":     (1, 1),
            "HTTPRequestNode":   (1, 1),
            "TableReadNode":      (1, 1),
            "TableWriteNode":     (1, 1),
            "ParallelStartNode": (1, 1),
            "ParallelEndNode":   (2, None),  # ≥ 2 — explicit fan-in join point
            "LoopBeginNode":     (1, 1),
            "LoopEndNode":       (1, None),  # ≥ 1 — body may contain branches
        }
        for n_id, node in workflow_dict.items():
            if n_id == "__meta__" or not isinstance(node, dict):
                continue
            n_type = node.get("node_type")
            rule = PARENT_RULES.get(n_type)
            if rule is None:
                continue  # unknown type — let other validators flag it
            n_parents = parent_count.get(n_id, 0)
            lo, hi = rule
            assert n_parents >= lo, (
                f"{n_type} '{n_id}' has {n_parents} parent(s); requires ≥ {lo}."
            )
            if hi is not None:
                if n_type == "EndNode":
                    assert n_parents <= hi, (
                        f"EndNode '{n_id}' has {n_parents} parent(s); allows ≤ {hi}. "
                        f"Different terminal branches must use different EndNode nodes; "
                        f"do not share one EndNode across branches."
                    )
                    continue
                assert n_parents <= hi, (
                    f"{n_type} '{n_id}' has {n_parents} parent(s); allows ≤ {hi}. "
                    f"If multiple branches need to merge here, route them through a "
                    f"ParallelEndNode or LoopEndNode instead."
                )

        # 6. Per-node configuration checks.
        #    The structural checks above (1–5) intentionally run FIRST, so a
        #    malformed graph fails with a clear structural message before a
        #    per-node check throws a confusing secondary error. This step is
        #    ADDITIVE: it invokes each node class's own `check()` — the same
        #    logic the agent path (`vibe_workflow.py`) already runs — so the
        #    route Check enforces node-type invariants the structural pass does
        #    NOT (e.g. ConditionNode `conditions`↔`children` parity + mandatory
        #    "others" fallback + single `condition` output_field, ParallelStart
        #    `branches`↔`children` parity). After this, "Check ✓" ⟺ runnable.
        #
        #    Resolution mirrors `_build_id2node` / `vibe_workflow.py`: look the
        #    class up in the shared `node_registry`. Per-node `check()` methods
        #    are wrapped with `@safe_call_with_args`, so they RETURN a result
        #    dict ({"status": ..., "error_message": ...}) rather than raising;
        #    a node type that does NOT override `check` inherits
        #    `BaseNode.check` (which raises NotImplementedError) and is skipped
        #    gracefully — those (e.g. api-only `KnowledgeSearchNode`) are
        #    validated where they are registered, not by the route Check.
        from .nodes.base import BaseNode  # local import: avoid import cycle at module load

        for n_id, node in workflow_dict.items():
            if n_id == "__meta__" or not isinstance(node, dict):
                continue
            n_type = node.get("node_type")
            try:
                node_class = node_registry.get(n_type)
            except KeyError:
                # Unknown node_type — structural pass / instantiation surfaces it;
                # don't double-report here.
                continue
            if node_class.check is BaseNode.check:
                # No per-node check implemented (inherits the default that
                # raises NotImplementedError) — skip gracefully.
                continue
            result = node_class.check(node)
            if isinstance(result, dict) and result.get("status") == "error":
                raise AssertionError(
                    f"Node '{n_id}' ({n_type}): {result.get('error_message', 'invalid node configuration')}"
                )

    def _build_id2node(self, workflow_dict: dict) -> dict:
        """Instantiate reusable nodes from a workflow dictionary.

        Node instances reference nested workflow structures directly. Callers
        that need to mutate the original dictionary after construction must
        provide their own deep copy.
        """
        id2node = {}
        for node_id, node_dict in workflow_dict.items():
            if node_id == "__meta__":
                continue
            node_type = node_dict.get("node_type")
            node_class = node_registry.get(node_type)
            id2node[node_id] = node_class(**node_dict)
        return id2node

    async def _execute(self, workflow_inputs: dict, info_queue=None, stop_event=None, run_context: dict | None = None, run_state: dict | None = None):
        """Execute a workflow with a fresh, isolated context.

        The engine uses an asyncio host process plus sandbox worker processes.
        ``info_queue`` is an :class:`asyncio.Queue`; ``stop_event`` is normally
        an :class:`asyncio.Event` and may be any compatible object exposing
        ``is_set``.

        ``run_state`` is an optional mutable dict owned by ``astream``
        owns so an out-of-band cancel watcher can reach this run's live ``extra``
        — specifically to tear down the lazily-created ``_code_pool`` (kill its
        worker subprocesses) the MOMENT cancellation fires, instead of waiting
        for a blocked CodeNode ``to_thread`` to return on its own.
        """
        start_time = time.perf_counter()

        start_node = next((n for n in self.id2node.values() if n.node_type == "StartNode"), None)
        if not start_node:
            if info_queue is not None:
                info_queue.put_nowait({"status": "error", "error_message": "StartNode not found."})
            return {}, {}, 0.0

        # 1. Initialize fresh per-run execution state.
        previous_outputs: dict = {}

        # Track the root trigger and every spawned ParallelStart coroutine. The
        # run finishes only after the counter reaches zero.
        pending_counter = [1]
        pending_lock = threading.Lock()
        done_event = asyncio.Event()
        spawned_tasks: list[asyncio.Task] = []

        # Keep the active loop so callbacks from worker threads can signal
        # completion safely.
        loop = asyncio.get_running_loop()

        def _on_trigger_done():
            with pending_lock:
                pending_counter[0] -= 1
                if pending_counter[0] == 0:
                    # asyncio.Event.set is not thread-safe.
                    try:
                        loop.call_soon_threadsafe(done_event.set)
                    except RuntimeError:
                        # No waiter remains once the event loop is closed.
                        done_event.set()

        def _on_trigger_submit(count: int):
            with pending_lock:
                pending_counter[0] += count

        # Retain a threading lock because callbacks may cross into worker
        # threads even though normal asyncio execution is single-threaded.
        extra = {
            "info_queue": info_queue,
            "parallel_signal": {},
            "loop_signal": {"loop_stack": []},
            "thread_lock": threading.Lock(),
            "error_dict": {},
            "id2node": self.id2node,
            "stop_event": stop_event,
            "trace_id": uuid.uuid4().hex,
            "_on_trigger_done": _on_trigger_done,
            "_on_trigger_submit": _on_trigger_submit,
            "_spawned_tasks": spawned_tasks,
        }

        # The API builds run_context; the engine forwards its opaque callbacks
        # to every node without importing API implementation details.
        if run_context:
            extra.update(run_context)

        # Branches shallow-copy extra. Establish one run-owned pool before
        # they split, so every branch shares it and final cleanup can find it.
        # Constructing the pool does not spawn workers; those remain lazy.
        for node in self.id2node.values():
            if node.node_type == "CodeNode":
                node._get_run_pool(extra)
                break

        # Publish this run's live ``extra`` to the caller's ``run_state``
        # holder so an out-of-band cancel watcher (in ``astream``) can reach the
        # lazily-created ``_code_pool`` and kill its workers the moment cancel
        # fires — even while a CodeNode is blocked in a ``to_thread`` pool.run().
        if run_state is not None:
            run_state["extra"] = extra

        # 2. Execute under a finally block so every outcome tears down workers.
        try:
            try:
                await start_node.trigger(
                    previous_outputs=previous_outputs,
                    extra=extra,
                    workflow_inputs=workflow_inputs,
                )
            except Exception as e:
                if info_queue is not None:
                    info_queue.put_nowait({"status": "error", "error_message": f"Engine critical error: {str(e)}"})
            finally:
                _on_trigger_done()

            # Wait for every parallel branch or the workflow timeout.
            try:
                # A resident runtime meters active execution time across the
                # whole graph, excluding human waits. Its independent stop
                # signal owns the budget; this legacy parallel-tail timer must
                # not expire while the graph awaits a human decision.
                timeout = None if extra.get("execution_budget_managed") else self._execution_timeout
                await asyncio.wait_for(done_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                extra["error_dict"]["__engine__"] = {
                    "status": "error",
                    "error_message": f"Workflow execution timed out after {self._execution_timeout}s",
                }
                # Cancel any branch still alive after the timeout.
                for t in spawned_tasks:
                    if not t.done():
                        t.cancel()
                # Await cancellation so no pending task is destroyed.
                if spawned_tasks:
                    await asyncio.gather(*spawned_tasks, return_exceptions=True)
        finally:
            for task in spawned_tasks:
                if not task.done():
                    task.cancel()
            if spawned_tasks:
                await asyncio.gather(*spawned_tasks, return_exceptions=True)
            # Always tear down the run's CodeNode worker pool
            # (lazily created by CodeNode.__call__ in ``extra["_code_pool"]``).
            # Runs in ``finally`` so it fires on success, node error, engine
            # exception, AND cancel — the per-run worker subprocesses never
            # outlive the run. ``close()`` is idempotent + fail-soft (the cancel
            # watcher may have closed it already; a close error never breaks the
            # run result). We pop so a later double-close is a no-op.
            code_pool = extra.pop("_code_pool", None)
            if code_pool is not None:
                try:
                    code_pool.close()
                except Exception:
                    pass

        end_time = time.perf_counter()
        execution_time = end_time - start_time

        # 3. Emit the terminal result event.
        if info_queue is not None:
            info_queue.put_nowait({
                "status": "finished",
                "final_outputs": previous_outputs,
                "error_dict": extra["error_dict"],
                "execution_time": execution_time,
                "trace_id": extra.get("trace_id"),
            })

        return previous_outputs, extra["error_dict"], execution_time

    async def astream(self, workflow_inputs: dict, stop_event: asyncio.Event | None = None, run_context: dict | None = None):
        """Async iterator over execution events.

        Yields each event the dispatcher emits (``status`` ∈
        ``"success" | "error" | "finished"``), in order. The internal
        producer task runs ``_execute`` against an :class:`asyncio.Queue`
        and signals end-of-stream via a sentinel.

        Cancelling the consumer (breaking out of the ``async for``) sets
        the ``stop_event`` and cancels this execution's producer/branches.
        Owned Code workers and asynchronous I/O stop before the stream exits.
        """
        info_queue: asyncio.Queue = asyncio.Queue()
        stop_event = stop_event or asyncio.Event()

        # Shared holder so the cancel watcher can reach the run's live
        # ``extra`` (hence its ``_code_pool``) once ``_execute`` populates it.
        run_state: dict = {}

        async def _producer():
            try:
                await self._execute(
                    workflow_inputs, info_queue=info_queue, stop_event=stop_event,
                    run_context=run_context, run_state=run_state,
                )
            finally:
                # End-of-stream sentinel — guaranteed even on exception.
                info_queue.put_nowait(_ASTREAM_END)

        async def _cancel_watcher():
            """When ``stop_event`` fires, tear down the run's CodeNode worker
            pool PROMPTLY (SIGKILL in-flight workers). Without this, a cancel
            during a long-running CodeNode would block on the worker's
            ``to_thread`` pipe read until its own timeout — the orphaned
            subprocess would keep running. ``close()`` is idempotent, so the
            ``_execute`` ``finally`` closing it again is a harmless no-op."""
            await stop_event.wait()
            extra = run_state.get("extra")
            if extra is not None:
                pool = extra.get("_code_pool")
                if pool is not None:
                    try:
                        pool.close()
                    except Exception:
                        pass

            if not exec_task.done():
                exec_task.cancel()

        exec_task = asyncio.create_task(_producer())
        watcher_task = asyncio.create_task(_cancel_watcher())
        try:
            while True:
                ev = await info_queue.get()
                if ev is _ASTREAM_END:
                    break
                yield ev
        finally:
            stop_event.set()
            await asyncio.gather(watcher_task, return_exceptions=True)
            await asyncio.gather(exec_task, return_exceptions=True)

        # Cooperative cancellation is an ordinary end of this stream; a real
        # producer error still propagates to the owning runtime invocation.
        if not exec_task.cancelled() and exec_task.exception() is not None:
            raise exec_task.exception()

    async def _trigger_inner(self, workflow_inputs: dict, stop_event=None, run_context: dict | None = None) -> tuple[dict, dict, float]:
        """Drain :py:meth:`astream` into the legacy
        ``(previous_outputs, error_dict, exec_time)`` tuple.

        Used exclusively by the sync :py:meth:`trigger` wrapper. New async
        callers should iterate ``astream`` directly so they see per-node
        events as they fire.
        """
        previous_outputs: dict = {}
        error_dict: dict = {}
        started = time.monotonic()
        async for ev in self.astream(workflow_inputs, stop_event=stop_event, run_context=run_context):
            status = ev.get("status")
            if status == "finished":
                # ``_execute`` keys the final outputs as ``final_outputs`` and
                # the per-node errors as ``error_dict`` on the finished event.
                previous_outputs = ev.get("final_outputs", previous_outputs)
                # Per-node errors arrive bundled on the finished event; merge
                # them so the sync caller sees the same shape as before T6.
                err_bundle = ev.get("error_dict") or {}
                if isinstance(err_bundle, dict):
                    error_dict.update(err_bundle)
            elif status == "error":
                # Engine-level critical error — not associated with a node id.
                node_key = ev.get("node_id", "__engine__")
                error_dict[node_key] = ev.get("error_message", "")
        return previous_outputs, error_dict, time.monotonic() - started

    def trigger(self, workflow_inputs: dict, stop_event=None, run_context: dict | None = None):
        """Sync entry point for CLI / legacy callers — wraps :py:meth:`astream`
        into the historical ``(previous_outputs, error_dict, execution_time)``
        tuple via :func:`asyncio.run`. New async callers should use
        ``async for ev in workflow.astream(inputs)`` directly.

        Raises:
            RuntimeError: when called from inside a running event loop.
                Use ``async for ev in workflow.astream(inputs)`` instead.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running loop in this thread — safe to drive one ourselves.
            pass
        else:
            raise RuntimeError(
                "Workflow.trigger() cannot be called from a running event loop. "
                "Use `async for ev in workflow.astream(inputs)` instead."
            )
        return asyncio.run(self._trigger_inner(workflow_inputs, stop_event=stop_event, run_context=run_context))
