# -*- coding: utf-8 -*-
"""RE-6 P2 — in-sandbox entrypoint (PURE ENGINE — NO api import).

Runs a pure-engine workflow INSIDE the selected OS sandbox. The bind-mounted run-tier
is the result channel (no socket / stdout protocol):

  - Host writes ``{run_root}/__exec__/workflow.json`` + ``inputs.json`` before
    launching the sandbox.
  - This entrypoint (inside the sandbox, ``run_root="/run"``) reads them, runs
    the workflow's ``astream`` with ``run_context = {run_id, run_dir: run_root}``
    (``/run`` is execution-local scratch space), and writes:
      * ``events.ndjson`` — one JSON ``astream`` event per line (observability).
      * ``result.json``   — ``{final_outputs, error_dict, execution_time}``.
  - Host reads ``result.json`` after the sandbox exits.

Runnable as ``python -m vibecanvas_engine.sandbox_entry <run_id>`` (run_root=/run).

The accumulation MIRRORS :py:meth:`Workflow._trigger_inner` EXACTLY (final
outputs from the ``finished`` event, error_dict merged from it, ``status ==
"error"`` keyed by ``node_id`` / ``__engine__``) — done inline while iterating
``astream`` ONCE, so the workflow runs a single time while we also persist the
raw events.
"""

from __future__ import annotations

import asyncio
import os
import sys


def _append_extra_python_paths() -> None:
    for path in os.environ.get("VC_SANDBOX_PYTHON_PATHS", "").split(os.pathsep):
        if path and path not in sys.path:
            sys.path.append(path)


_append_extra_python_paths()

import glob
import json
import threading
import time

from .sandbox_bus import MSG_NODE_EVENT, MSG_RESULT, connect_bus
from .workflow import Workflow
from .utils import InputNormalizeError, normalize_inputs_for_fields, start_node_input_fields

# Environment variable carrying the in-sandbox bus socket path. The host binds a
# short per-run UDS dir into the sandbox (gvisor.py) and sets this to the in-
# sandbox socket path (e.g. ``/run/__exec__/bus.sock``). When UNSET → no bus, the
# entrypoint behaves exactly as before (events.ndjson + result.json only).
_BUS_SOCK_ENV = "VC_BUS_SOCK"


class _MalformedRunSubpath(Exception):
    """A job's ``run_subpath`` escapes the runs root (absolute or contains ``..``).

    Raised inside the serve loop's per-job ``try`` so it falls into the same
    malformed-job handler: skip running, still write ``.done``, never crash.
    """


def _exec_dir(run_root: str) -> str:
    return os.path.join(run_root, "__exec__")


def _write_result(run_root: str, final_outputs: dict, error_dict: dict, execution_time: float) -> None:
    path = os.path.join(_exec_dir(run_root), "result.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "final_outputs": final_outputs,
                "error_dict": error_dict,
                "execution_time": execution_time,
            },
            f,
            ensure_ascii=False,
            default=str,
        )


# P0 — job-kind dispatch. The host MAY write ``__exec__/job.json`` =
# {"kind": "workflow"|"node"|"tool"|"code", ...}. Absent → "workflow"
# (back-compat: the legacy one-shot path wrote only workflow.json/inputs.json).
# Only "workflow" is wired in this build; node/tool/code are recognized but
# return a clean engine-error result (their runners are later phases). PURE
# stdlib — the engine never learns about the host's job semantics beyond a kind.
JOB_WORKFLOW = "workflow"
JOB_NODE = "node"
JOB_CODE = "code"
JOB_UNSUPPORTED_KINDS = ("tool",)


def read_job(run_root: str) -> dict:
    """Read ``__exec__/job.json`` if present; default to a workflow job.

    A malformed/missing descriptor degrades to ``{"kind": "workflow"}`` so a
    legacy bundle (no job.json) runs exactly as before. NEVER raises."""
    path = os.path.join(_exec_dir(run_root), "job.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            job = json.load(f)
        if isinstance(job, dict) and isinstance(job.get("kind"), str):
            return job
    except (OSError, ValueError):
        pass
    return {"kind": JOB_WORKFLOW}


async def _drive_node(
    node_dict: dict, inputs: dict, extra: dict, events_path: str,
    bus_sock: "str | None" = None,
) -> tuple[dict, dict, float]:
    """Run ONE node via the pure-engine ``run_node`` and synthesize the same
    running→completed/error frame stream the api ``run_node_to_frames`` emits.
    Returns ``(final_outputs, error_dict, execution_time)`` —
    ``final_outputs={node_id: output}`` on success / ``error_dict={node_id: msg}``
    on a node (or construction) error. NEVER raises — any exception degrades to an
    error result so the outcome ALWAYS propagates out of the sandbox."""
    import time as _time
    from .nodes.exec import run_node
    node_id = node_dict.get("node_id") or "node_x"
    started = _time.monotonic()
    bus = None
    if bus_sock:
        try:
            bus = await connect_bus(bus_sock)
        except Exception:
            bus = None
    final_outputs: dict = {}
    error_dict: dict = {}
    try:
        with open(events_path, "w", encoding="utf-8") as ev_f:
            running = {"node_id": node_id, "status": "running"}
            ev_f.write(json.dumps(running, ensure_ascii=False, default=str) + "\n"); ev_f.flush()
            if bus is not None:
                try: await bus.send({"type": MSG_NODE_EVENT, **running})
                except Exception: bus = None
            try:
                result = await run_node(node_dict, inputs, extra)
            except Exception as e:
                result = {"status": "error", "error_message": str(e)}
            if result.get("status") == "success":
                final_outputs = {node_id: result.get("output")}
                term = {"node_id": node_id, "status": "completed",
                        "result": json.dumps(result.get("output"), default=str, ensure_ascii=False)}
            else:
                msg = result.get("error_message", "") or "node error"
                error_dict = {node_id: msg}
                term = {"node_id": node_id, "status": "error", "error": msg}
            ev_f.write(json.dumps(term, ensure_ascii=False, default=str) + "\n"); ev_f.flush()
            if bus is not None:
                try: await bus.send({"type": MSG_NODE_EVENT, **term})
                except Exception: bus = None
        execution_time = _time.monotonic() - started
        if bus is not None:
            try:
                await bus.send({"type": MSG_RESULT, "final_outputs": final_outputs,
                                "error_dict": error_dict, "execution_time": execution_time})
            except Exception:
                pass
        return final_outputs, error_dict, execution_time
    finally:
        if bus is not None:
            await bus.close()


def run_node_exec(run_root: str, run_id: str) -> int:
    """``kind=="node"`` dispatch: read the self-contained job.json ({node, inputs,
    extra}) and run the single node in-sandbox. Always writes result.json
    (success OR error) so the outcome propagates out. Returns 0 unless a pre-run
    I/O failure (then 1, with an __engine__ result)."""
    exec_dir = _exec_dir(run_root)
    events_path = os.path.join(exec_dir, "events.ndjson")
    try:
        with open(os.path.join(exec_dir, "job.json"), "r", encoding="utf-8") as f:
            job = json.load(f)
        node_dict = job.get("node") or {}
        try:
            inputs = normalize_inputs_for_fields(
                job.get("inputs") or {},
                node_dict.get("input_fields") if isinstance(node_dict, dict) else {},
            )
        except InputNormalizeError as exc:
            # A rejected row is a completed execution result, not a crashed
            # worker. CLI batches must retain it and continue the other rows.
            _write_result(run_root, {}, {"__input__": str(exc)}, 0.0)
            return 0
        extra = job.get("extra") or {}
        bus_sock = os.environ.get(_BUS_SOCK_ENV) or None
        kwargs = {"bus_sock": bus_sock} if bus_sock else {}
        final_outputs, error_dict, t = asyncio.run(
            _drive_node(node_dict, inputs, extra, events_path, **kwargs))
    except Exception as e:
        _write_result(run_root, {}, {"__engine__": str(e)}, 0.0)
        return 1
    _write_result(run_root, final_outputs, error_dict, t)
    return 0


def run_code_exec(run_root: str, run_id: str) -> int:
    """kind=='code': run an arbitrary Python script (job.json 'script') with
    'inputs' piped as JSON on stdin. Capture stdout/stderr/exit into result.json.
    Pure stdlib; never raises (errors -> result.json)."""
    import subprocess, sys, time
    exec_dir = _exec_dir(run_root)
    os.makedirs(exec_dir, exist_ok=True)
    start = time.perf_counter()
    try:
        job = read_job(run_root)
        script = job.get("script") or ""
        inputs = job.get("inputs") or {}
        timeout_s = float(job.get("timeout_s") or 60)
        script_path = os.path.join(exec_dir, "_skill_script.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script)
        proc = subprocess.run(
            [sys.executable, script_path],
            input=json.dumps(inputs, ensure_ascii=False, default=str),
            capture_output=True, text=True, timeout=timeout_s, cwd=run_root,
        )
        final = {"stdout": proc.stdout, "stderr": proc.stderr, "exit_code": proc.returncode}
        err = {"__code__": proc.stderr or f"exit {proc.returncode}"} if proc.returncode != 0 else {}
        _write_result(run_root, final, err, time.perf_counter() - start)
        return 0
    except subprocess.TimeoutExpired:
        _write_result(run_root, {"stdout": "", "stderr": "timeout", "exit_code": -1}, {"__code__": "timeout"}, time.perf_counter() - start)
        return 0
    except Exception as exc:
        _write_result(run_root, {}, {"__engine__": str(exc)}, time.perf_counter() - start)
        return 1


def run_job(run_root: str, run_id: str) -> int:
    """Dispatch by job kind. ``workflow`` → :func:`run_exec` (the whole graph).
    ``node`` → :func:`run_node_exec` (a single node in-sandbox). ``code`` →
    :func:`run_code_exec` (an arbitrary Python script in-sandbox). ``tool`` →
    a clean unsupported-result (exit 2) until its runner lands in a later
    phase. Unknown kind → also unsupported."""
    kind = read_job(run_root).get("kind", JOB_WORKFLOW)
    if kind == JOB_WORKFLOW:
        return run_exec(run_root, run_id)
    if kind == JOB_NODE:
        return run_node_exec(run_root, run_id)
    if kind == JOB_CODE:
        return run_code_exec(run_root, run_id)
    os.makedirs(_exec_dir(run_root), exist_ok=True)
    _write_result(
        run_root, {},
        {"__engine__": f"job kind {kind!r} is not supported in this build"}, 0.0,
    )
    return 2


async def _drive(
    wf: "Workflow",
    inputs: dict,
    run_context: dict,
    events_path: str,
    bus_sock: "str | None" = None,
    cancel_path: "str | None" = None,
) -> tuple[dict, dict, float]:
    """Iterate ``astream`` ONCE: write every event as a line to ``events.ndjson``
    AND accumulate the ``_trigger_inner``-shape ``(final_outputs, error_dict,
    execution_time)`` tuple inline. Single workflow run, both outputs captured.

    When ``bus_sock`` is set, also stream over the host↔sandbox UDS
    message bus while retaining the crash-durable events.ndjson file. The bus is
    connected before ``astream`` is iterated so a future ``inject`` can feed node
    1); each astream event is sent as a ``node_event`` frame, and a terminal
    ``result`` frame is sent at the end. ``bus_sock=None`` → unchanged behavior.

    When ``cancel_path`` is set, a lightweight stdlib-only
    watcher thread polls for that marker file (the host writes it on cancel —
    bind-mounted, so it crosses the process boundary the host ``stop_event``
    cannot). On the marker it sets the astream ``stop_event`` → the producer stops
    at the NEXT node boundary (the running node finishes; no new node starts) —
    COOPERATIVE, not a kill (the warm worker is shared and must survive). The
    accumulated partial ``(final_outputs, error_dict, execution_time)`` is still
    returned so a partial ``result.json`` is written + ``.done`` still fires.
    ``cancel_path=None`` → no watcher, zero behavior change. A marker that
    appears AFTER the run already finished is ignored (the watcher self-terminates
    in the ``finally``)."""
    final_outputs: dict = {}
    error_dict: dict = {}
    started = time.monotonic()

    # Connect before iterating astream so the host has accepted
    # the connection before node 1 runs. A connect failure must NOT crash the run
    # — the events.ndjson + result.json path stays authoritative for the bundle.
    bus = None
    if bus_sock:
        try:
            bus = await connect_bus(bus_sock)
        except Exception:
            bus = None

    # Set the astream stop_event when the cancel marker appears.
    # Engine stays PURE (stdlib os/threading only). The watcher exits when the
    # run ends (the ``finally`` sets ``_watch_done``) so a post-run marker write
    # (B3 "ignore a marker written after .done") never lingers a thread.
    stop_event = asyncio.Event()
    _watch_done = threading.Event()
    watcher: "threading.Thread | None" = None
    if cancel_path:
        loop = asyncio.get_event_loop()

        def _watch() -> None:
            while not _watch_done.wait(0.05):
                try:
                    if os.path.exists(cancel_path):
                        loop.call_soon_threadsafe(stop_event.set)
                        return
                except OSError:
                    pass

        watcher = threading.Thread(target=_watch, daemon=True)
        watcher.start()

    try:
        with open(events_path, "w", encoding="utf-8") as ev_f:
            async for ev in wf.astream(
                inputs, stop_event=stop_event, run_context=run_context
            ):
                # Persist the raw event (one JSON object per line) — crash-durable.
                ev_f.write(json.dumps(ev, ensure_ascii=False, default=str))
                ev_f.write("\n")
                ev_f.flush()

                # ADDITIVE: stream the raw event over the bus VERBATIM so the host
                # reuses its ``to_exec_update`` mapper unchanged.
                if bus is not None:
                    try:
                        await bus.send({"type": MSG_NODE_EVENT, **ev})
                    except Exception:
                        bus = None  # peer gone — fall back to file-only.

                # Mirror _trigger_inner's accumulation EXACTLY.
                status = ev.get("status")
                if status == "finished":
                    final_outputs = ev.get("final_outputs", final_outputs)
                    err_bundle = ev.get("error_dict") or {}
                    if isinstance(err_bundle, dict):
                        error_dict.update(err_bundle)
                elif status == "error":
                    node_key = ev.get("node_id", "__engine__")
                    error_dict[node_key] = ev.get("error_message", "")

        execution_time = time.monotonic() - started
        # The terminal frame is authoritative for the live host SSE. It is sent
        # AFTER the loop so it always follows the last node_event.
        if bus is not None:
            try:
                await bus.send({
                    "type": MSG_RESULT,
                    "final_outputs": final_outputs,
                    "error_dict": error_dict,
                    "execution_time": execution_time,
                })
            except Exception:
                pass
        return final_outputs, error_dict, execution_time
    finally:
        # Signal the cancel watcher to stop (run ended — ignore a marker written
        # after .done, B3) + join it so no daemon thread lingers.
        _watch_done.set()
        if watcher is not None:
            watcher.join(timeout=1.0)
        if bus is not None:
            await bus.close()


def _read_host_extra(exec_dir: str) -> dict:
    """Read host-written ambient ``extra`` (``__exec__/extra.json``) — e.g.
    ``{"llm_credentials": {...}}`` so an in-sandbox PromptNode can resolve a saved
    model name. The api route writes it; the engine stays DB-free. NEVER raises;
    absent/malformed → ``{}``.

    Delete the file immediately after reading it, before
    the engine run starts. CodeNode no longer runs in an in-process jail — user
    code does real ``open(...)`` — so the in-sandbox creds file must not linger
    where user ``open("/run/__exec__/extra.json")`` could read it back. The
    credentials are already loaded into ``run_context`` in-memory by the caller."""
    extra_path = os.path.join(exec_dir, "extra.json")
    try:
        with open(extra_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        out = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        out = {}
    try:
        os.remove(extra_path)
    except OSError:
        pass
    return out


def run_exec(run_root: str, run_id: str) -> int:
    """Read workflow/inputs from ``{run_root}/__exec__/``, run the workflow, and
    write ``result.json`` + ``events.ndjson`` back. Returns 0 on success, 1 on
    an engine-level crash (load / build / run).

    The run executes against ``/run`` at ``run_root``. This directory is a
    temporary host↔sandbox result channel, not persistent Workflow storage.
    Cross-run resources belong to the user-level ``/mount`` namespace."""
    exec_dir = _exec_dir(run_root)
    events_path = os.path.join(exec_dir, "events.ndjson")
    try:
        with open(os.path.join(exec_dir, "workflow.json"), "r", encoding="utf-8") as f:
            wf_dict = json.load(f)
        with open(os.path.join(exec_dir, "inputs.json"), "r", encoding="utf-8") as f:
            raw_inputs = json.load(f)
        try:
            inputs = normalize_inputs_for_fields(raw_inputs, start_node_input_fields(wf_dict))
        except InputNormalizeError as exc:
            _write_result(run_root, {}, {"__input__": str(exc)}, 0.0)
            return 0

        run_context = {"run_id": run_id, "run_dir": run_root}
        # Host-provided ambient ``extra`` written next to workflow.json by the api
        # route (e.g. ``{"llm_credentials": {...}}`` so an in-sandbox PromptNode
        # can resolve a saved model name). The engine stays DB-free — the host did
        # the credential lookup + serialized the mapping here. ``_execute`` does
        # ``extra.update(run_context)``, so merging it into run_context surfaces
        # ``extra['llm_credentials']`` to PromptNode. Absent → unchanged behavior.
        run_context.update(_read_host_extra(exec_dir))
        wf = Workflow(wf_dict)
        # The host sets ``VC_BUS_SOCK`` to the in-sandbox bus socket path
        # when the debug-execute bus is wired; absent → file-only (unchanged). The
        # ``bus_sock`` kwarg is only PASSED when set so the no-bus call keeps the
        # original 4-arg ``_drive`` signature (back-compat).
        bus_sock = os.environ.get(_BUS_SOCK_ENV) or None
        drive_kwargs = {"bus_sock": bus_sock} if bus_sock else {}
        # The host writes ``{exec_dir}/cancel`` to gracefully
        # stop the run at the next node boundary (warm worker survives). Always
        # pass the marker path: the watcher only ever ACTS on the marker's
        # presence, so absent-marker = zero behavior change.
        cancel_path = os.path.join(exec_dir, "cancel")
        final_outputs, error_dict, execution_time = asyncio.run(
            _drive(wf, inputs, run_context, events_path,
                   cancel_path=cancel_path, **drive_kwargs)
        )
    except Exception as e:  # engine-level crash (import/build/run) — no result.
        _write_result(run_root, {}, {"__engine__": str(e)}, 0.0)
        return 1

    _write_result(run_root, final_outputs, error_dict, execution_time)
    return 0


def serve_once(work_dir: str, runs_root: str) -> str | None:
    """Claim + serve ONE ready job from the file-based job channel.

    The warm worker (which imported the engine ONCE at process start) serves
    runs over files on the bind-mounted ``work_dir``:

      - ``{work_dir}/inbox/{job_id}.json``  — ``{"tenant", "run_id", ...}``
      - ``{work_dir}/inbox/{job_id}.ready`` — atomic marker; the host writes it
        LAST (write-then-rename) so its presence guarantees both the job json
        AND the run-tier ``__exec__/*.json`` are fully written.

    Glob ``*.ready``; if none → return ``None``. Otherwise pick one
    deterministically (sorted), CLAIM it atomically by renaming
    ``.ready``→``.taken`` (the rename IS the claim; ``.taken`` is also crash
    visibility — an orphan ``.taken`` marks a job a prior worker died on). Only
    AFTER the claim do we touch the run-tier (the ``.ready`` gate guarantees the
    non-atomic run-tier writes finished). Run via P2 ``run_exec``; on any
    failure write a guard ``result.json`` so the run still has a result. Write
    ``{work_dir}/outbox/{job_id}.done`` LAST (atomic write-then-rename), then
    drop the inbox markers. Return ``job_id``.

    NEVER raises — the serve loop must survive any job, no matter how broken.
    """
    try:
        inbox = os.path.join(work_dir, "inbox")
        ready = sorted(glob.glob(os.path.join(inbox, "*.ready")))
        if not ready:
            return None

        job_id = os.path.splitext(os.path.basename(ready[0]))[0]
        taken_path = os.path.join(inbox, f"{job_id}.taken")
        # The rename is the claim (atomic on POSIX). If it loses a race with
        # another worker, FileNotFoundError → treat as "no job claimed".
        try:
            os.rename(os.path.join(inbox, f"{job_id}.ready"), taken_path)
        except OSError:
            return None

        # Claim succeeded → the host finished writing the run-tier. Read job.
        try:
            with open(os.path.join(inbox, f"{job_id}.json"), "r", encoding="utf-8") as f:
                job = json.load(f)
            tenant = job["tenant"]
            run_id = job["run_id"]
            # Per-tenant warm pools mount ONLY {store_root}/run/{tenant} → /runs,
            # so the run lives at /runs/{run_id} (no tenant prefix). The job then
            # carries an explicit ``run_subpath`` and the loop joins it onto the
            # runs root. Absent → legacy {tenant}/{run_id} (shared-mount).
            sub = job.get("run_subpath")
            if sub is not None and (os.path.isabs(sub) or ".." in sub.split(os.sep)):
                # Descriptor trust boundary (N3): a subpath that escapes the runs
                # root is MALFORMED — skip running it, but still write .done so
                # the host's poll never hangs. NEVER raise.
                raise _MalformedRunSubpath(sub)
            run_root = (
                os.path.join(runs_root, sub) if sub
                else os.path.join(runs_root, tenant, run_id)
            )
            try:
                run_exec(run_root, run_id)
            except Exception as e:  # outer guard — run_exec already self-guards,
                # but the loop must never die even on an unexpected failure.
                try:
                    os.makedirs(_exec_dir(run_root), exist_ok=True)
                    _write_result(run_root, {}, {"__engine__": str(e)}, 0.0)
                except Exception:
                    pass
        except Exception:
            # Malformed job json / missing tenant|run_id — still finish the job
            # (write .done below) so the host never waits forever on it.
            pass

        # Write the .done marker LAST, atomically (temp then rename).
        outbox = os.path.join(work_dir, "outbox")
        os.makedirs(outbox, exist_ok=True)
        done_path = os.path.join(outbox, f"{job_id}.done")
        tmp_done = done_path + ".tmp"
        try:
            with open(tmp_done, "w", encoding="utf-8") as f:
                f.write("")
            os.rename(tmp_done, done_path)
        except Exception:
            pass

        # Drop the inbox markers (best-effort).
        for p in (taken_path, os.path.join(inbox, f"{job_id}.json")):
            try:
                os.remove(p)
            except OSError:
                pass

        return job_id
    except Exception:
        # Absolute backstop — serve_once NEVER raises.
        return None


def serve_loop(work_dir: str, runs_root: str, poll_interval: float = 0.02) -> None:
    """Long-lived serve loop for the warm worker (engine imported ONCE above).

    Orphan ``*.taken`` sweep on entry: a ``.taken`` means a PRIOR worker claimed
    a job and died before finishing it. CHOICE: delete the orphan ``.taken`` and
    leave its ``.json`` so the HOST times out and re-handles that run (the host
    owns retry/cleanup via its outbox poll). This is the simplest crash-recovery
    that keeps the worker from re-running a possibly-half-written run-tier.

    Then poll ``{work_dir}/inbox/*.ready`` via ``serve_once``; ``time.sleep`` the
    poll interval when idle (no busy-spin). Exit cleanly when
    ``{work_dir}/shutdown`` appears (host teardown sentinel).
    """
    inbox = os.path.join(work_dir, "inbox")
    for taken in glob.glob(os.path.join(inbox, "*.taken")):
        try:
            os.remove(taken)
        except OSError:
            pass

    while True:
        if os.path.exists(os.path.join(work_dir, "shutdown")):
            return
        claimed = serve_once(work_dir, runs_root)
        if claimed is None:
            time.sleep(poll_interval)


def main() -> None:
    # Egress proxy: start the in-sandbox forward proxy iff the provider signaled
    # "proxy" mode via env (VC_EGRESS_SOCK + VC_EGRESS_PORT). No-op in dev/host-
    # network mode. Local import keeps the cold-import path lean (matches this
    # file's local-import pattern). The returned proxy runs on a daemon thread for
    # the process lifetime, so it can be ignored.
    from vibecanvas_engine.egress_proxy import maybe_start_egress_proxy
    maybe_start_egress_proxy()  # no-op unless the provider signaled proxy mode

    # ``serve`` mode: long-lived warm worker over the file-job channel. run_ids
    # are uuids and never literally "serve", so this argv dispatch is safe.
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        serve_loop(sys.argv[2], sys.argv[3])
        return
    # P2 single-run path: workflow runs bind the run-tier at /run. Agent code
    # jobs may bind the same internal channel at a hidden mount so pure Chat
    # sessions do not expose /run to user commands.
    sys.exit(run_job(os.environ.get("VIBECANVAS_RUN_ROOT", "/run"), sys.argv[1]))


if __name__ == "__main__":
    main()
