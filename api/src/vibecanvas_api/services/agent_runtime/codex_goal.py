"""Native Codex goal state. The server, not Flowork, schedules continuation turns."""
from __future__ import annotations

from typing import Any

from .goal_commands import GoalCommand


async def read_goal(client: Any, thread_id: str) -> dict | None:
    result = await client.request("thread/goal/get", {"threadId": thread_id}, timeout_s=10.0)
    return result.get("goal")


async def apply_goal_command(client: Any, thread_id: str, command: GoalCommand) -> dict | None:
    if command.action == "status":
        return await read_goal(client, thread_id)
    if command.action == "clear":
        await client.request("thread/goal/clear", {"threadId": thread_id}, timeout_s=10.0)
        return None
    params: dict[str, Any] = {"threadId": thread_id, "status": "active"}
    if command.action == "edit":
        goal = await read_goal(client, thread_id)
        if goal is None:
            raise ValueError("goal_not_found")
        params["objective"] = command.objective
        params["status"] = goal["status"]
    elif command.action == "new":
        params["objective"] = command.objective
    else:
        goal = await read_goal(client, thread_id)
        if goal is None:
            raise ValueError("goal_not_found")
        if goal.get("status") == "complete":
            raise ValueError("goal_already_complete")
    result = await client.request("thread/goal/set", params, timeout_s=10.0)
    return result.get("goal")


async def pause_active_goal(client: Any, thread_id: str) -> dict | None:
    goal = await read_goal(client, thread_id)
    if goal and goal.get("status") == "active":
        result = await client.request(
            "thread/goal/set", {"threadId": thread_id, "status": "paused"}, timeout_s=10.0,
        )
        return result.get("goal")
    return goal


class GoalContinuity:
    """Carry a chat goal across native thread forks without resetting its usage."""

    def __init__(self, snapshot: dict | None):
        self.snapshot = snapshot
        self.time_offset = 0
        self.token_offset = 0

    async def restore(self, client: Any, thread_id: str, goal: dict | None) -> dict | None:
        saved = self.snapshot
        if not saved:
            return goal
        if goal is not None:
            if saved.get('threadId') == thread_id:
                self.time_offset = saved.get('timeOffsetSeconds', 0)
                self.token_offset = saved.get('tokenOffset', 0)
            return goal
        self.time_offset = saved.get('timeUsedSeconds', 0)
        self.token_offset = saved.get('tokensUsed', 0)
        params = {'threadId': thread_id, 'objective': saved['objective'], 'status': saved['status']}
        budget = saved.get('tokenBudget')
        if budget is not None:
            params['tokenBudget'] = max(1, budget - self.token_offset)
            if budget <= self.token_offset and params['status'] == 'active':
                params['status'] = 'budgetLimited'
        result = await client.request('thread/goal/set', params, timeout_s=10.0)
        return result.get('goal')

    def project(self, goal: dict | None) -> dict | None:
        if goal is None:
            return None
        if self.snapshot and goal.get('objective') != self.snapshot.get('objective'):
            self.snapshot = None
            self.time_offset = self.token_offset = 0
        result = dict(goal)
        result['timeUsedSeconds'] = goal.get('timeUsedSeconds', 0) + self.time_offset
        result['tokensUsed'] = goal.get('tokensUsed', 0) + self.token_offset
        result['timeOffsetSeconds'] = self.time_offset
        result['tokenOffset'] = self.token_offset
        if self.snapshot:
            result['createdAt'] = self.snapshot.get('createdAt', goal.get('createdAt'))
        if goal.get('tokenBudget') is not None:
            result['tokenBudget'] = goal['tokenBudget'] + self.token_offset
        return result
