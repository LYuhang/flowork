"""Authorize and prepare local CLI inputs; never start or own execution."""
from copy import deepcopy

from vibecanvas_api.authorization.types import Action
from vibecanvas_api.services.agent_resources.authorization import require_workflow_action
from vibecanvas_api.services.agent_resources.workflow_transfer import read_workflow_snapshot
from vibecanvas_api.services.agent_resources.workflow_graph import validate_workflow_for_context
from vibecanvas_api.services.llm_credentials_inject import inject_into_run_context_async
from vibecanvas_api.services.workflow_resources import prepare_execution_resources
from vibecanvas_api.agents.tools._session_fs import _require_session
from vibecanvas_api.services.workflow_sandbox_runner import prepare_code_pythonpath
from vibecanvas_api.flowork_cli.cli import error


async def prepare(context, arguments):
    workflow_id = arguments['workflow_id']
    await require_workflow_action(context, workflow_id, Action.EXECUTE)
    snapshot = await read_workflow_snapshot(context, workflow_id=workflow_id, major=arguments['major'])
    workflow = deepcopy(arguments.get('workflow', snapshot['workflow']))
    if arguments.get('node'):
        from vibecanvas_api.services.agent_resources.node_execution import select_execution_node
        workflow = await select_execution_node(workflow, arguments['node'], context)
        errors = []
    else:
        errors = await validate_workflow_for_context(workflow, context)
    if errors:
        return {**error('invalid_workflow', 'Workflow validation failed.', 'Inspect the node definitions.'), 'errors': errors}
    session = await _require_session(context)
    extra = await inject_into_run_context_async({}, workflow, context.tenant_id,
        user_id=context.username, workflow_id=workflow_id,
        execution_id=workflow_id, execution_resource_type='workflow')
    resources = await prepare_execution_resources(sandbox_session=session, workflow=workflow,
        tenant_id=context.tenant_id, user_id=context.username, workflow_id=workflow_id,
        execution_id=workflow_id, execution_resource_type='workflow')
    if resources:
        extra['workflow_resources'] = resources
    code_path = await prepare_code_pythonpath(workflow, session=session)
    if code_path:
        extra['code_pythonpath'] = code_path
    result = {'workflow': workflow, 'context': extra, 'id': workflow_id,
            'version': snapshot['version'], 'source': 'file' if 'workflow' in arguments else 'saved'}

    if any(isinstance(node, dict) and node.get('node_type') == 'HumanApprovalNode' for node in workflow.values()):
        from vibecanvas_api.config import config
        from .model_capability import authorization_model_generation
        from .local_approval_capability import mint_local_approval_capability, workflow_digest
        result['approval_service'] = {
            'url': config.mcp.platform_internal_base_url.rstrip('/') + '/api/internal/local-workflow-approvals/v1',
            'capability': mint_local_approval_capability(organization_id=context.tenant_id,
                user_id=context.username, workflow_id=workflow_id, run_id=arguments['run_id'], sandbox_id=session.wf_id,
                workflow_digest=workflow_digest(workflow), workflow_version=snapshot['version'],
                authorization_generation=authorization_model_generation(model_id=config.openfga_authorization_model_id),
                secret=config.signing_secret, ttl_s=config.mcp.runtime_model_capability_ttl_s),
        }
    return result
