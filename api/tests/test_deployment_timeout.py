"""Real worker timeout and human-wait budget regression."""
import asyncio, shutil, uuid
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import text
from tests.storage.test_workflow_history import owner, approval_graph
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo
from vibecanvas_api.services.sandbox.workflow_execution_driver import WorkflowExecutionDriver
from vibecanvas_api.services.sandbox.workflow_rpc_slot import WorkflowRpcWorker, WorkflowInvocationSlot
from vibecanvas_api.services.sandbox.bubblewrap import BubblewrapProvider

@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['timeout', 'approval', 'requested'])
async def test_worker_stop_and_approval_budget(pg_engine, monkeypatch, mode):
    tenant, actor, _ = await owner()
    execution = str(uuid.uuid4()); graph = approval_graph()
    graph['node_2']['node_config']['timeout_seconds'] = 3
    monkeypatch.setattr('vibecanvas_api.services.sandbox.workflow_execution_driver.refresh_execution_context', AsyncMock(return_value={'llm_credentials': {}, 'workflow_resources': {}}))
    async with session_scope(tenant_id=tenant) as db:
        await WorkflowHistoryRepo(db).create(execution_id=execution,tenant_id=tenant,wf_id='wf-budget',source_type='workflow',source_id='wf-budget',initiator_user_id=actor,workflow=graph,inputs={},approvers={'node_2':actor})
    with TemporaryDirectory(prefix='fw-budget-') as root:
        worker=WorkflowRpcWorker(provider=BubblewrapProvider(shutil.which('bwrap')),root=root,revision='v1',workflow=graph)
        slot=WorkflowInvocationSlot(worker)
        try:
            await slot.start()
            if mode == 'timeout':
                async def hang(*a, **k): await asyncio.sleep(60)
                slot.invoke=hang
            if mode == 'requested':
                async with session_scope(tenant_id=tenant) as db:
                    await db.execute(text('UPDATE workflow_execution_runs SET timeout_requested_at=now() WHERE id=:id'),{'id':uuid.UUID(execution)})
            driver=WorkflowExecutionDriver(tenant_id=tenant,execution_id=execution,slot=slot,persist_artifacts=AsyncMock())
            result=await asyncio.wait_for(driver.run(inputs={},context={},timeout_seconds=1),15)
            async with session_scope(tenant_id=tenant) as db: detail=await WorkflowHistoryRepo(db).detail(execution)
            if mode == 'approval':
                assert detail['status']=='succeeded'
                assert result['final_outputs']['__end__']['approved'] is False
            else:
                assert detail['status']=='timed_out' and detail['error_code']=='execution_timeout'
                assert slot.alive and slot.invocation_id is None
        finally: await worker.close()

@pytest.mark.asyncio
async def test_timeout_snapshot_and_unclaimed_fence(pg_engine, app_engine):
    from tests.test_deployment_rollout import setup_rollout
    from vibecanvas_api.storage.repo_deployment_invocations import DeploymentInvocationsRepo
    from vibecanvas_api.services.deployment_execution_history import create_deployment_history
    from vibecanvas_api.services.deployment_observer import observe_invocation
    controller,dep,spec=await setup_rollout(pg_engine,app_engine)
    tenant=str(dep['tenant_id']); invocation=uuid.uuid4()
    async with session_scope(tenant_id=tenant) as db:
        await db.execute(text('UPDATE deployments SET timeout_seconds=1 WHERE id=:id'),{'id':dep['id']})
        await DeploymentInvocationsRepo(db).create(invocation_id=invocation,tenant_id=dep['tenant_id'],deployment_id=dep['id'],wf_id=dep['wf_id'],trigger_type='api',source='sync_api',status='running',revision_id=dep['active_revision_id'])
        await create_deployment_history(db,invocation_id=invocation,deployment=dep,revision={'id':dep['active_revision_id']},workflow=await controller.graph(spec),inputs={'x':1})
        history = await WorkflowHistoryRepo(db).detail(str(invocation))
        assert history['workflow_version'] == f"v{spec['pinned_major']}.sv{spec['pinned_sub']}"
        await db.execute(text("UPDATE deployment_invocations SET submitted_at=now()-interval '2 seconds' WHERE id=:id"),{'id':invocation})
        await db.execute(text('UPDATE deployments SET timeout_seconds=120 WHERE id=:id'),{'id':dep['id']})
    response=await observe_invocation(tenant_id=tenant,slug='test',invocation_id=str(invocation))
    assert response.status_code==504
    async with session_scope(tenant_id=tenant) as db:
        row=(await db.execute(text('SELECT status,timeout_seconds,runtime_claim FROM deployment_invocations WHERE id=:id'),{'id':invocation})).one()
        assert row==('timed_out',1,None)


@pytest.mark.asyncio
async def test_sync_success_waits_for_runtime_capacity_release(pg_engine, app_engine):
    from tests.test_deployment_rollout import setup_rollout
    from vibecanvas_api.services.deployment_observer import observe_invocation
    from vibecanvas_api.services.deployment_execution_history import create_deployment_history
    from vibecanvas_api.storage.repo_deployment_invocations import DeploymentInvocationsRepo
    controller,dep,spec=await setup_rollout(pg_engine,app_engine)
    tenant=str(dep['tenant_id']); invocation=uuid.uuid4()
    async with session_scope(tenant_id=tenant) as db:
        await DeploymentInvocationsRepo(db).create(invocation_id=invocation,tenant_id=dep['tenant_id'],deployment_id=dep['id'],wf_id=dep['wf_id'],trigger_type='api',source='sync_api',status='running',revision_id=dep['active_revision_id'])
        await create_deployment_history(db,invocation_id=invocation,deployment=dep,revision={'id':dep['active_revision_id']},workflow=await controller.graph(spec),inputs={'x':1})
        repo=WorkflowHistoryRepo(db)
        await repo.bind_runtime(str(invocation),'test')
        await repo.persist_events(str(invocation),'test',[{'seq':1,'generation':'test','invocation_id':str(invocation),'type':'result','status':'succeeded','final_outputs':{'__end__':{'value':7}},'error_dict':{},'execution_time':0.1}])
    observer=asyncio.create_task(observe_invocation(tenant_id=tenant,slug='test',invocation_id=str(invocation)))
    try:
        await asyncio.sleep(.3)
        assert not observer.done(), 'History completion must not imply capacity release'
        async with session_scope(tenant_id=tenant) as db:
            await DeploymentInvocationsRepo(db).mark_terminal(invocation,status='succeeded',latency_ms=100)
        response=await asyncio.wait_for(observer,3)
        assert response['outputs']=={'value':7}
    finally:
        observer.cancel()
        await asyncio.gather(observer,return_exceptions=True)
