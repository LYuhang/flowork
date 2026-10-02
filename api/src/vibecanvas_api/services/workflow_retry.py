"""Explicit manual retry of the latest personal whole-workflow failure.

History remains immutable. Only encrypted server-owned successful events and
artifacts are reused; callers cannot supply outputs or resume someone else's run.
"""

import uuid
from sqlalchemy import text
from vibecanvas_engine.resume import successful_visits
from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo


async def latest_workflow_run(session, *, wf_id, user_id):
    repo = WorkflowHistoryRepo(session)
    # Node debugging shares the history table but cannot replace the last
    # whole-workflow execution. Page IDs to avoid loading an unbounded history.
    offset = 0
    while True:
        rows = (await session.execute(text("""SELECT id FROM workflow_execution_runs
            WHERE source_type='workflow' AND source_id=:wf AND wf_id=:wf
            AND initiator_user_id=:actor ORDER BY created_at DESC,id DESC LIMIT 50 OFFSET :offset"""),
            {"wf": wf_id, "actor": uuid.UUID(str(user_id)), "offset": offset})).scalars().all()
        for row in rows:
            detail = await repo.detail(str(row))
            if detail.get("node_id") is None:
                return detail
        if len(rows) < 50:
            return None
        offset += len(rows)


def retry_reason(detail, *, wf_id, user_id, workflow, inputs):
    if (not detail or detail["source_type"] != "workflow" or detail["wf_id"] != wf_id
            or detail["source_id"] != wf_id or detail["initiator_user_id"] != str(user_id)
            or detail.get("node_id") is not None or detail["status"] != "failed"
            or not detail.get("result")):
        return "no_failure"
    if detail["workflow"] != workflow:
        return "workflow_changed"
    if detail["inputs"] != inputs:
        return "inputs_changed"
    return None


async def load_retry_visits(session, source_id):
    repo = WorkflowHistoryRepo(session)
    visits, after = {}, 0
    while True:
        events = await repo.events(source_id, after=after, limit=500)
        visits.update(successful_visits(events))
        if len(events) < 500:
            return visits
        after = events[-1]["seq"]
