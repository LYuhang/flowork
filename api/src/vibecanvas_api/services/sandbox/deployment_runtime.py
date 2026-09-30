"""sandboxd-owned resident revisions; business admission is durable in PostgreSQL."""
from __future__ import annotations

import asyncio
import copy
import contextlib
import os
import shutil
import time
import uuid

from vibecanvas_api.services.deployment_revisions import revision_scope
from vibecanvas_api.services.deployment_completion import complete_before_cancelling

LEASE_HEARTBEAT_SECONDS = 10


class DeploymentRuntime:
    def __init__(self, manager):
        self.manager = manager
        self._locks: dict[str, asyncio.Lock] = {}
        self._ready: dict[str, object] = {}
        self._active_requests: dict[str, int] = {}
        self._resources = None
        self._terminals: dict[str, dict] = {}

    async def prepare(self, *, tenant_id: str, revision_id: str, spec: dict, workflow: dict):
        from vibecanvas_api.services.workflow_sandbox_runner import prepare_code_pythonpath
        from vibecanvas_api.services.sandbox.bubblewrap import BubblewrapProvider
        from vibecanvas_api.services.sandbox.resource_limits import ResourceAllocator, ResourceBudget
        key = f"{tenant_id}:{revision_id}"
        async with self._locks.setdefault(key, asyncio.Lock()):
            previous = self._ready.get(key)
            if previous is not None:
                pool = previous._fileop_pool
                if not previous.closed and pool is not None and pool._handles and pool._handles[0].proc.poll() is None:
                    return previous
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
                await prepare_code_pythonpath(workflow, session=session)
                await session.prewarm_fileops()
                # Warm the actual reusable engine process, without invoking the
                # customer's workflow or making model/network calls.
                status = await session.submit_sandbox_job({
                    "kind": "prewarm", "tenant": tenant_id,
                    "run_id": "prewarm", "run_subpath": "prewarm",
                }, timeout=60)
                if status.get("status") != "success":
                    raise RuntimeError("deployment_engine_prewarm_failed")
            except BaseException:
                await self.manager.close_session(tenant_id, revision_scope(revision_id))
                self._resources.release(revision_id)
                raise
            self._ready[key] = session
            return session

    async def run(self, *, tenant_id: str, deployment_id: str, revision_id: str,
                  workflow: dict, inputs: dict, run_id: str, extra: dict | None = None,
                  **unused) -> dict:
        # A disconnected RPC may cause the API's durable invocation to become
        # terminal before its sandbox worker exits. Keep an independent local
        # drain lease until execution and credential cleanup are confirmed.
        key = f"{tenant_id}:{revision_id}"
        self._active_requests[key] = self._active_requests.get(key, 0) + 1
        try:
            return await self._run_admitted(
                tenant_id=tenant_id, deployment_id=deployment_id, revision_id=revision_id,
                workflow=workflow, inputs=inputs, run_id=run_id, extra=extra,
            )
        finally:
            remaining = self._active_requests[key] - 1
            if remaining:
                self._active_requests[key] = remaining
            else:
                self._active_requests.pop(key, None)

    async def _run_admitted(self, *, tenant_id: str, deployment_id: str, revision_id: str,
                            workflow: dict, inputs: dict, run_id: str, extra: dict | None) -> dict:
        from sqlalchemy import text
        from vibecanvas_api.storage.db import short_session_scope
        claim = uuid.uuid4()
        # No arbitrary RPC-supplied graph may select another deployment's pool.
        async with short_session_scope(tenant_id=tenant_id) as db:
            row = (await db.execute(text("""SELECT r.spec FROM deployment_runtime_revisions r
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
            subpath = f"requests/{uuid.UUID(run_id).hex}"
            runtime_extra = dict(extra or {})
            path = getattr(session, "_workflow_dependency_pythonpath", None)
            if path:
                runtime_extra["code_pythonpath"] = path
            result = await self._execute_request(session, workflow=workflow, inputs=inputs,
                extra=runtime_extra, tenant_id=tenant_id, run_id=run_id, subpath=subpath)
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
                            WHERE id=:id AND runtime_claim=:claim AND status='running'
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
            await DeploymentInvocationsRepo(db).mark_terminal(
                uuid.UUID(run_id), status='failed' if failed else 'succeeded',
                latency_ms=elapsed * 1000,
                error='execution_failed' if failed else None,
                result_summary={'output_count': len(outputs) if isinstance(outputs, dict) else 0,
                                'error_count': len(errors) if isinstance(errors, dict) else 0},
                runtime_claim=claim,
            )

    async def _execute_request(self, session, *, workflow, inputs, extra, tenant_id, run_id, subpath):
        started = time.perf_counter()
        # Shield the owner of the offloaded blocking job. Cancelling to_thread
        # alone does not stop its worker and must not release egress or files.
        job = asyncio.create_task(session.execute_workflow_job(
            workflow=workflow, inputs=inputs, extra=extra, tenant=tenant_id,
            run_id=run_id, run_subpath=subpath, timeout=None, kill_individually=True,
        ))
        try:
            try:
                outcome = await asyncio.shield(job)
            except asyncio.CancelledError:
                # Signal only this request's worker, including if cancellation
                # arrives before enqueue. Never restart the shared sandbox.
                stop = asyncio.create_task(session.kill_workflow_job(
                    run_id=run_id, tenant=tenant_id, run_subpath=subpath))
                for pending in (stop, job):
                    while not pending.done():
                        try:
                            await asyncio.shield(pending)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    # Consume failures; if signalling failed, waiting for the
                    # original job still prevents premature instance retirement.
                    if not pending.cancelled():
                        pending.exception()
                raise
            result = outcome.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("deployment_job_failed")
            # Keep mounts durable while the instance stays resident.
            await session._sync_mount_folder()
            result.setdefault("execution_time", time.perf_counter() - started)
            return result
        finally:
            # Request channels contain scoped broker credentials: never retain
            # them in a resident instance after returning the result.
            root = os.path.join(os.path.dirname(session.workflow_run_dir), subpath)
            await asyncio.to_thread(shutil.rmtree, root, True)

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
