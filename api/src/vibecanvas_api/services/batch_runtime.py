"""Shared batch workflow runtime.

This module owns the reusable batch execution contract used by Task-page batch
runs and workflow-page batch runs:

* one task-scoped run workspace
* one serve-parallel sandbox lifecycle per batch execution
* row-level runtime ledger during execution
* ordered final artifacts after terminal state

The caller owns DB task status/events. This module only runs rows and returns a
summary plus artifact URIs.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from vibecanvas_api.authorization.types import ResourceType
from vibecanvas_api.services.batch_output import build_output_sink, serialize_results
from vibecanvas_api.services.llm_credentials_inject import inject_into_run_context_async
from vibecanvas_api.services.object_store import get_object_store, uri_to_key
from vibecanvas_api.services.sandbox.coordinator import get_sandbox_coordinator
from vibecanvas_api.services.workflow_sandbox_runner import ensure_code_pythonpath

_ROW_STATUS_RERUN = {
    "error",
    "timeout",
    "cancelled",
    "not_started",
    "worker_crashed",
    "sandbox_crashed",
}


@dataclass
class BatchProgress:
    index: int
    status: str
    done: int
    total: int
    row: dict


@dataclass
class BatchRunResult:
    status: str
    summary: dict
    results_uri: str | None
    artifact_uris: dict[str, str]
    rows: list[dict]


ProgressCallback = Callable[[BatchProgress], Awaitable[None] | None]


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def input_hash(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def normalize_batch_inputs(rows: list[dict], column_mapping: dict) -> list[dict]:
    mapped: list[dict] = []
    for row in rows:
        mapped.append({column_mapping.get(k, k): v for k, v in row.items()})
    return mapped


def _attempt_for(previous: dict | None) -> int:
    try:
        return int((previous or {}).get("attempt") or 0) + 1
    except Exception:
        return 1


def _flatten_error(error: object) -> str:
    if error is None:
        return ""
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        message = error.get("message")
        return str(message if message is not None else error)
    return str(error)


def _row_from_result(
    *,
    index: int,
    original_input: dict,
    mapped_input: dict,
    previous: dict | None,
    result_json: dict | None,
    status_result: dict | None,
) -> dict:
    attempt = _attempt_for(previous)
    ih = input_hash(mapped_input)
    started_at = datetime.now(timezone.utc).isoformat()
    if (status_result or {}).get("status") == "cancelled":
        return {
            "schema_version": 1,
            "i": index,
            "index": index,
            "input_hash": ih,
            "status": "cancelled",
            "attempt": attempt,
            "input": original_input,
            "mapped_input": mapped_input,
            "output": None,
            "error": {
                "code": "cancelled",
                "message": str((status_result or {}).get("error_message") or "cancelled by user"),
            },
            "execution_time": None,
            "elapsed_ms": None,
            "ok": False,
            "previous_status": (previous or {}).get("status"),
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
    if result_json is None:
        error_message = (
            (status_result or {}).get("error_message")
            or (status_result or {}).get("error")
            or "workflow row produced no result"
        )
        return {
            "schema_version": 1,
            "i": index,
            "index": index,
            "input_hash": ih,
            "status": "error",
            "attempt": attempt,
            "input": original_input,
            "mapped_input": mapped_input,
            "output": None,
            "error": {
                "code": "row_execution_error",
                "message": str(error_message),
            },
            "execution_time": None,
            "elapsed_ms": None,
            "ok": False,
            "previous_status": (previous or {}).get("status"),
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }

    outputs = result_json.get("final_outputs") or {}
    errors = result_json.get("error_dict") or {}
    has_errors = bool(errors)
    exec_time = result_json.get("execution_time")
    return {
        "schema_version": 1,
        "i": index,
        "index": index,
        "input_hash": ih,
        "status": "error" if has_errors else "success",
        "attempt": attempt,
        "input": original_input,
        "mapped_input": mapped_input,
        "output": outputs,
        "error": (
            {
                "code": "workflow_error",
                "message": "; ".join(f"{k}: {v}" for k, v in errors.items()),
                "details": errors,
            }
            if has_errors
            else None
        ),
        "execution_time": exec_time,
        "elapsed_ms": None if exec_time is None else int(float(exec_time) * 1000),
        "ok": not has_errors,
        "previous_status": (previous or {}).get("status"),
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


def _placeholder_row(index: int, original_input: dict, mapped_input: dict, status: str, message: str) -> dict:
    return {
        "schema_version": 1,
        "i": index,
        "index": index,
        "input_hash": input_hash(mapped_input),
        "status": status,
        "attempt": 0,
        "input": original_input,
        "mapped_input": mapped_input,
        "output": None,
        "error": {"code": status, "message": message},
        "execution_time": None,
        "elapsed_ms": None,
        "ok": False,
    }


def _load_previous_results(uri: str | None) -> dict[int, dict]:
    if not uri:
        return {}
    try:
        data = get_object_store().fetch_bytes(uri_to_key(uri)).decode("utf-8")
    except Exception:
        return {}
    out: dict[int, dict] = {}
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            out[int(row["index"])] = row
        except Exception:
            continue
    return out


def _should_reuse(previous: dict | None, mapped_input: dict) -> bool:
    if not previous:
        return False
    if previous.get("status") != "success":
        return False
    return previous.get("input_hash") == input_hash(mapped_input)


def _results_jsonl(rows: list[dict]) -> bytes:
    text = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows)
    return (text + ("\n" if text else "")).encode("utf-8")


def _summary(
    *,
    task_id: str,
    workflow_id: str,
    rows: list[dict],
    resume_count: int,
    rerun_rows: int,
    reused_success_rows: int,
    artifact_uris: dict[str, str],
    output_path: str | None = None,
    output_error: str | None = None,
) -> dict:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.get("status") or "unknown"] = counts.get(row.get("status") or "unknown", 0) + 1
    failed = sum(v for k, v in counts.items() if k != "success")
    task_status = "finished" if failed == 0 else "finished_with_errors"
    summary = {
        "task_id": task_id,
        "workflow_id": workflow_id,
        "task_status": task_status,
        "rows_total": len(rows),
        "rows_ok": counts.get("success", 0),
        "rows_failed": failed,
        "success": counts.get("success", 0),
        "error": counts.get("error", 0),
        "timeout": counts.get("timeout", 0),
        "cancelled": counts.get("cancelled", 0),
        "not_started": counts.get("not_started", 0),
        "worker_crashed": counts.get("worker_crashed", 0),
        "sandbox_crashed": counts.get("sandbox_crashed", 0),
        "resume_count": resume_count,
        "rerun_rows": rerun_rows,
        "reused_success_rows": reused_success_rows,
        "can_resume": failed > 0,
        "resume_policy": "skip_success",
        "artifact_uris": artifact_uris,
        "sample_errors": [
            f"row {r['index']}: {_flatten_error(r.get('error'))}"
            for r in rows
            if r.get("status") != "success" and r.get("error")
        ][:5],
    }
    if output_path:
        summary["output_path"] = output_path
    if output_error:
        summary["output_error"] = output_error
    return summary


async def run_batch_workflow(
    *,
    task_id: str,
    tenant_id: str,
    user_id: str,
    workflow_id: str,
    workflow: dict,
    rows: list[dict],
    column_mapping: dict,
    output: dict | None = None,
    output_columns: list | None = None,
    concurrency: int = 1,
    previous_results_uri: str | None = None,
    resume_count: int = 0,
    on_progress: ProgressCallback | None = None,
    stop_event: object | None = None,
    execution_principal_type: str = "user",
    execution_principal_id: str | None = None,
    execution_principal_generation: int = 0,
    prepared_run_extra: dict | None = None,
) -> BatchRunResult:
    """Run a batch workflow through one sandbox lifecycle.

    This is the shared runtime. It does not update the `tasks` table and does not
    emit task events; callers provide `on_progress` for that.
    """
    original_rows = rows or []
    mapped_rows = normalize_batch_inputs(original_rows, column_mapping or {})
    previous = _load_previous_results(previous_results_uri)
    merged: dict[int, dict] = {}
    jobs = []
    job_meta: list[tuple[int, dict, dict]] = []
    workers = max(1, min(int(concurrency or 1), 16))
    done = 0
    if prepared_run_extra is None:
        injection_claims = {
            "user_id": user_id,
            "workflow_id": workflow_id,
            "execution_id": task_id,
            "execution_resource_type": ResourceType.TASK.value,
        }
        if execution_principal_type != "user":
            injection_claims.update({
                "principal_type": execution_principal_type,
                "principal_id": execution_principal_id,
                "principal_generation": execution_principal_generation,
            })
        run_extra = await inject_into_run_context_async(
            {}, workflow, tenant_id, **injection_claims,
        )
    else:
        # background worker's synchronous task body prepares credentials through the
        # short-session sync bridge before opening this asyncio.run loop. That
        # prevents the process-global async DB pool from leaking a Future bound
        # to an earlier worker loop into this batch loop.
        run_extra = dict(prepared_run_extra)

    # Batch rows execute the same Workflow contract as interactive runs. Ensure
    # the incremental package layer once per batch, then reuse its read-only,
    # content-addressed path for every row job. The idempotent builder makes a
    # warm run a lookup and lets a cold Task recover without a manual editor run.
    code_pythonpath = await ensure_code_pythonpath(workflow)
    if code_pythonpath:
        run_extra["code_pythonpath"] = code_pythonpath
    else:
        run_extra.pop("code_pythonpath", None)

    # A batch gets a daemon-owned, task-scoped workspace.  API/background worker processes
    # pass logical payloads over gRPC and never stage files in a shared host
    # directory.  This remains valid when sandboxd moves to another node and
    # materializes the same object-store prefix locally.
    batch_scope_id = f"batch-{task_id}"
    coordinator = get_sandbox_coordinator()
    session = await coordinator.get_session(
        tenant_id,
        batch_scope_id,
        user_id=user_id,
        expose_run=True,
        expose_runtime=False,
        lease="interactive",
    )
    try:
        for i, (orig, mapped) in enumerate(zip(original_rows, mapped_rows)):
            prev = previous.get(i)
            if _should_reuse(prev, mapped):
                merged[i] = prev
                continue
            sub = f"batch/jobs/{i:06d}"
            jobs.append({
                "kind": "workflow",
                "tenant": tenant_id or "",
                "run_id": f"{task_id}_{i}",
                "run_subpath": sub,
                "inputs": mapped,
            })
            job_meta.append((i, orig, mapped))

        semaphore = asyncio.Semaphore(workers)

        def _stop_requested() -> bool:
            return (
                stop_event is not None
                and getattr(stop_event, "is_set", lambda: False)()
            )

        def _cancelled_outcome() -> dict:
            return {
                "status": {
                    "status": "cancelled",
                    "error_message": "cancelled by user",
                },
                "result": None,
            }

        async def _run_one(job_pos: int, job: dict) -> None:
            nonlocal done
            row_index, orig, mapped = job_meta[job_pos]
            async with semaphore:
                # Coroutines are created for every row up front. Check after
                # acquiring the concurrency slot so rows queued behind active
                # work observe a cancellation requested while they waited.
                if _stop_requested():
                    outcome = _cancelled_outcome()
                else:
                    try:
                        outcome = await session.execute_workflow_job(
                            workflow=workflow,
                            inputs=job["inputs"],
                            extra=run_extra,
                            tenant=job["tenant"],
                            run_id=job["run_id"],
                            run_subpath=job["run_subpath"],
                            timeout=600.0,
                        )
                    except Exception as exc:  # noqa: BLE001 - isolate row failures
                        outcome = {
                            "status": {
                                "status": "error",
                                "error_message": str(exc),
                            },
                            "result": None,
                        }

                    # Soft cancellation lets the in-flight sandbox operation
                    # reach a safe boundary, but cancellation must still win
                    # over its late success/error result. This keeps the Task
                    # resumable and prevents a business error from replacing
                    # the user's cancellation request.
                    if _stop_requested():
                        outcome = _cancelled_outcome()
            status_result = outcome.get("status") or {}
            row = _row_from_result(
                index=row_index,
                original_input=orig,
                mapped_input=mapped,
                previous=previous.get(row_index),
                result_json=outcome.get("result"),
                status_result=status_result,
            )
            merged[row_index] = row
            done += 1
            if on_progress is not None:
                maybe = on_progress(BatchProgress(
                    index=row_index,
                    status=row["status"],
                    done=done,
                    total=len(jobs),
                    row=row,
                ))
                if asyncio.iscoroutine(maybe):
                    await maybe

        if jobs:
            await asyncio.gather(*[
                _run_one(index, job) for index, job in enumerate(jobs)
            ])

        final_rows: list[dict] = []
        for i, (orig, mapped) in enumerate(zip(original_rows, mapped_rows)):
            final_rows.append(
                merged.get(i)
                or _placeholder_row(i, orig, mapped, "not_started", "Task ended before this row was scheduled.")
            )

        final_jsonl = _results_jsonl(final_rows)
        final_csv, csv_content_type = serialize_results(
            final_rows,
            path="results.csv",
            columns=output_columns,
        )
        summary_probe = {
            "task_id": task_id,
            "workflow_id": workflow_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        store = get_object_store()
        artifact_uris = {
            "jsonl": store.put_bytes(
                f"tasks/{task_id}/results.jsonl",
                final_jsonl,
                content_type="application/jsonl",
            ),
            "csv": store.put_bytes(
                f"tasks/{task_id}/results.csv",
                final_csv,
                content_type=csv_content_type,
            ),
        }

        output_path = None
        output_error = None
        try:
            sink = build_output_sink(
                output,
                wf_id=workflow_id,
                tenant_id=tenant_id,
                default_name=f"batch-{task_id}.csv",
                columns=output_columns,
            )
            if sink is not None:
                output_path = sink.write_rows(final_rows)
        except Exception as exc:  # noqa: BLE001
            output_error = str(exc)

        summary = _summary(
            task_id=task_id,
            workflow_id=workflow_id,
            rows=final_rows,
            resume_count=resume_count,
            rerun_rows=len(jobs),
            reused_success_rows=len(original_rows) - len(jobs),
            artifact_uris=artifact_uris,
            output_path=output_path,
            output_error=output_error,
        )
        summary.update(summary_probe)
        if any(row.get("status") == "cancelled" for row in final_rows):
            summary["task_status"] = "interrupted"
            summary["can_resume"] = True
        summary_bytes = json.dumps(
            summary,
            ensure_ascii=False,
            default=str,
            indent=2,
        ).encode("utf-8")
        artifact_uris["summary"] = store.put_bytes(
            f"tasks/{task_id}/summary.json",
            summary_bytes,
            content_type="application/json",
        )
        summary["artifact_uris"] = artifact_uris
        # Storage lists Task artifacts without downloading them. Persist the
        # plaintext byte counts alongside the durable URIs so encrypted local
        # objects and remote S3 objects can report an accurate size without a
        # full read (which may be very expensive for a large batch result).
        summary["artifact_sizes"] = {
            "jsonl": len(final_jsonl),
            "csv": len(final_csv),
            "summary": len(summary_bytes),
        }

        return BatchRunResult(
            status=summary["task_status"],
            summary=summary,
            results_uri=artifact_uris["csv"],
            artifact_uris=artifact_uris,
            rows=final_rows,
        )
    finally:
        # Results are uploaded to the durable Object Store before release.  The
        # task workspace is transient and must not become a second persistence
        # mechanism alongside VFS/Object Storage.
        await coordinator.close_session(tenant_id, batch_scope_id)
