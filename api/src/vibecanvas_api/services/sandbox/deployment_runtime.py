"""sandboxd-owned resident revisions; business admission is durable in PostgreSQL."""
from __future__ import annotations

import asyncio
import copy
import contextlib
import os
import time
import uuid

from vibecanvas_api.services.deployment_revisions import revision_scope
from vibecanvas_api.services.deployment_completion import complete_before_cancelling

LEASE_HEARTBEAT_SECONDS = 10


class DeploymentRuntime:
    def __init__(self, manager):
        self.manager = manager
        from vibecanvas_api.services.deployment_workspace import DeploymentWorkspaces
        self._workspaces = DeploymentWorkspaces()
        self._locks: dict[str, asyncio.Lock] = {}
        self._ready: dict[str, object] = {}
        self._active_requests: dict[str, int] = {}
        self._resources = None
        self._terminals: dict[str, dict] = {}
        self._invocations: dict[tuple[str, str], asyncio.Task] = {}
        self._dispatches: dict[tuple[str, str], asyncio.Task] = {}

    async def start(self, **kwargs) -> dict:
        """Acknowledge durable ownership without occupying a queue worker.

        Losing this RPC never cancels the separately owned execution. Retries
        inspect the original claim and never replay an already claimed run.
        """
        from sqlalchemy import text
        from vibecanvas_api.storage.db import short_session_scope

        tenant_id, run_id = kwargs["tenant_id"], kwargs["run_id"]
        key = (tenant_id, run_id)
        while True:
            async with short_session_scope(tenant_id=tenant_id) as db:
                row = (await db.execute(text("""SELECT runtime_claim,status
                    FROM deployment_invocations WHERE id=:id AND deployment_id=:deployment
                    AND revision_id=:revision"""), {
                    "id": uuid.UUID(run_id), "deployment": uuid.UUID(kwargs["deployment_id"]),
                    "revision": uuid.UUID(kwargs["revision_id"]),
                })).mappings().one_or_none()
            if row is None:
                raise RuntimeError("deployment_revision_not_admitted")
            if row["runtime_claim"] is not None or row["status"] not in {"queued", "running"}:
                return {"invocation_id": run_id, "accepted": True}
            task = self._dispatches.get(key)
            if task is None:
                task = asyncio.create_task(self.run(**kwargs))
                self._dispatches[key] = task

                def finished(done):
                    if self._dispatches.get(key) is done:
                        self._dispatches.pop(key, None)
                    if not done.cancelled():
                        done.exception()  # Completion is recorded by the runtime.

                task.add_done_callback(finished)
            await asyncio.wait({task}, timeout=0.05)
            if task.done():
                task.result()  # Propagate a failure before ownership was claimed.
                return {"invocation_id": run_id, "accepted": True}

    async def prepare(self, *, tenant_id: str, revision_id: str, spec: dict, workflow: dict):
        from vibecanvas_api.services.workflow_sandbox_runner import prepare_code_pythonpath
        from vibecanvas_api.services.sandbox.bubblewrap import BubblewrapProvider
        from vibecanvas_api.services.sandbox.resource_limits import ResourceAllocator, ResourceBudget
        key = f"{tenant_id}:{revision_id}"
        async with self._locks.setdefault(key, asyncio.Lock()):
            previous = self._ready.get(key)
            if previous is not None:
                pool = previous._fileop_pool
                execution_pool = getattr(previous, "_workflow_rpc_pool", None)
                if (not previous.closed and pool is not None and pool._handles
                        and pool._handles[0].proc.poll() is None
                        and execution_pool is not None and execution_pool.accepting):
                    return previous
                if execution_pool is not None and execution_pool.busy:
                    raise RuntimeError("deployment_instance_unavailable")
                await self.manager.close_session(tenant_id, revision_scope(revision_id))
                if self._resources and not self._resources.release(revision_id):
                    raise RuntimeError('deployment_resource_instance_still_running')
                self._ready.pop(key, None)
            if self._resources is None:
                self._resources = ResourceAllocator.from_environment()
            group = self._resources.reserve(revision_id, ResourceBudget(
                cpu_millis=spec.get('cpu_millis', 500), memory_mb=spec.get('memory_mb', 256)))
            try:
                session = await self.manager.get_session(
                    tenant_id, revision_scope(revision_id), user_id=spec["user_id"],
                    expose_run=True, expose_runtime=False,
                    expose_mount=spec["mount_enabled"], lease="resident",
                    workspace_profile="execution",
                )
                if not isinstance(session.provider, BubblewrapProvider):
                    raise RuntimeError('deployment_resource_provider_unsupported')
                # Session-local clone: never apply a deployment's limits to a
                # sibling Chat or another instance through the shared provider.
                session.provider = copy.copy(session.provider)
                session.provider.resource_group = group
                from vibecanvas_api.services.deployment_resource_preflight import validate_deployment_resources
                await validate_deployment_resources(tenant_id=tenant_id, revision_id=revision_id,
                                                    spec=spec, workflow=workflow, sandbox_session=session)
                # Resolve ownership from the durable revision, never from an RPC path.
                from sqlalchemy import text
                from vibecanvas_api.storage.db import short_session_scope
                async with short_session_scope(tenant_id=tenant_id) as db:
                    deployment_id = str(await db.scalar(text(
                        "SELECT deployment_id FROM deployment_runtime_revisions WHERE id=:id"
                    ), {"id": uuid.UUID(revision_id)}))
                workspace = await self._workspaces.acquire(tenant_id, deployment_id, revision_id)
                session._deployment_workspace = workspace
                session.workflow_run_dir = str(workspace.root)
                session.workflow_run_id = workspace.run_id

                async def release_workspace():
                    await self._workspaces.release(tenant_id, deployment_id, revision_id)
                session._release_deployment_workspace = release_workspace
                await prepare_code_pythonpath(workflow, session=session)
                await session.prewarm_fileops()
                from .workflow_rpc_pool import WorkflowRpcPool
                session._workflow_rpc_pool = WorkflowRpcPool.for_session(
                    session=session, revision=revision_id, workflow=workflow,
                    capacity=(-1 if spec.get("worker_concurrency", -1) == -1 else int(spec.get("worker_count", 1)) * int(spec["worker_concurrency"])),
                    worker_count=int(spec.get("worker_count", 1)),
                    artifacts_root=str(workspace.root),
                )
                await session._workflow_rpc_pool.prewarm()
            except BaseException:
                await self.manager.close_session(tenant_id, revision_scope(revision_id))
                self._resources.release(revision_id)
                raise
            self._ready[key] = session
            return session

    @complete_before_cancelling
    async def run(self, *, tenant_id: str, deployment_id: str, revision_id: str,
                  workflow: dict, inputs: dict, run_id: str, extra: dict | None = None,
                  resource_claims: dict | None = None, **unused) -> dict:
        # A disconnected RPC may cause the API's durable invocation to become
        # terminal before its sandbox worker exits. Keep an independent local
        # drain lease until execution and credential cleanup are confirmed.
        key = f"{tenant_id}:{revision_id}"
        invocation_key = (tenant_id, run_id)
        if invocation_key in self._invocations:
            raise RuntimeError("deployment_invocation_already_owned")
        self._invocations[invocation_key] = asyncio.current_task()
        self._active_requests[key] = self._active_requests.get(key, 0) + 1
        try:
            return await self._run_admitted(
                tenant_id=tenant_id, deployment_id=deployment_id, revision_id=revision_id,
                workflow=workflow, inputs=inputs, run_id=run_id, extra=extra,
                resource_claims=resource_claims,
            )
        finally:
            self._invocations.pop(invocation_key, None)
            remaining = self._active_requests[key] - 1
            if remaining:
                self._active_requests[key] = remaining
            else:
                self._active_requests.pop(key, None)

    async def stop_expired_invocation(self, *, tenant_id: str, revision_id: str, invocation_id: str) -> bool:
        """A lease deadline is not evidence that side effects stopped."""
        owner = self._invocations.get((tenant_id, invocation_id))
        if owner is not None:
            owner.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.gather(owner, return_exceptions=True)), timeout=15)
            except TimeoutError:
                return False
        key = f"{tenant_id}:{revision_id}"
        async with self._locks.setdefault(key, asyncio.Lock()):
            session = self._ready.get(key)
            if session is not None:
                pool = getattr(session, "_workflow_rpc_pool", None)
                if pool is None:
                    return False
                for slot in pool._slots.values():
                    if slot.invocation_id == invocation_id and slot.alive:
                        await slot.close()
                        if slot.invocation_id is not None:
                            return False
                # New pools are created only after any previous revision
                # cgroup is empty. No matching owner/process means this old
                # invocation cannot continue on this daemon's current pool.
                return (tenant_id, invocation_id) not in self._invocations
            # After daemon restart there is no in-memory owner. The cgroup is
            # the OS evidence: a populated orphan must keep its capacity until
            # --die-with-parent shutdown is complete. Never replay the call.
            if self._resources is None:
                from .resource_limits import ResourceAllocator
                self._resources = ResourceAllocator.from_environment()
            return await asyncio.to_thread(self._resources.release, revision_id)

    async def _run_admitted(self, *, tenant_id: str, deployment_id: str, revision_id: str,
                            workflow: dict, inputs: dict, run_id: str, extra: dict | None,
                            resource_claims: dict | None = None) -> dict:
        from sqlalchemy import text
        from vibecanvas_api.storage.db import short_session_scope
        claim = uuid.uuid4()
        # No arbitrary RPC-supplied graph may select another deployment's pool.
        async with short_session_scope(tenant_id=tenant_id) as db:
            row = (await db.execute(text("""SELECT r.spec,i.timeout_seconds,i.submitted_at FROM deployment_runtime_revisions r
                JOIN deployment_invocations i ON i.revision_id=r.id
                WHERE r.id=:revision AND r.deployment_id=:deployment
                AND i.id=:invocation AND i.status IN ('queued','running')
                AND i.runtime_claim IS NULL
                AND (i.dispatch_deadline IS NULL OR i.dispatch_deadline > now())
                AND r.state IN ('active','draining') FOR UPDATE OF i"""),
                {"revision": uuid.UUID(revision_id), "deployment": uuid.UUID(deployment_id),
                 "invocation": uuid.UUID(run_id)})).mappings().one_or_none()
            if row is not None:
                await db.execute(text("""UPDATE deployment_invocations SET runtime_claim=:claim,
                    execution_lease_until=now()+interval '60 seconds', dispatch_deadline=NULL,
                    status='running', started_at=COALESCE(started_at,now()) WHERE id=:id"""),
                    {'claim': claim, 'id': uuid.UUID(run_id)})
        if row is None:
            raise RuntimeError("deployment_revision_not_admitted")
        started = time.perf_counter()
        result = None
        owner = asyncio.current_task()
        heartbeat = asyncio.create_task(self._maintain_execution_lease(tenant_id, run_id, claim, owner))
        try:
            session = await self.prepare(tenant_id=tenant_id, revision_id=revision_id,
                                         spec=row["spec"], workflow=workflow)
            from vibecanvas_api.services.workflow_approvers import resolve_workflow_approvers
            from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo
            async with short_session_scope(tenant_id=tenant_id) as db:
                history = WorkflowHistoryRepo(db)
                if await history.get(run_id) is None:
                    approvers = await resolve_workflow_approvers(
                        db, tenant_id=tenant_id, workflow=workflow,
                        initiator_user_id=row["spec"]["user_id"], require_explicit=True,
                    )
                    await history.create(
                        execution_id=run_id, tenant_id=tenant_id, wf_id=row["spec"]["wf_id"],
                        source_type="deployment", source_id=deployment_id,
                        initiator_user_id=row["spec"]["user_id"], workflow=workflow,
                        inputs=inputs, approvers=approvers, revision_id=revision_id,
                    )
            runtime_extra = dict(extra or {})
            # Admission captures the budget; edits never change in-flight work.
            from datetime import datetime, timezone
            timeout = row["timeout_seconds"]
            remaining = None if timeout is None else max(0, timeout - (datetime.now(timezone.utc) - row["submitted_at"]).total_seconds())
            runtime_extra["_deployment_timeout_deadline"] = None if remaining is None else time.monotonic() + remaining
            from vibecanvas_api.services.workflow_resources import (
                collect_subagent_resources, prepare_execution_resources,
            )
            # Host execution claims are fenced against the durable invocation by
            # prepare_execution_resources; caller-supplied snapshots are ignored.
            runtime_extra.pop("workflow_resources", None)
            if collect_subagent_resources(workflow):
                claims = dict(resource_claims or {})
                if (claims.get("tenant_id") != tenant_id
                        or claims.get("execution_id") != run_id
                        or claims.get("execution_resource_type") != "deployment_invocation"):
                    raise PermissionError("deployment_resource_identity_missing")
                runtime_extra["workflow_resources"] = await prepare_execution_resources(
                    sandbox_session=session, workflow=workflow, **claims,
                )
            path = getattr(session, "_workflow_dependency_pythonpath", None)
            if path:
                runtime_extra["code_pythonpath"] = path
            result = await self._execute_request(session, workflow=workflow, inputs=inputs,
                extra=runtime_extra, tenant_id=tenant_id, run_id=run_id, wf_id=row["spec"]["wf_id"])
            return result
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            # sandboxd owns completion once it accepts execution. The HTTP/queue
            # caller can disappear after dispatch without stranding the drain
            # lease. Only summary counts are persisted, never workflow outputs.
            await self._record_completion(tenant_id, run_id, result, time.perf_counter() - started, claim)

    async def _maintain_execution_lease(self, tenant_id, run_id, claim, owner):
        """An executor stops its own request if it cannot renew ownership.

        This runs independently of revision preparation/rollout. Expired claims
        cannot be renewed, and another caller cannot take over a claimed run.
        """
        from sqlalchemy import text
        from vibecanvas_api.storage.db import short_session_scope
        try:
            while True:
                await asyncio.sleep(LEASE_HEARTBEAT_SECONDS)
                async with asyncio.timeout(5):
                    async with short_session_scope(tenant_id=tenant_id) as db:
                        renewed = (await db.execute(text("""UPDATE deployment_invocations
                            SET execution_lease_until=now()+interval '60 seconds'
                            WHERE id=:id AND runtime_claim=:claim AND status IN ('running','waiting_approval')
                            AND NOT EXISTS (SELECT 1 FROM workflow_execution_runs h WHERE h.id=:id AND h.timeout_requested_at IS NOT NULL)
                            AND execution_lease_until > now() RETURNING id"""),
                            {'id': uuid.UUID(run_id), 'claim': claim})).scalar_one_or_none()
                if renewed is None:
                    owner.cancel()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            # Fail closed on database loss. _execute_request stops only this
            # worker and holds the local drain lease until exit is confirmed.
            owner.cancel()

    @complete_before_cancelling
    async def _record_completion(self, tenant_id, run_id, result, elapsed, claim):
        from vibecanvas_api.storage.db import short_session_scope
        from vibecanvas_api.storage.repo_deployment_invocations import DeploymentInvocationsRepo
        errors = (result or {}).get('error_dict') or {}
        outputs = (result or {}).get('final_outputs') or {}
        failed = result is None or bool(errors)
        async with short_session_scope(tenant_id=tenant_id) as db:
            from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo, TERMINAL_STATUSES
            history = WorkflowHistoryRepo(db)
            execution = await history.get(run_id)
            if execution is not None and execution["status"] not in TERMINAL_STATUSES:
                if execution.get("timeout_requested_at") is not None:
                    await history.confirm_timed_out(run_id)
                else:
                    await history.fail(run_id, error_code="execution_failed")
                execution = await history.get(run_id)
            terminal_status = execution["status"] if execution is not None else ("failed" if failed else "succeeded")
            await DeploymentInvocationsRepo(db).mark_terminal(
                uuid.UUID(run_id), status=terminal_status,
                latency_ms=elapsed * 1000,
                error=(execution['error_code'] if execution is not None else None) or ('execution_failed' if failed else None),
                result_summary={'output_count': len(outputs) if isinstance(outputs, dict) else 0,
                                'error_count': len(errors) if isinstance(errors, dict) else 0},
                runtime_claim=claim,
            )

    async def _execute_request(self, session, *, workflow, inputs, extra, tenant_id, run_id, wf_id):
        from .workflow_execution_driver import WorkflowExecutionDriver

        if session._workflow_rpc_pool.workflow is not None and session._workflow_rpc_pool.workflow != workflow:
            raise RuntimeError("deployment_revision_workflow_mismatch")
        session._begin_activity()
        try:
            async with session._workflow_rpc_pool.acquire(run_id) as slot:
                async def persist_artifacts():
                    await session._deployment_workspace.sync()
                    await session._sync_mount_folder()

                async def on_event(frame):
                    if frame["type"] not in {"approval_requested", "approval_resolved"}:
                        return
                    from sqlalchemy import text
                    from vibecanvas_api.storage.db import short_session_scope
                    # Project the durable state, not an older frame in this batch.
                    async with short_session_scope(tenant_id=tenant_id) as db:
                        await db.execute(text("""UPDATE deployment_invocations i
                            SET status=h.status FROM workflow_execution_runs h
                            WHERE i.id=:id AND h.id=i.id
                            AND i.status IN ('running','waiting_approval')
                            AND h.status IN ('running','waiting_approval')"""), {"id": uuid.UUID(run_id)})

                driver = WorkflowExecutionDriver(
                    tenant_id=tenant_id, execution_id=run_id, slot=slot,
                    persist_artifacts=persist_artifacts, on_event=on_event,
                )
                context = dict(extra)
                deadline = context.pop("_deployment_timeout_deadline", None)
                timeout = None if deadline is None else max(0, deadline - time.monotonic())
                return await driver.run(inputs=inputs, context=context, timeout_seconds=timeout)
        finally:
            try:
                # Timeout/cancellation also preserves files already written.
                await session._deployment_workspace.sync()
            finally:
                session._end_activity()

    async def retire(self, tenant_id: str, revision_id: str):
        key = f"{tenant_id}:{revision_id}"
        async with self._locks.setdefault(key, asyncio.Lock()):
            if self._active_requests.get(key, 0):
                return False
            for identifier, terminal in list(self._terminals.items()):
                if terminal['scope'][:2] == (tenant_id, revision_id):
                    self._terminals.pop(identifier, None)
                    try:
                        await terminal['session'].submit_sandbox_job({'kind': 'terminal', 'op': {
                            'action': 'close', 'terminal_id': identifier}}, timeout=5)
                    except Exception:
                        # Whole-instance shutdown below is authoritative.
                        pass
            await self.manager.close_session(tenant_id, revision_scope(revision_id))
            if self._resources is None and os.environ.get('SANDBOX_CGROUP_ROOT'):
                from vibecanvas_api.services.sandbox.resource_limits import ResourceAllocator
                self._resources = ResourceAllocator.from_environment()
            if self._resources and not self._resources.release(revision_id):
                return False
            self._ready.pop(key, None)
        # Lock entries are small; removal is owned by the single controller
        # after durable retirement, when admission can no longer reference it.
        self._locks.pop(key, None)
        return True

    async def terminal(self, *, tenant_id: str, deployment_id: str, revision_id: str,
                       user_id: str, terminal_id: str, action: str, **fields) -> dict:
        """Trusted API RPC; bind every PTY operation to its original identity.

        The API owns browser authentication and continuously checks UPDATE
        authority. This second boundary prevents a different connection scope
        from selecting an already-open shell by its identifier.
        """
        from sqlalchemy import text
        from vibecanvas_api.storage.db import short_session_scope
        identifier = str(uuid.UUID(terminal_id))
        tenant_id, revision_id = str(uuid.UUID(tenant_id)), str(uuid.UUID(revision_id))
        scope = (tenant_id, revision_id, str(uuid.UUID(deployment_id)), str(uuid.UUID(user_id)))
        if action not in {'open', 'read', 'write', 'resize', 'close'}:
            raise ValueError('unknown_terminal_operation')
        record = self._terminals.get(identifier)
        if record is not None and record['scope'] != scope:
            raise RuntimeError('terminal_scope_mismatch')
        if action == 'open':
            if record is not None:
                raise RuntimeError('terminal_already_open')
            # Recheck the actual serving revision; opening a terminal never
            # cold-starts a sandbox or attaches to an old draining revision.
            async with short_session_scope(tenant_id=tenant_id) as db:
                active = (await db.execute(text("""SELECT 1 FROM deployments
                    WHERE id=:id AND enabled AND deleted_at IS NULL
                    AND active_revision_id=:revision"""),
                    {'id': uuid.UUID(deployment_id), 'revision': uuid.UUID(revision_id)})).scalar_one_or_none()
            if active is None:
                raise RuntimeError('terminal_instance_changed')
            session = self._ready.get(f'{tenant_id}:{revision_id}')
            if session is None or session.closed or self.metrics(tenant_id, revision_id) is None:
                raise RuntimeError('terminal_instance_unavailable')
            # Local admission complements the supervisor's 4-per-instance cap.
            # Entries abandoned by a crashed API expire with the guest PTY.
            for stale_id, stale in list(self._terminals.items()):
                if time.monotonic() - stale['touched'] > 130:
                    self._terminals.pop(stale_id, None)
            if len(self._terminals) >= 256:
                raise RuntimeError('terminal_capacity_exhausted')
            record = {'scope': scope, 'session': session, 'touched': time.monotonic()}
            self._terminals[identifier] = record
        if record is None:
            return {'ok': action == 'close', 'error': 'terminal_closed'}
        session = record['session']
        if action == 'close':
            self._terminals.pop(identifier, None)
        if session.closed:
            self._terminals.pop(identifier, None)
            return {'ok': False, 'error': 'terminal_instance_changed'}
        record['touched'] = time.monotonic()
        op = {'action': action, 'terminal_id': identifier}
        for key in ('columns', 'rows', 'data'):
            if key in fields:
                op[key] = fields[key]
        try:
            result = await session.submit_sandbox_job({'kind': 'terminal', 'op': op}, timeout=5)
            if not result.get('ok'):
                self._terminals.pop(identifier, None)
            return result
        except BaseException:
            self._terminals.pop(identifier, None)
            # An interrupted open can still reach the guest; its idle timeout
            # reaps it even when the API never receives the connection ID.
            raise

    def metrics(self, tenant_id: str, revision_id: str) -> dict | None:
        session = self._ready.get(f'{tenant_id}:{revision_id}')
        if session is None or session.closed:
            return None
        pool = session._fileop_pool
        if pool is None or not pool._handles or pool._handles[0].proc.poll() is not None:
            return None
        group = getattr(session.provider, 'resource_group', None)
        return group.metrics() if group else None
