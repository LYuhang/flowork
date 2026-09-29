"""Cookie-authenticated, origin-checked deployment PTY WebSocket bridge."""
from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from contextlib import suppress

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from vibecanvas_api.auth.deps import current_user, tenant_db
from vibecanvas_api.auth.session_security import validate_browser_origin
from vibecanvas_api.authorization.dependencies import get_authz_service
from vibecanvas_api.routes.deployments import _authorize_deployment, Action, ConsistencyPreference
from vibecanvas_api.services.sandbox.manager import get_sandbox_manager
from vibecanvas_api.storage.repo_deployments import DeploymentsRepo

router = APIRouter(prefix='/api/v1/deployments', tags=['deployments'])


async def terminal_identity(ws: WebSocket, dep_id: uuid.UUID) -> tuple[dict, tuple]:
    # WebSocket handshakes cannot set our normal CSRF header. Enforce the
    # browser's Origin explicitly, then resolve the HttpOnly session cookie.
    # No bearer/session credential is accepted in the URL or WebSocket body.
    request = Request({**ws.scope, 'type': 'http', 'method': 'POST'})
    validate_browser_origin(request)
    request.scope['method'] = 'GET'
    ctx = await current_user(request, creds=None)
    async for session in tenant_db(request, ctx):
        service = await get_authz_service(request, ctx, session)
        await _authorize_deployment(request=request, ctx=ctx, service=service,
            deployment_id=dep_id, action=Action.UPDATE,
            consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
        dep = await DeploymentsRepo(session).get(dep_id)
        if not dep or not dep['enabled'] or not dep.get('active_revision_id'):
            raise HTTPException(409, 'terminal_instance_unavailable')
        target = {'tenant_id': str(dep['tenant_id']), 'deployment_id': str(dep_id),
                  'revision_id': str(dep['active_revision_id']), 'user_id': str(ctx.user_id)}
    binding = (ctx.user_id, ctx.session_id, ctx.session_generation, ctx.active_organization_id)
    return target, binding


@router.websocket('/{dep_id}/terminal')
async def deployment_terminal(ws: WebSocket, dep_id: uuid.UUID):
    target = None
    receiver = None
    accepted = False
    manager = get_sandbox_manager()
    identifier = str(uuid.uuid4())
    try:
        target, binding = await terminal_identity(ws, dep_id)
        await ws.accept()
        accepted = True
        result = await manager.deployment_terminal(**target, terminal_id=identifier,
            action='open', columns=80, rows=24)
        if not result.get('ok'):
            raise RuntimeError('terminal_open_failed')
        await ws.send_json({'type': 'ready', 'revision_id': target['revision_id']})
        unacknowledged = 0
        next_auth = time.monotonic() + 5
        receiver = asyncio.create_task(ws.receive_text())
        while True:
            if time.monotonic() >= next_auth:
                current, current_binding = await terminal_identity(ws, dep_id)
                if current != target or current_binding != binding:
                    await ws.send_json({'type': 'instance_changed'})
                    break
                next_auth = time.monotonic() + 5
            done, _ = await asyncio.wait({receiver}, timeout=0.1)
            if done:
                raw = receiver.result()
                if len(raw) > 32_768:
                    raise ValueError('terminal_message_too_large')
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError('invalid_terminal_message')
                kind = message.get('type')
                if kind == 'input':
                    encoded = message.get('data', '')
                    if not isinstance(encoded, str) or len(encoded) > 24_000:
                        raise ValueError('terminal_input_too_large')
                    remaining = base64.b64decode(encoded, validate=True)
                    deadline = time.monotonic() + 4
                    while remaining:
                        result = await manager.deployment_terminal(**target, terminal_id=identifier,
                            action='write', data=base64.b64encode(remaining).decode('ascii'))
                        if not result.get('ok') or time.monotonic() >= deadline:
                            raise RuntimeError('terminal_input_blocked')
                        written = result.get('written', 0)
                        if not isinstance(written, int) or written < 0 or written > len(remaining):
                            raise RuntimeError('invalid_terminal_write')
                        remaining = remaining[written:]
                        if not written:
                            await asyncio.sleep(0.02)
                elif kind == 'resize':
                    result = await manager.deployment_terminal(**target, terminal_id=identifier,
                        action='resize', columns=message.get('columns'), rows=message.get('rows'))
                    if not result.get('ok'):
                        raise RuntimeError('terminal_resize_failed')
                elif kind == 'ack':
                    size = message.get('bytes')
                    if type(size) is not int or not 0 < size <= unacknowledged:
                        raise ValueError('invalid_terminal_ack')
                    unacknowledged -= size
                elif kind == 'disconnect':
                    break
                else:
                    raise ValueError('unknown_terminal_message')
                receiver = asyncio.create_task(ws.receive_text())
            # Bound output buffered in the browser. Resume only after xterm
            # confirms rendering; a hidden/throttled tab cannot grow forever.
            if unacknowledged >= 65_536:
                continue
            result = await manager.deployment_terminal(**target, terminal_id=identifier, action='read')
            if not result.get('ok'):
                # Retirement can close the PTY before the next periodic check.
                # Preserve the explicit reconnect-to-new-instance UX.
                current, current_binding = await terminal_identity(ws, dep_id)
                if current != target or current_binding != binding:
                    await ws.send_json({'type': 'instance_changed'})
                    break
                raise RuntimeError('terminal_closed')
            data = base64.b64decode(result.get('data', ''), validate=True)
            if data:
                await ws.send_bytes(data)
                unacknowledged += len(data)
            if result.get('exit_code') is not None:
                await ws.send_json({'type': 'exit', 'exit_code': result['exit_code']})
                break
    except WebSocketDisconnect:
        pass
    except (HTTPException, ValueError, RuntimeError, json.JSONDecodeError):
        if accepted:
            with suppress(Exception):
                # Do not forward RPC internals, paths, or authorization details.
                await ws.send_json({'type': 'error', 'code': 'terminal_connection_ended'})
    finally:
        if receiver is not None:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
        if target is not None:
            with suppress(Exception):
                await asyncio.wait_for(manager.deployment_terminal(**target,
                    terminal_id=identifier, action='close'), timeout=6)
        with suppress(Exception):
            await ws.close(code=1000 if accepted else 1008)
