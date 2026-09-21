"""Workflow instrumentation that consumes the engine's event stream.

Builds a parent ``workflow.execute`` span and retrospective
per-node spans (the engine emits a node event only AFTER the node finishes, so
spans are constructed with start_time = end − execution_time). Reuses the
engine-provided span_id/parent_span_id/trace_id only as attributes for
correlation. Engine is NOT modified. Fail-safe.

Return-shape parity (the risk of T5): this consumer returns
``(final_outputs, error_dict, exec_time)`` — the SAME tuple
``Workflow._trigger_inner`` / ``Workflow.trigger`` historically returned:

* ``final_outputs`` is the engine's ``finished`` event ``final_outputs`` key,
  which ``Workflow._execute`` populates with ``previous_outputs`` — byte
  identical to what ``trigger`` returned as its first element.
* ``error_dict`` reproduces ``_trigger_inner``'s exact merge: engine-level
  ``error`` events are keyed by ``node_id`` (falling back to ``__engine__``),
  and the ``finished`` event's bundled ``error_dict`` is merged in. This keeps
  ``batch_exec``'s ``errors.items()`` rendering unchanged.
* ``exec_time`` is the engine's perf-counter ``execution_time`` (seconds),
  matching ``run_workflow_sync``'s historical seconds contract. The async
  ``drain_astream`` wrapper converts to milliseconds for its own callers.
"""
from __future__ import annotations

import logging
import time

from vibecanvas_api.observability.metrics import (
    WORKFLOW_EXECUTIONS_TOTAL,
    WORKFLOW_EXECUTION_DURATION,
    WORKFLOW_NODE_DURATION,
    WORKFLOW_NODE_ERRORS_TOTAL,
)
from vibecanvas_api.observability.otel import trace

_log = logging.getLogger(__name__)
_tracer = trace.get_tracer("vibecanvas.workflow")


def _emit_node_span(ev: dict) -> None:
    """Build a retroactive completed span for one finished node event."""
    try:
        node_type = ev.get("node_type", "unknown")
        exec_s = float(ev.get("execution_time") or 0.0)
        end = time.time()
        start = end - exec_s
        span = _tracer.start_span(
            "workflow.node",
            start_time=int(start * 1e9),
            attributes={
                "node.id": ev.get("node_id", ""),
                "node.type": node_type,
                "node.status": ev.get("status", ""),
                "engine.span_id": str(ev.get("span_id", "")),
                "engine.parent_span_id": str(ev.get("parent_span_id", "")),
                "engine.trace_id": str(ev.get("trace_id", "")),
            },
        )
        if ev.get("status") == "error":
            span.set_status(trace.Status(trace.StatusCode.ERROR, ev.get("error_message", "")))
            WORKFLOW_NODE_ERRORS_TOTAL.labels(node_type=node_type).inc()
        WORKFLOW_NODE_DURATION.labels(node_type=node_type).observe(exec_s)
        span.end(end_time=int(end * 1e9))
    except Exception:
        _log.exception("node span emit failed")  # never block the workflow


async def instrumented_drain(
    wf, inputs: dict, run_context: dict | None = None,
) -> tuple[dict, dict, float]:
    """Shared instrumented consumer of ``wf.astream()``. Returns
    ``(final_outputs, error_dict, execution_time)`` — same contract as
    ``Workflow._trigger_inner`` (the body of ``Workflow.trigger``). Used by
    BOTH the async API path (``drain_astream``) and the sync background worker path
    (``run_workflow_sync``).

    The error merge mirrors ``_trigger_inner`` exactly so this is
    behavior-preserving for every caller of ``run_workflow_sync``.

    ``run_context`` (RE-3 T2) is the run-tier seam — forwarded verbatim into
    ``wf.astream`` so the engine merges it into every node's ``extra``. ``None``
    (the default) preserves the pre-RE-3 behaviour exactly.
    """
    final_outputs: dict = {}
    error_dict: dict = {}
    exec_time = 0.0
    had_error_event = False
    start = time.time()
    with _tracer.start_as_current_span("workflow.execute") as parent:
        try:
            async for ev in wf.astream(inputs, run_context=run_context):
                status_ = ev.get("status")
                if status_ == "success":
                    _emit_node_span(ev)
                elif status_ == "error":
                    # Engine-level critical error — not associated with a node
                    # id in _execute (it carries only error_message). Key it the
                    # same way _trigger_inner does so batch_exec's errors.items()
                    # stays identical.
                    _emit_node_span(ev)
                    had_error_event = True
                    node_key = ev.get("node_id", "__engine__")
                    error_dict[node_key] = ev.get("error_message", "")
                    parent.set_status(trace.Status(trace.StatusCode.ERROR))
                elif status_ == "finished":
                    final_outputs = ev.get("final_outputs", final_outputs)
                    # Per-node errors arrive bundled on the finished event;
                    # merge them (matches _trigger_inner).
                    err_bundle = ev.get("error_dict") or {}
                    if isinstance(err_bundle, dict):
                        error_dict.update(err_bundle)
                    exec_time = float(ev.get("execution_time") or 0.0)
        finally:
            try:
                WORKFLOW_EXECUTION_DURATION.observe(time.time() - start)
            except Exception:
                _log.exception("workflow duration observe failed")  # never block the workflow
    status_label = "error" if (error_dict or had_error_event) else "success"
    # Fail-safe (D7): a broken metric must never break the workflow result.
    try:
        WORKFLOW_EXECUTIONS_TOTAL.labels(status=status_label).inc()
    except Exception:
        _log.exception("workflow executions counter inc failed")  # never block the workflow
    return final_outputs, error_dict, exec_time
