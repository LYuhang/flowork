"""One explicit input dispatch by the process already executing the turn."""
import asyncio

from vibecanvas_api.storage.chat_inputs_repo import ChatInputsRepo
from vibecanvas_api.storage.db import session_scope
from .protocol import RuntimeSteerInput, RuntimeTurnRequest


async def deliver_pending_input(runtime, request: RuntimeTurnRequest) -> dict | None:
    async with session_scope(tenant_id=request.tenant_id, user_id=request.user_id) as session:
        receipt = await ChatInputsRepo(session, request.user_id).claim_pending(request.chat_id, request.turn_id)
    # The claim transaction has committed. Never re-enqueue this input on error.
    if receipt is None:
        return None
    if receipt['status'] == 'rejected':
        return {'input_id': receipt['input_id'], 'status': 'rejected', 'error': receipt['error_code']}
    try:
        await asyncio.wait_for(runtime.respond(RuntimeSteerInput(
            input_id=receipt['input_id'], chat_id=request.chat_id,
            turn_id=request.turn_id, text=receipt['text'],
        )), timeout=5)
    except (TimeoutError, ConnectionError, OSError, LookupError, RuntimeError):
        # An interrupted write does not establish whether the runtime received
        # it. The native acknowledgment may later settle this unknown result.
        return {'input_id': receipt['input_id'], 'status': 'unknown', 'error': 'steer_transport_failed'}
    return None  # Native USER_INPUT_STATUS supplies the actual acknowledgment.
