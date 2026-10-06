"""Resolve already-authorized run references to their durable resource workspace.

Execution history remains independently authorized. Resolving a run reference
here does not grant access to its parent resource or to another execution.
"""
import uuid

from sqlalchemy import select

from vibecanvas_api.services.workspace_storage import WorkspaceIdentity
from vibecanvas_api.storage.models import Workflow
from vibecanvas_api.storage.models_tasks import Task
from vibecanvas_api.storage.models_deployments import Deployment
from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo


def run_relative_path(identity: WorkspaceIdentity, path: str) -> str:
    if not path.startswith('/run/'):
        raise ValueError('Invalid run path')
    relative = path[len('/run/'):]
    if any(part in {'', '.', '..'} for part in relative.split('/')):
        raise ValueError('Invalid run path')
    if identity.kind == 'workflow' and relative.split('/')[0] == 'chats':
        raise ValueError('Chat files are not run artifacts')
    return relative


async def resolve_run_workspace(session, run_id: str) -> WorkspaceIdentity:
    if run_id.startswith('deployment-run-'):
        kind, resource_id = 'deployment', str(uuid.UUID(run_id.removeprefix('deployment-run-')))
    elif run_id.startswith('task-run-'):
        kind, resource_id = 'task', str(uuid.UUID(run_id.removeprefix('task-run-')))
    else:
        try:
            execution_id = str(uuid.UUID(run_id))
        except ValueError:
            kind, resource_id = 'workflow', run_id
        else:
            execution = await WorkflowHistoryRepo(session).get(execution_id)
            if execution is None:
                raise FileNotFoundError('Execution workspace not found')
            kind, resource_id = execution['source_type'], str(execution['source_id'])
    if kind == 'workflow':
        statement = select(Workflow.tenant_id).where(Workflow.wf_id == resource_id)
    elif kind in {'task', 'deployment'}:
        model = Task if kind == 'task' else Deployment
        resource_id = str(uuid.UUID(resource_id))
        statement = select(model.tenant_id).where(model.id == uuid.UUID(resource_id))
    else:
        raise ValueError('Unsupported execution source')
    tenant_id = (await session.execute(statement)).scalar_one_or_none()
    if tenant_id is None:
        raise FileNotFoundError('Run resource not found')
    return WorkspaceIdentity(str(tenant_id), kind, resource_id)
