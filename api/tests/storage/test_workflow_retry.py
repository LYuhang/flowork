"""Scope, latest-execution selection and immutable run files for manual retry."""
import uuid

import pytest
from sqlalchemy import text

from vibecanvas_api.services.workflow_retry import latest_workflow_run, retry_reason, load_retry_visits
from vibecanvas_api.services.workflow_artifacts import persist_workflow_artifacts, restore_workflow_artifacts
from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo
from vibecanvas_api.storage.db import session_scope
from test_workflow_history import owner


@pytest.mark.asyncio
async def test_retry_requires_latest_personal_full_failure_and_identical_snapshot(pg_engine):
    tenant, actor, _ = await owner()
    other_tenant, other_actor, _ = await owner()
    graph={'node_1':{'node_id':'node_1','node_type':'StartNode'}}
    inputs={'value':'private'}
    async with session_scope(tenant_id=tenant) as db:
        repo=WorkflowHistoryRepo(db)
        async def create(**kwargs):
            eid=str(uuid.uuid4())
            await repo.create(execution_id=eid,tenant_id=tenant,wf_id='retry-test',source_type='workflow',
                source_id='retry-test',initiator_user_id=actor,workflow=graph,inputs=inputs,approvers={},**kwargs)
            await db.execute(text("UPDATE workflow_execution_runs SET created_at=clock_timestamp() WHERE id=:id"), {"id": uuid.UUID(eid)})
            return eid
        failed=await create()
        await repo.bind_runtime(failed,'test-generation')
        await repo.persist_events(failed,'test-generation',[
            {'seq':1,'type':'node_event','generation':'test-generation','invocation_id':failed,
             'node_id':'node_1','node_type':'StartNode','status':'success','inputs':inputs,'output':inputs,'loop_stack':[]},
            {'seq':2,'type':'result','generation':'test-generation','invocation_id':failed,
             'status':'failed','final_outputs':{},'error_dict':{'node_2':'failed'},'execution_time':0},
        ])
        # A later single-node debug run must not displace the full workflow.
        single=await create(node_id='node_2')
        await repo.fail(single,error_code='execution_dispatch_failed')
        detail=await latest_workflow_run(db,wf_id='retry-test',user_id=actor)
        assert detail['id']==failed
        args=dict(wf_id='retry-test',user_id=actor,workflow=graph,inputs=inputs)
        assert retry_reason(detail,**args) is None
        assert retry_reason(detail,**{**args,'user_id':other_actor})=='no_failure'
        assert retry_reason(detail,**{**args,'wf_id':'foreign'})=='no_failure'
        assert retry_reason(detail,**{**args,'workflow':{}})=='workflow_changed'
        assert retry_reason(detail,**{**args,'inputs':{}})=='inputs_changed'
        visits=await load_retry_visits(db,failed)
        assert len(visits)==1 and next(iter(visits.values()))['output']==inputs
        # A new whole run makes the old failure ineligible even when queued.
        newest=await create()
        detail=await latest_workflow_run(db,wf_id='retry-test',user_id=actor)
        assert detail['id']==newest and retry_reason(detail,**args)=='no_failure'
    async with session_scope(tenant_id=other_tenant) as db:
        assert await WorkflowHistoryRepo(db).detail(failed) is None
        assert await latest_workflow_run(db,wf_id='retry-test',user_id=actor) is None


@pytest.mark.asyncio
async def test_retry_copies_previous_files_without_mutating_history(pg_engine,tmp_path):
    tenant, actor, _=await owner()
    eid=str(uuid.uuid4())
    async with session_scope(tenant_id=tenant) as db:
        await WorkflowHistoryRepo(db).create(execution_id=eid,tenant_id=tenant,wf_id='retry-files',
            source_type='workflow',source_id='retry-files',initiator_user_id=actor,
            workflow={},inputs={},approvers={})
    source=tmp_path/'source';source.mkdir()
    (source/'nested').mkdir();(source/'nested'/'result.bin').write_bytes(b'previous output\x00')
    (source/'outside-link').symlink_to('/etc/passwd')
    await persist_workflow_artifacts(root=str(source),tenant_id=tenant,execution_id=eid,wf_id='retry-files')
    for index in range(2):
        target=tmp_path/str(index);target.mkdir()
        await restore_workflow_artifacts(root=target,tenant_id=tenant,execution_id=eid)
        assert (target/'nested'/'result.bin').read_bytes()==b'previous output\x00'
        assert not (target/'outside-link').exists()
        (target/'nested'/'result.bin').write_bytes(b'new output')
