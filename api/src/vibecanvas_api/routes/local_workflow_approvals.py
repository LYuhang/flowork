"""Approval business transport for sandbox-local Workflow CLI commands."""
import json

from fastapi import APIRouter, HTTPException, Request

from vibecanvas_api.config import config
from vibecanvas_api.routes.runtime_mcp_broker import _bounded_body, _extract_capability
from vibecanvas_api.services.agent_runtime.local_approval_capability import verify_local_approval_capability
from vibecanvas_api.services.local_approval_records import append_evidence, decisions
from vibecanvas_api.services.workflow_execution_authorization import authorize_workflow_execution
from vibecanvas_api.storage.workflow_history_repo import HistoryConflict

router = APIRouter(tags=['local-workflow-approvals'])


@router.post('/api/internal/local-workflow-approvals/v1/{operation}', include_in_schema=False)
async def local_workflow_approvals(request: Request, operation: str):
    if operation not in {'events', 'decisions'}:
        raise HTTPException(404)
    capability = verify_local_approval_capability(_extract_capability(request), secret=config.signing_secret)
    if capability is None:
        raise HTTPException(401, detail={'code': 'local_approval_capability_invalid'})
    try:
        body = json.loads(await _bounded_body(request))
        if not isinstance(body, dict):
            raise ValueError('invalid request')
        if operation == 'decisions' and set(body) != {'index'}:
            raise ValueError('invalid decision query')
        async def resolve(*, session, **kwargs):
            if operation == 'events':
                return await append_evidence(session=session, capability=capability, body=body)
            return await decisions(session=session, capability=capability, index=body['index'])
        return await authorize_workflow_execution(request, capability, resolve=resolve)
    except HistoryConflict as exc:
        raise HTTPException(409, detail={'code': 'local_approval_conflict'}) from exc
    except KeyError as exc:
        raise HTTPException(404, detail={'code': 'local_approval_unavailable'}) from exc
    except (ValueError, TypeError, UnicodeError) as exc:
        raise HTTPException(422, detail={'code': 'local_approval_invalid_request'}) from exc
