"""One-time, transactional conversion of legacy floating resource versions.

Run during a maintenance window before restarting workers. Returns only IDs and
version identifiers, never workflow content or credentials. Dry-run by default.
"""
from sqlalchemy import text

from vibecanvas_api.services.deployment_revisions import desired_key
from vibecanvas_api.services.deployment_snapshots import resolve_workflow
from vibecanvas_api.services.task_snapshots import freeze_workflow
from vibecanvas_api.storage.repo_tasks import TasksRepo


async def pin_resource_versions(session, *, apply=False):
    report = []
    rows = (await session.execute(text("""SELECT * FROM deployments
        WHERE deleted_at IS NULL AND version_pin != 'specific' ORDER BY id FOR UPDATE"""))).mappings().all()
    for row in rows:
        dep = dict(row)
        previous_key = desired_key(dep)
        revisions = (await session.execute(text("""SELECT id, spec FROM deployment_runtime_revisions
            WHERE deployment_id=:id AND state IN ('active','preparing') ORDER BY
            CASE WHEN id=:active THEN 0 ELSE 1 END, created_at DESC FOR UPDATE"""),
            {"id": dep['id'], "active": dep['active_revision_id']})).mappings().all()
        matching = next((r['spec'] for r in revisions if r['spec'].get('desired_key') == previous_key), None)
        # Preserve the serving version when it corresponds to saved settings.
        # A pending explicit configuration change instead keeps its own target.
        graph = await resolve_workflow(session, dep['user_id'], matching or dep)
        meta = graph['__meta__']
        dep.update(version_pin='specific', pinned_major=meta['workflow_version'], pinned_sub=meta['workflow_subversion'])
        report.append({'kind': 'deployment', 'id': str(dep['id']),
                       'version': f"v{dep['pinned_major']}.sv{dep['pinned_sub']}"})
        if apply:
            await session.execute(text("""UPDATE deployments SET version_pin='specific',
                pinned_major=:major, pinned_sub=:sub, updated_at=now() WHERE id=:id"""),
                {'id': dep['id'], 'major': dep['pinned_major'], 'sub': dep['pinned_sub']})
            for rev in revisions:
                spec = rev['spec']
                if (spec.get('desired_key') == previous_key and
                    (spec['pinned_major'], spec['pinned_sub']) == (dep['pinned_major'], dep['pinned_sub'])):
                    # Avoid a needless restart when only pin policy changes.
                    await session.execute(text("""UPDATE deployment_runtime_revisions
                        SET spec=jsonb_set(spec, '{desired_key}', to_jsonb(CAST(:key AS text))) WHERE id=:id"""),
                        {'id': rev['id'], 'key': desired_key(dep)})
    tasks = TasksRepo(session)
    rows = (await session.execute(text('SELECT id FROM task_schedules ORDER BY id FOR UPDATE'))).all()
    for (schedule_id,) in rows:
        schedule = await tasks.get_schedule(schedule_id)
        selector = schedule.workflow_selector or {}
        if selector.get('version') and not selector.get('major'):
            continue
        frozen = await freeze_workflow(session, schedule.user_id, schedule.workflow_id,
                                       major=selector.get('major'), version=selector.get('version'))
        report.append({'kind': 'schedule', 'id': str(schedule_id), 'version': frozen['version']})
        if apply:
            await tasks.update_schedule(schedule_id, workflow_selector={'version': frozen['version']})
    return report
