"""Reconcile enabled deployments on sandboxd, preserving the active revision.

The old instance is never freed to make space for its replacement. Admission
and promotion serialize on the deployment row; queued/running invocation rows
are the durable drain leases, including across API/daemon restarts.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
import structlog
from sqlalchemy import text
from vibecanvas_api.services.tenant_db import session_scope_admin
from vibecanvas_api.storage.db import short_session_scope
from vibecanvas_api.services.deployment_revisions import desired_key, execution_spec
from vibecanvas_api.services.workflow_runner import load_workflow_version

log = structlog.get_logger(__name__)
POLL_SECONDS = 5


class DeploymentRollouts:
    def __init__(self, manager):
        self.manager = manager
        self.graphs = {}
        self.retry_after = {}

    async def matches_desired(self, db, dep, spec):
        """Recheck saved settings and floating versions while holding admission lock."""
        if not dep['enabled'] or dep['deleted_at'] is not None:
            return False
        if desired_key(dep) != spec['desired_key']:
            return False
        from vibecanvas_api.services.deployment_snapshots import resolve_workflow
        current = await resolve_workflow(db, dep['user_id'], dep)
        meta = current['__meta__']
        return (meta['workflow_version'], meta['workflow_subversion']) == (spec['pinned_major'], spec['pinned_sub'])

    async def graph(self, spec):
        key = (spec['wf_id'], spec['version_pin'], spec.get('pinned_major'), spec.get('pinned_sub'))
        if spec['version_pin'] == 'specific' and key in self.graphs:
            return self.graphs[key]
        graph = await load_workflow_version(spec)
        if spec['version_pin'] == 'specific':
            if len(self.graphs) >= 64:
                self.graphs.pop(next(iter(self.graphs)))
            self.graphs[key] = graph
        return graph

    async def tick(self, *, maintenance=True):
        if maintenance:
            await self.expire_invocations()
        async with session_scope_admin() as db:
            rows = (await db.execute(text("""SELECT d.*, a.created_by AS runtime_user_id
                FROM deployments d LEFT JOIN service_accounts a ON a.service_account_id=d.service_account_id
                WHERE (d.enabled AND d.deleted_at IS NULL) OR d.active_revision_id IS NOT NULL
                    OR EXISTS (SELECT 1 FROM deployment_runtime_revisions r
                        WHERE r.deployment_id=d.id AND r.state IN ('active','preparing'))
                ORDER BY d.created_at"""))).mappings().all()
        for row in rows:
            dep = dict(row)
            try:
                await self.reconcile(dep)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Exception formatting may include graph data and bootstrap
                # credentials in locals. Keep repeated recovery failures small
                # and safe in host logs as well as in the UI.
                log.warning('deployment_rollout_reconcile_failed',
                            deployment_id=str(dep['id']), error_type=type(exc).__name__)
                async with short_session_scope(tenant_id=str(dep['tenant_id'])) as db:
                    locked = (await db.execute(text('SELECT * FROM deployments WHERE id=:id FOR UPDATE'), {'id': dep['id']})).mappings().one_or_none()
                    if (locked is not None and locked['enabled'] and locked['deleted_at'] is None
                            and desired_key(locked) == desired_key(dep)
                            and locked['active_revision_id'] == dep['active_revision_id']):
                        await db.execute(text("UPDATE deployments SET rollout_status='failed', rollout_error='preparation_failed' WHERE id=:id"), {'id': dep['id']})
        if maintenance:
            await self.sample_metrics()
            await self.drain()

    async def expire_invocations(self):
        from vibecanvas_api.services.deployment_expiry import expire_deployment_invocations
        await expire_deployment_invocations(self.manager.deployments)

    async def sample_metrics(self):
        async with session_scope_admin() as db:
            rows = (await db.execute(text("SELECT id, tenant_id FROM deployment_runtime_revisions WHERE state IN ('active','preparing','draining')"))).mappings().all()
        for row in rows:
            metrics = self.manager.deployments.metrics(str(row['tenant_id']), str(row['id']))
            async with short_session_scope(tenant_id=str(row['tenant_id'])) as db:
                await db.execute(text('UPDATE deployment_runtime_revisions SET runtime_metrics=CAST(:metrics AS jsonb), observed_at=now() WHERE id=:id'),
                    {'id': row['id'], 'metrics': json.dumps(metrics) if metrics is not None else None})

    async def reconcile(self, dep):
        tenant = str(dep['tenant_id'])
        async with short_session_scope(tenant_id=tenant) as db:
            revisions = [dict(r) for r in (await db.execute(text("""SELECT * FROM deployment_runtime_revisions
                WHERE deployment_id=:id AND state IN ('active','preparing','draining') ORDER BY created_at"""),
                {'id': dep['id']})).mappings()]
        if not dep['enabled'] or dep['deleted_at'] is not None:
            async with short_session_scope(tenant_id=tenant) as db:
                locked = (await db.execute(text('SELECT * FROM deployments WHERE id=:id FOR UPDATE'), {'id': dep['id']})).mappings().one()
                if not locked['enabled'] or locked['deleted_at'] is not None:
                    await db.execute(text("UPDATE deployment_runtime_revisions SET state='draining' WHERE deployment_id=:id AND state IN ('active','preparing')"), {'id': dep['id']})
                    await db.execute(text("UPDATE deployments SET active_revision_id=NULL, rollout_status='stopped', rollout_error=NULL WHERE id=:id"), {'id': dep['id']})
            return
        if not dep.get('runtime_user_id'):
            raise RuntimeError('deployment_identity_unavailable')
        # Recover the active instance first after sandboxd restart, even when a
        # newer desired revision is broken or there is no rolling-update slot.
        active = next((r for r in revisions if r['id'] == dep['active_revision_id']), None)
        if active:
            active_key = str(active['id'])
            failures, retry_at = self.retry_after.get(active_key, (0, 0))
            if time.monotonic() < retry_at:
                return
            try:
                await self.manager.deployments.prepare(tenant_id=tenant, revision_id=active_key,
                    spec=active['spec'], workflow=await self.graph(active['spec']))
            except Exception as exc:
                # Recovery needs the same bounded backoff as a new candidate.
                # Otherwise broken active instances rebuild every tick and compete
                # with task execution for the shared resident capacity.
                self.retry_after[active_key] = (failures + 1,
                    time.monotonic() + min(60, 5 * 2 ** min(failures, 4)))
                code = 'waiting_capacity' if 'capacity' in str(exc).lower() else 'preparation_failed'
                async with short_session_scope(tenant_id=tenant) as db:
                    await db.execute(text("""UPDATE deployments
                        SET rollout_status=:state, rollout_error=:code
                        WHERE id=:id AND active_revision_id=:revision
                            AND enabled AND deleted_at IS NULL"""),
                        {'id': dep['id'], 'revision': active['id'], 'code': code,
                         'state': 'waiting_capacity' if code == 'waiting_capacity' else 'failed'})
                log.warning('deployment_active_not_ready', deployment_id=str(dep['id']), reason=code)
                return
            self.retry_after.pop(active_key, None)
        workflow = await self.graph(dep)
        spec = execution_spec(dep, workflow, str(dep['runtime_user_id']))
        already_active = active is not None and active['spec'] == spec
        candidate = None if already_active else next((r for r in revisions if r['state'] == 'preparing' and r['spec'] == spec), None)
        # At most one candidate for the desired settings; discard superseded
        # candidates, but leave active/draining instances untouched.
        for stale in revisions:
            if stale['state'] == 'preparing' and stale != candidate:
                if await self.manager.deployments.retire(tenant, str(stale['id'])) is False:
                    return
                self.retry_after.pop(str(stale['id']), None)
                async with short_session_scope(tenant_id=tenant) as db:
                    await db.execute(text("UPDATE deployment_runtime_revisions SET state='retired', retired_at=now() WHERE id=:id AND state='preparing'"), {'id': stale['id']})
        if already_active:
            async with short_session_scope(tenant_id=tenant) as db:
                locked = (await db.execute(text('SELECT * FROM deployments WHERE id=:id FOR UPDATE'), {'id': dep['id']})).mappings().one()
                if locked['active_revision_id'] == active['id'] and await self.matches_desired(db, locked, spec):
                    await db.execute(text("UPDATE deployments SET rollout_status='ready', rollout_error=NULL WHERE id=:id"), {'id': dep['id']})
            return
        if candidate is None:
            candidate = {'id': uuid.uuid4(), 'spec': spec}
            async with short_session_scope(tenant_id=tenant) as db:
                locked = (await db.execute(text('SELECT * FROM deployments WHERE id=:id FOR UPDATE'), {'id': dep['id']})).mappings().one()
                if not await self.matches_desired(db, locked, spec):
                    return
                await db.execute(text("""INSERT INTO deployment_runtime_revisions(id,tenant_id,deployment_id,spec)
                    VALUES (:id,:tenant,:dep,CAST(:spec AS jsonb))"""),
                    {'id': candidate['id'], 'tenant': dep['tenant_id'], 'dep': dep['id'], 'spec': json.dumps(spec)})
                await db.execute(text("UPDATE deployments SET rollout_status='preparing', rollout_error=NULL WHERE id=:id"), {'id': dep['id']})
        candidate_key = str(candidate['id'])
        failures, retry_at = self.retry_after.get(candidate_key, (0, 0))
        if time.monotonic() < retry_at:
            return
        try:
            await self.manager.deployments.prepare(tenant_id=tenant, revision_id=str(candidate['id']), spec=spec, workflow=workflow)
        except Exception as exc:
            failures += 1
            self.retry_after[candidate_key] = (failures, time.monotonic() + min(60, 5 * 2 ** min(failures - 1, 4)))
            code = 'waiting_capacity' if 'capacity' in str(exc).lower() else 'preparation_failed'
            async with short_session_scope(tenant_id=tenant) as db:
                locked = (await db.execute(text('SELECT * FROM deployments WHERE id=:id FOR UPDATE'), {'id': dep['id']})).mappings().one()
                if await self.matches_desired(db, locked, spec):
                    await db.execute(text('UPDATE deployments SET rollout_status=:state, rollout_error=:code WHERE id=:id'),
                        {'id': dep['id'], 'state': 'waiting_capacity' if code == 'waiting_capacity' else 'failed', 'code': code})
            log.warning('deployment_candidate_not_ready', deployment_id=str(dep['id']), reason=code)
            return
        self.retry_after.pop(candidate_key, None)
        async with short_session_scope(tenant_id=tenant) as db:
            locked = dict((await db.execute(text('SELECT * FROM deployments WHERE id=:id FOR UPDATE'), {'id': dep['id']})).mappings().one())
            # Including the resolved version prevents a slow earlier branch
            # build from overwriting a newer saved workflow revision.
            matches = await self.matches_desired(db, locked, spec)
            if not matches:
                return  # next tick reaps this now-superseded candidate
            await db.execute(text("UPDATE deployment_runtime_revisions SET state='draining' WHERE deployment_id=:id AND state='active'"), {'id': dep['id']})
            await db.execute(text("UPDATE deployment_runtime_revisions SET state='active', activated_at=now() WHERE id=:id"), {'id': candidate['id']})
            await db.execute(text("UPDATE deployments SET active_revision_id=:rev, rollout_status='ready', rollout_error=NULL WHERE id=:id"), {'rev': candidate['id'], 'id': dep['id']})
        log.info('deployment_traffic_switched', deployment_id=str(dep['id']), revision_id=str(candidate['id']))

    async def drain(self):
        async with session_scope_admin() as db:
            rows = (await db.execute(text("""SELECT r.* FROM deployment_runtime_revisions r
                WHERE r.state='draining' AND NOT EXISTS (SELECT 1 FROM deployment_invocations i
                    WHERE i.revision_id=r.id AND i.status IN ('queued','running','waiting_approval'))"""))).mappings().all()
        for row in rows:
            if await self.manager.deployments.retire(str(row['tenant_id']), str(row['id'])) is False:
                continue
            self.retry_after.pop(str(row['id']), None)
            async with short_session_scope(tenant_id=str(row['tenant_id'])) as db:
                await db.execute(text("UPDATE deployment_runtime_revisions SET state='retired',retired_at=now() WHERE id=:id AND state='draining'"), {'id': row['id']})

    async def maintain(self, stop):
        while not stop.is_set():
            try:
                await self.expire_invocations()
                await self.sample_metrics()
                await self.drain()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('deployment_rollout_maintenance_failed')
            try:
                await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def serve(self, stop):
        # Dependency installation can be slow. Keep metrics, crash recovery
        # and old-instance draining independent of candidate preparation.
        maintenance = asyncio.create_task(self.maintain(stop), name='deployment-maintenance')
        try:
            while not stop.is_set():
                try:
                    await self.tick(maintenance=False)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception('deployment_rollout_tick_failed')
                try:
                    await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
                except asyncio.TimeoutError:
                    pass
        finally:
            maintenance.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await maintenance
