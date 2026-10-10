"""Native history branching, without starting a model turn or inheriting a Goal."""
from __future__ import annotations

from .codex_app_server import CodexAppServer, CodexAppServerError


async def fork_native_thread(
    client: CodexAppServer, *, source_thread_id: str, last_turn_id: str,
    overrides: dict,
) -> str:
    """Fork exactly once; let the caller report failures without replaying it."""
    result = await client.request('thread/fork', {
        **overrides,
        'threadId': source_thread_id,
        'lastTurnId': last_turn_id,
        'excludeTurns': True,
        'deferGoalContinuation': True,
    }, timeout_s=45.0)
    thread = result.get('thread')
    target = thread.get('id') if isinstance(thread, dict) else None
    if not isinstance(target, str) or not target or target == source_thread_id:
        raise RuntimeError('native_branch_invalid_thread')
    # deferGoalContinuation prevents automatic work during the fork itself.
    # Clear the inherited Goal before a later explicit user turn can resume it.
    try:
        await client.request('thread/goal/clear', {'threadId': target}, timeout_s=10.0)
    except CodexAppServerError as exc:
        # Only a missing source rollout may use durable-history recovery.
        # A failure after creating the target must not start another thread.
        raise RuntimeError(f'native_branch_goal_clear_failed: {exc}') from exc
    return target
