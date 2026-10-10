"""One dispatch after successful completion; uncertain attempts are never replayed."""
from collections.abc import Awaitable, Callable
import asyncio

from fastapi import HTTPException
import structlog

from vibecanvas_api.storage.chat_queue_repo import ChatQueueRepo
from vibecanvas_api.storage.db import session_scope


logger = structlog.get_logger(__name__)


async def dispatch_queued_input(*, tenant_id: str, user_id: str, chat_id: str,
                                finished_run_id: str,
                                submit: Callable[[dict, str, dict | None], Awaitable[None]]) -> None:
    # Commit the single claim before any external effect. A cancelled process
    # leaves a claimed receipt, never another eligible item for startup replay.
    async with session_scope(tenant_id=tenant_id, user_id=user_id) as session:
        queued = await ChatQueueRepo(session, user_id).claim(chat_id, finished_run_id)
    if queued is None:
        return
    error_code = None
    rejected = False
    cancelled = False
    try:
        await submit(queued['message'], queued['input_id'], queued['authority'])
    except asyncio.CancelledError:
        error_code = 'submission_interrupted'
        cancelled = True
    except HTTPException as exc:
        error_code = exc.detail if isinstance(exc.detail, str) else f'http_{exc.status_code}'
        rejected = 400 <= exc.status_code < 500
    except Exception:
        logger.exception('chat_queue_submission_unconfirmed', chat_id=chat_id, input_id=queued['input_id'])
        error_code = 'submission_unconfirmed'
    # Even a failed response may follow a committed AgentRun. Link that run
    # instead of claiming the input was not sent, or submitting it again.
    async with session_scope(tenant_id=tenant_id, user_id=user_id) as session:
        await ChatQueueRepo(session, user_id).resolve_dispatch(
            chat_id, queued['input_id'], error_code=error_code, rejected=rejected,
        )
    if cancelled:
        raise asyncio.CancelledError
