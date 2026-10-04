"""Creating a deployment from a shared Workflow preserves recipient ownership."""
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from vibecanvas_api.app import build_app
from vibecanvas_api.config import config
from tests.test_workflow_authorization_integration import (
    _browser_sessions, _register, _headers, _RelationshipStore as WorkflowStore,
)
from tests.test_deployment_authorization_integration import _RelationshipStore as DeploymentStore
from tests.test_task_authorization_integration import _RelationshipStore as TaskStore


class Store(DeploymentStore):
    def _allowed(self, user, relation, object_):
        if object_.startswith('workflow:'):
            return WorkflowStore._allowed(self, user, relation, object_)
        if object_.startswith('task:'):
            return TaskStore._allowed(self, user, relation, object_)
        return super()._allowed(user, relation, object_)


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['viewer', 'editor', 'operator', 'manager'])
async def test_shared_workflow_deployment_creation(pg_engine, monkeypatch, role):
    monkeypatch.setattr(config, 'resource_sharing_enabled', True)
    app = build_app()
    app.state.openfga_client = Store()
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        owner_headers, owner = await _register(client, 'source_owner')
        recipient_headers, recipient = await _register(client, 'instance_creator')
        made = await client.post('/api/v1/workflows', json={'name': 'Shared source'}, headers=_headers(owner_headers))
        assert made.status_code == 201, made.text
        workflow = made.json()['wf_id']
        resolved = await client.post(f'/api/v1/resource-access/workflow/{workflow}/resolve-target',
            headers=_headers(owner_headers), json={'target_type': 'user', 'identifier': recipient['email']})
        assert resolved.status_code == 200, resolved.text
        grant = await client.post(f'/api/v1/workflows/{workflow}/access',
            headers=_headers(owner_headers, **{'Idempotency-Key': uuid.uuid4().hex}),
            json={'relation': role, 'resolution_token': resolved.json()['target']['resolution_token']})
        assert grant.status_code == 201, grant.text
        slug = 'shared-source-' + uuid.uuid4().hex[:10]
        created = await client.post('/api/v1/deployments', headers=_headers(recipient_headers), json={
            'name': 'Recipient instance', 'wf_id': workflow, 'slug': slug,
            'trigger_type': 'api', 'version_pin': 'specific', 'pinned_major': 1, 'pinned_sub': 0})
        if role != 'manager':
            assert created.status_code == 404, created.text
            async with pg_engine.connect() as connection:
                assert not (await connection.execute(text('SELECT id FROM deployments WHERE slug=:slug'), {'slug': slug})).first()
            return
        assert created.status_code == 201, created.text
        identifier = created.json()['id']
        async with pg_engine.connect() as connection:
            row = (await connection.execute(text('''SELECT d.tenant_id, d.workflow_tenant_id, d.user_id, d.owner_id,
                a.tenant_id AS account_tenant, a.created_by, d.pinned_major, d.pinned_sub
                FROM deployments d JOIN service_accounts a ON a.service_account_id=d.service_account_id
                WHERE d.id=:id'''), {'id': uuid.UUID(identifier)})).one()
            assert str(row.workflow_tenant_id) == owner['active_organization_id']
            assert str(row.tenant_id) == str(row.account_tenant) == recipient['active_organization_id']
            assert str(row.user_id) == str(row.owner_id) == str(row.created_by) == recipient['user_id']
            assert (row.pinned_major, row.pinned_sub) == (1, 0)
        base = '/api/v1/deployments/' + identifier
        assert (await client.get(base, headers=_headers(recipient_headers))).status_code == 200
        assert (await client.get(base, headers=_headers(owner_headers))).status_code == 404

        from vibecanvas_api.storage.db import session_scope
        from vibecanvas_api.storage.repo_deployments import DeploymentsRepo
        from vibecanvas_api.storage.repo_deployment_invocations import DeploymentInvocationsRepo
        from vibecanvas_api.services.deployment_snapshots import resolve_workflow
        from vibecanvas_api.services.deployment_revisions import execution_spec
        async with session_scope(tenant_id=recipient['active_organization_id'], user_id=recipient['user_id']) as session:
            dep = await DeploymentsRepo(session).get(uuid.UUID(identifier))
            graph = await resolve_workflow(session, recipient['user_id'], dep)
            assert graph['__meta__']['workflow_id'] == workflow
            spec = execution_spec(dep, graph, recipient['user_id'])
            assert spec['workflow_tenant_id'] == owner['active_organization_id']
            assert spec['user_id'] == recipient['user_id']
            assert await resolve_workflow(session, recipient['user_id'], spec) == graph
            from fastapi import HTTPException
            with pytest.raises(HTTPException):
                await resolve_workflow(session, recipient['user_id'], {**spec, 'pinned_sub': 999})
            assert await session.scalar(text("SELECT current_setting('app.tenant_id',true)")) == recipient['active_organization_id']
            invocation = await DeploymentInvocationsRepo(session).create(
                tenant_id=uuid.UUID(recipient['active_organization_id']), deployment_id=uuid.UUID(identifier),
                wf_id=workflow, trigger_type='api', source='test', status='running', inputs={}, snapshot={'workflow': graph})
        async with pg_engine.connect() as connection:
            invocation_owner = (await connection.execute(text('SELECT tenant_id,workflow_tenant_id FROM deployment_invocations WHERE id=:id'),
                {'id': invocation})).one()
            assert str(invocation_owner.tenant_id) == recipient['active_organization_id']
            assert str(invocation_owner.workflow_tenant_id) == owner['active_organization_id']
        from types import SimpleNamespace
        from starlette.requests import Request
        from vibecanvas_api.services.workflow_execution_authorization import authorize_workflow_execution
        from vibecanvas_api.services.agent_runtime.model_capability import authorization_model_generation
        from vibecanvas_api.authorization.openfga_client import OpenFgaTuple
        capability = SimpleNamespace(organization_id=recipient['active_organization_id'], user_id=recipient['user_id'],
            workflow_id=workflow, execution_id=str(invocation), execution_resource_type='deployment_invocation',
            principal_type='service_account', principal_id=str(dep['service_account_id']), principal_generation=1,
            authorization_generation=authorization_model_generation(model_id=config.openfga_authorization_model_id))
        request = Request({'type': 'http', 'app': app, 'headers': [], 'path': '/runtime'})
        resolved_scopes = []
        async def resolve(**kwargs):
            resolved_scopes.append(await kwargs['session'].scalar(text("SELECT current_setting('app.tenant_id',true)")))
            assert kwargs['authz_context'].active_organization_id == recipient['active_organization_id']
            assert not kwargs['authz_context'].admitted_resource_organization_id
            return 'authorized'
        assert await authorize_workflow_execution(request, capability, resolve=resolve) == 'authorized'
        assert resolved_scopes == [recipient['active_organization_id']]
        source_edge = OpenFgaTuple('service_account:' + str(dep['service_account_id']), 'operator', 'workflow:' + workflow)
        app.state.openfga_client.tuples.remove(source_edge)
        with pytest.raises(HTTPException) as revoked:
            await authorize_workflow_execution(request, capability, resolve=resolve)
        assert revoked.value.detail['code'] == 'runtime_model_workflow_access_revoked'
        assert resolved_scopes == [recipient['active_organization_id']]
        app.state.openfga_client.tuples.add(source_edge)
        deletion = await client.delete('/api/v1/workflows/' + workflow, headers=_headers(owner_headers))
        assert deletion.status_code == 409, deletion.text

        from vibecanvas_api.authorization.projection import collect_structural_projection
        from vibecanvas_api.authorization.mutations import MutationEdge
        workflow_edge = MutationEdge(owner['active_organization_id'], 'workflow', workflow,
            'operator', 'service_account', str(dep['service_account_id']))
        async with session_scope(tenant_id=owner['active_organization_id']) as session:
            expected_edges = await collect_structural_projection(session, organization_id=owner['active_organization_id'])
            assert workflow_edge in expected_edges
        async with session_scope(tenant_id=recipient['active_organization_id']) as session:
            expected_edges = await collect_structural_projection(session, organization_id=recipient['active_organization_id'])
            assert workflow_edge not in expected_edges
        async with pg_engine.connect() as connection:
            edge_owner = (await connection.execute(text("SELECT tenant_id FROM authz_mutations WHERE object_type='workflow' "
                "AND object_id=:wf AND subject_type='service_account' AND subject_id=:account AND desired_state='present'"),
                {'wf': workflow, 'account': str(dep['service_account_id'])})).scalar_one()
            assert str(edge_owner) == owner['active_organization_id']

        # A pending revoke in the SOURCE organization must deny immediately,
        # even before the external tuple write has caught up.
        from vibecanvas_api.authorization.mutations import AuthzMutationCoordinator
        coordinator = AuthzMutationCoordinator(client=app.state.openfga_client,
            organization_id=owner['active_organization_id'])
        async with session_scope(tenant_id=owner['active_organization_id']) as session:
            await coordinator.enqueue_structural(session=session, actor_type='user', actor_id=owner['user_id'],
                edge=workflow_edge, desired_present=False, idempotency_key='revoke-' + uuid.uuid4().hex,
                source_revision='test-pending-revocation')
        assert source_edge in app.state.openfga_client.tuples
        with pytest.raises(HTTPException) as pending:
            await authorize_workflow_execution(request, capability, resolve=resolve)
        assert pending.value.detail['code'] == 'runtime_model_workflow_access_revoked'
        assert resolved_scopes == [recipient['active_organization_id']]


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['viewer', 'editor', 'operator', 'manager'])
@pytest.mark.parametrize('kind', ['schedule', 'batch'])
async def test_shared_workflow_schedule_creation_and_execution(pg_engine, monkeypatch, role, kind):
    monkeypatch.setattr(config, 'resource_sharing_enabled', True)
    app = build_app()
    app.state.openfga_client = Store()
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        owner_headers, owner = await _register(client, 'schedule_source_owner')
        recipient_headers, recipient = await _register(client, 'schedule_creator')
        made = await client.post('/api/v1/workflows', json={'name': 'Shared schedule source'}, headers=_headers(owner_headers))
        assert made.status_code == 201, made.text
        workflow = made.json()['wf_id']
        resolved = await client.post(f'/api/v1/resource-access/workflow/{workflow}/resolve-target',
            headers=_headers(owner_headers), json={'target_type': 'user', 'identifier': recipient['email']})
        assert resolved.status_code == 200, resolved.text
        grant = await client.post(f'/api/v1/workflows/{workflow}/access',
            headers=_headers(owner_headers, **{'Idempotency-Key': uuid.uuid4().hex}),
            json={'relation': role, 'resolution_token': resolved.json()['target']['resolution_token']})
        assert grant.status_code == 201, grant.text
        if kind == 'batch':
            created = await client.post(f'/api/v1/workflows/{workflow}/batch', headers=_headers(recipient_headers),
                json={'version': 'v1.sv0', 'data_source': {'rows': []}, 'column_mapping': {}})
        else:
            created = await client.post('/api/v1/tasks/scheduled-runs', headers=_headers(recipient_headers), json={
                'name': 'Recipient schedule', 'workflow_id': workflow, 'schedule_type': 'interval',
                'interval_seconds': 3600, 'version': 'v1.sv0'})
        if role in {'viewer', 'editor'}:
            assert created.status_code == 404, created.text
            return
        assert created.status_code == 201, created.text
        if kind == 'batch':
            identifier = created.json()['task_id']
            from vibecanvas_api.storage.db import session_scope
            from vibecanvas_api.storage.repo_tasks import TasksRepo
            async with session_scope(tenant_id=recipient['active_organization_id']) as session:
                task = await TasksRepo(session).get(uuid.UUID(identifier))
                assert str(task.tenant_id) == recipient['active_organization_id']
                assert str(task.workflow_tenant_id) == owner['active_organization_id']
                assert str(task.user_id) == recipient['user_id']
                assert task.payload['workflow_snapshot']['version'] == 'v1.sv0'
            assert (await client.get('/api/v1/tasks/' + identifier, headers=_headers(owner_headers))).status_code == 404
            return
        identifier = created.json()['task']['id']
        base = '/api/v1/tasks/scheduled-runs/' + identifier
        edited = await client.patch(base, headers=_headers(recipient_headers), json={'version': 'v1.sv0'})
        assert edited.status_code == 200, edited.text
        running = await client.post(base + '/run-now', headers=_headers(recipient_headers))
        assert running.status_code == 202, running.text
        execution_id = running.json()['execution']['id']
        assert (await client.get('/api/v1/tasks/' + identifier, headers=_headers(owner_headers))).status_code == 404
        async with pg_engine.connect() as connection:
            row = (await connection.execute(text("""SELECT t.tenant_id,t.workflow_tenant_id,t.user_id,
                s.workflow_tenant_id AS schedule_source,e.workflow_tenant_id AS execution_source,
                e.tenant_id AS execution_tenant,a.tenant_id AS account_tenant,a.created_by
                FROM tasks t JOIN task_schedules s ON s.task_id=t.id
                JOIN service_accounts a ON a.service_account_id=t.service_account_id
                JOIN scheduled_run_executions e ON e.schedule_id=s.id WHERE t.id=:id AND e.id=:execution"""),
                {'id': uuid.UUID(identifier), 'execution': uuid.UUID(execution_id)})).one()
            assert str(row.tenant_id) == str(row.execution_tenant) == str(row.account_tenant) == recipient['active_organization_id']
            assert str(row.workflow_tenant_id) == str(row.schedule_source) == str(row.execution_source) == owner['active_organization_id']
            assert str(row.user_id) == str(row.created_by) == recipient['user_id']
        from vibecanvas_api.storage.db import session_scope
        from vibecanvas_api.storage.repo_tasks import TasksRepo
        async with session_scope(tenant_id=recipient['active_organization_id']) as session:
            execution = await TasksRepo(session).get_scheduled_execution(uuid.UUID(execution_id))
            assert execution.workflow_snapshot['version'] == 'v1.sv0'
            assert execution.workflow_snapshot['workflow']['__meta__']['workflow_id'] == workflow
        deleted = await client.delete('/api/v1/workflows/' + workflow, headers=_headers(owner_headers))
        assert deleted.status_code == 409, deleted.text
