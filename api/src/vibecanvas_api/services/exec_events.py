# -*- coding: utf-8 -*-
"""Canonical engine-event to frontend ``EXEC_UPDATE`` mapper.

This is THE adapter both transports use — the in-process live route
(``routes/executions.py``), the sandbox tail, and the node-execute route all
reuse the same frame shapes. It is a
pure function (no I/O, no DB, no globals) so it is trivially unit-tested.

It is an *explicit* mapping, NOT a 1:1 mirror of the engine event. The
engine emits ``output`` (a dict) / ``error_message`` (a str); the
frontend reads ``result`` (a JSON *string*) / ``error`` (a str) /
``status``. The mapper bridges that gap and is the one place the two
vocabularies meet:

    engine status   →   frontend status
    ------------------------------------
    "running"       →   "running"      (per-node started)
    "success"       →   "completed"    (per-node done, + result/inputs)
    "completed"     →   "completed"    (sandbox/node-run synonym)
    "error" (node)  →   "error"        (per-node failure)
    "finished"      →   terminal "completed" (whole-workflow outputs)
    "error" (engine)→   terminal "error" (no node_id)

``result`` is built with ``json.dumps(output, default=str,
ensure_ascii=False)`` (M3) — a node ``output`` can carry non-JSON types
(numpy / Decimal / bytes / PIL); a bare ``json.dumps`` would raise
mid-stream and tear down the whole SSE run. ``default=str`` mirrors
``sandbox_entry.py``'s event-line serialization so both transports
degrade identically.
"""

from __future__ import annotations

import json


def to_exec_update(ev: dict, exec_id: str) -> tuple[str, dict] | None:
    """Map ONE engine astream event → an SSE ``(event_name, payload)``.

    Returns ``None`` for events that carry no frontend-visible frame (so
    callers ``continue`` past them). Today every recognized status maps to
    a frame; an unrecognized status is dropped (``None``) so a future
    engine event cannot crash the live stream.
    """
    status = ev.get("status")

    if status == "running":
        # ``node_id`` is enriched onto every per-node start event.
        return ("EXEC_UPDATE", {
            "exec_id": exec_id,
            "node_id": ev.get("node_id"),
            "node_name": ev.get("node_name"),
            "node_type": ev.get("node_type"),
            "status": "running",
        })

    if status in {"success", "completed"}:
        output = ev.get("output") if "output" in ev else ev.get("result")
        return ("EXEC_UPDATE", {
            "exec_id": exec_id,
            "node_id": ev.get("node_id"),
            "node_name": ev.get("node_name"),
            "node_type": ev.get("node_type"),
            "status": "completed",
            # M3: default=str so a non-serializable node output degrades to
            # its str() rather than raising and killing the whole run.
            "result": json.dumps(
                output, default=str, ensure_ascii=False
            ),
            "inputs": ev.get("inputs"),
            **({'resource_audit': ev['resource_audit']} if 'resource_audit' in ev else {}),
            # Per-node wall-clock seconds — the engine stamps ``execution_time``
            # onto the success envelope (``utils.safe_call_with_args`` /
            # ``nodes/code.py``). Surface it so the Run output can show each
            # node's duration next to its "completed" badge. May be ``None`` if
            # the envelope predates timing (forward/back-compat).
            "duration": ev.get("execution_time"),
        })

    if status == "error":
        node_id = ev.get("node_id")
        if node_id:
            # Per-node failure (input-resolution / dispatch error).
            return ("EXEC_UPDATE", {
                "exec_id": exec_id,
                "node_id": node_id,
                "node_name": ev.get("node_name"),
                "node_type": ev.get("node_type"),
                "status": "error",
                "error": ev.get("error_message", ""),
                **({"result": json.dumps(ev["output"], default=str, ensure_ascii=False)}
                   if isinstance(ev.get("output"), dict) and "__traces__" in ev["output"] else {}),
                # Failed nodes are the most important ones to reproduce in the
                # isolated debugger.  The engine includes the resolved inputs
                # on node-scoped error envelopes; carry them through to the
                # live store and the durable __exec__/nodes result file.
                "inputs": ev.get("inputs"),
                **({'resource_audit': ev['resource_audit']} if 'resource_audit' in ev else {}),
            })
        # Engine-level critical error — not tied to a node → terminal error.
        return ("EXEC_UPDATE", {
            "exec_id": exec_id,
            "status": "error",
            "error": ev.get("error_message", ""),
        })

    if status == "finished":
        final_outputs = ev.get("final_outputs") or {}
        errors = ev.get("error_dict") or {}
        return ("EXEC_UPDATE", {
            "exec_id": exec_id,
            # The engine always emits a final ``finished`` envelope, including
            # when one or more nodes failed.  Do not translate that transport
            # fence into a successful workflow status when ``error_dict`` is
            # non-empty; the UI otherwise renders the contradictory pair
            # "Status: completed" and an error card.
            "status": "error" if errors else "completed",
            "outputs": final_outputs.get("__end__", {})
            if isinstance(final_outputs, dict) else {},
            "errors": errors,
            "duration": ev.get("execution_time"),
        })

    return None
