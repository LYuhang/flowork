"""Batch result observation and isolated, durable evaluation.

Private configuration and records live in the Task's encrypted document. Queue
arguments contain only IDs; the task lock serializes evaluation and resume.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator

from vibecanvas_api.services.object_store import get_task_result_store, uri_to_key
from vibecanvas_api.storage.repo_tasks import TasksRepo

TERMINAL = {"finished", "finished_with_errors", "failed", "interrupted", "cancelled"}
ALLOWED_MODULES = {"math", "statistics", "collections", "re", "json", "decimal", "fractions", "itertools", "functools", "operator", "string"}
MAX_RESULT_BYTES = 32 * 1024 * 1024


def validate_script(script: str) -> None:
    if not script.strip():
        raise ValueError("Evaluation script is required.")
    try:
        tree = ast.parse(script)
    except SyntaxError as exc:
        raise ValueError(f"Invalid Python syntax at line {exc.lineno}.") from exc
    if not any(isinstance(n, ast.FunctionDef) and n.name == "evaluate" for n in tree.body):
        raise ValueError("Define evaluate(results) returning a metrics dictionary.")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [n.name for n in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""] if not node.level else [""]
        else:
            continue
        if any(name not in ALLOWED_MODULES for name in names):
            raise ValueError("Only approved standard-library imports are allowed: " + ", ".join(sorted(ALLOWED_MODULES)))


class EvaluationConfig(BaseModel):
    enabled: bool = False
    script: str = Field(default="", max_length=65536)

    @model_validator(mode="after")
    def check_script(self):
        if self.enabled or self.script.strip():
            validate_script(self.script)
        return self


def result_uri(task) -> str:
    if task.task_type != "batch_exec" or task.status not in TERMINAL:
        raise HTTPException(409, "Batch results are available after inference finishes.")
    uri = ((task.result or {}).get("artifact_uris") or {}).get("jsonl")
    if not isinstance(uri, str) or not uri:
        raise HTTPException(404, "No result file is available.")
    return uri


def load_results(uri: str, *, store=None) -> tuple[list[dict], str]:
    # Bound memory independently of the client-requested page size.
    chunks, size = [], 0
    try:
        for chunk in (store or get_task_result_store()).iter_bytes(uri_to_key(uri)):
            size += len(chunk)
            if size > MAX_RESULT_BYTES:
                raise HTTPException(413, "Result exceeds the 32 MiB interactive evaluation limit; use Download.")
            chunks.append(chunk)
    except KeyError as exc:
        raise HTTPException(404, "No result file is available.") from exc
    raw = b"".join(chunks)
    try:
        rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("invalid result row")
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(422, "Stored results are not valid JSONL objects.") from exc
    return rows, hashlib.sha256(raw).hexdigest()


def active_evaluation(task) -> bool:
    return any(r.get("status") in {"queued", "running"} for r in (task.payload or {}).get("evaluations", []))


async def queue_evaluation(session, task, *, automatic=False):
    from vibecanvas_api.services.background_queue import enqueue_background_job_in_transaction
    uri = result_uri(task)
    payload = dict(task.payload or {})
    configuration = EvaluationConfig.model_validate(payload.get("evaluation") or {})
    validate_script(configuration.script)
    if active_evaluation(task):
        raise HTTPException(409, "An evaluation is already queued or running.")
    # Resume takes the same row lock, so this version cannot change under us.
    rows, version = await asyncio.to_thread(load_results, uri)
    record = {
        "id": str(uuid.uuid4()), "status": "queued", "script": configuration.script,
        "result_version": version, "row_count": len(rows), "automatic": automatic,
        "partial": task.status not in {"finished", "finished_with_errors"},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "metrics": None, "error": None,
    }
    payload.pop("evaluation_queue_error", None)
    payload["evaluations"] = [record, *payload.get("evaluations", [])]
    await TasksRepo(session).update_status(task.id, payload=payload)
    await enqueue_background_job_in_transaction(
        session, "batch_evaluation", job_id=record["id"], queue="interactive",
        kwargs={"task_id": str(task.id), "evaluation_id": record["id"]},
    )
    await TasksRepo(session).insert_event(task.id, "log", evaluation_log(record), task.tenant_id)
    return record


def evaluation_log(record):
    status = record["status"]
    return {
        "schema_version": 1, "category": "task",
        "level": "error" if status == "failed" else "info",
        "action": "evaluation." + status,
        "message": "Batch evaluation " + status + ".",
        "data": {"evaluation_id": record["id"], **{key: record.get(key) for key in
                 ("status", "metrics", "error", "row_count", "result_version", "automatic")}},
    }


# This runs only inside a fresh OS sandbox, with network disabled and no user
# mount. Restricted builtins/imports express the feature contract, not the
# isolation boundary. Printed text is discarded; protocol output is bounded.
EVALUATOR = r'''
import sys, json, resource, builtins, contextlib
resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
job = json.load(sys.stdin)
allowed = set(job['allowed_modules'])
original_import = builtins.__import__
def checked_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level or name not in allowed:
        raise ImportError('Only approved standard-library imports are allowed.')
    return original_import(name, globals, locals, fromlist, level)
safe = {k: v for k, v in vars(builtins).items() if k not in {
    'open', 'eval', 'exec', 'compile', 'input', 'breakpoint', 'help',
    'globals', 'locals', 'vars', '__import__', 'exit', 'quit',
}}
safe['__import__'] = checked_import
class Sink:
    def write(self, value): return len(value)
    def flush(self): pass
try:
    scope = {'__builtins__': safe, '__name__': 'evaluation'}
    with contextlib.redirect_stdout(Sink()), contextlib.redirect_stderr(Sink()):
        exec(compile(job['script'], '<evaluation>', 'exec'), scope)
        metrics = scope['evaluate'](job['results'])
    if not isinstance(metrics, dict):
        raise ValueError('evaluate must return a dictionary.')
    encoded = json.dumps(metrics, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode('utf-8')) > 65536:
        raise ValueError('Metrics exceed the 64 KiB limit.')
    answer = {'metrics': metrics}
except BaseException as exc:
    answer = {'error': type(exc).__name__ + ': ' + str(exc)[:2000]}
print(json.dumps(answer, ensure_ascii=False, allow_nan=False))
'''


async def run_evaluation(task_id: str, evaluation_id: str):
    from vibecanvas_api.storage.sync_session import short_admin_session
    from vibecanvas_api.services.sandbox.coordinator import get_sandbox_coordinator, dispose_sandbox_rpc_client
    async with short_admin_session() as db:
        repo = TasksRepo(db)
        task = await repo.get(uuid.UUID(task_id), for_update=True)
        if task is None:
            return
        payload = dict(task.payload or {})
        record = next((r for r in payload.get("evaluations", []) if r["id"] == evaluation_id), None)
        if not record or record["status"] not in {"queued", "running"}:
            return
        tenant, user = str(task.tenant_id), str(task.user_id)
        uri = result_uri(task)
        record.update(status="running", started_at=datetime.now(timezone.utc).isoformat())
        await repo.update_status(task.id, payload=payload)
    coordinator = get_sandbox_coordinator()
    scope = "eval-" + evaluation_id
    metrics, error = None, None
    try:
        rows, version = await asyncio.to_thread(load_results, uri)
        if version != record["result_version"]:
            raise ValueError("Result version changed; start a new evaluation.")
        validate_script(record["script"])
        sandbox = await coordinator.get_session(tenant, scope, user_id=user, expose_mount=False, expose_runtime=False)
        result = await sandbox.run_code(EVALUATOR, {
            "script": record["script"], "results": rows, "allowed_modules": sorted(ALLOWED_MODULES),
        }, timeout_s=30, network="none")
        if result.get("exit_code") != 0:
            raise ValueError("Evaluation exceeded its time or memory limit, or the sandbox could not execute it.")
        answer = json.loads(result.get("stdout") or "{}")
        metrics, error = answer.get("metrics"), answer.get("error")
        if not error and not isinstance(metrics, dict):
            raise ValueError("Evaluation returned invalid metrics.")
    except Exception as exc:
        error = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
        if not isinstance(exc, (ValueError, HTTPException)):
            error = "Evaluation sandbox failed. Please retry."
    finally:
        try:
            await coordinator.close_session(tenant, scope)
        except Exception:
            pass
        await dispose_sandbox_rpc_client()
    async with short_admin_session() as db:
        repo = TasksRepo(db)
        task = await repo.get(uuid.UUID(task_id), for_update=True)
        if task is None:
            return
        payload = dict(task.payload or {})
        target = next((r for r in payload.get("evaluations", []) if r["id"] == evaluation_id), None)
        if target:
            target.update(status="failed" if error else "succeeded", metrics=metrics if not error else None,
                          error=error, finished_at=datetime.now(timezone.utc).isoformat())
            await repo.update_status(task.id, payload=payload)
            await repo.insert_event(task.id, "log", evaluation_log(target), task.tenant_id)


def evaluation_job(*, task_id: str, evaluation_id: str):
    asyncio.run(run_evaluation(task_id, evaluation_id))
