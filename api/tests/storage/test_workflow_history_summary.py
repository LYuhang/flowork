"""Real database coverage for batched history summaries and cursor boundaries."""
from datetime import datetime
import uuid

import pytest

from tests.storage.test_workflow_history import owner
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo


@pytest.mark.asyncio
async def test_summary_batch_exceeds_old_limit_and_retains_cursor_and_filters(pg_engine):
    tenant, actor, _ = await owner()
    source = 'history-batch-' + uuid.uuid4().hex
    async with session_scope(tenant_id=tenant) as session:
        repo = WorkflowHistoryRepo(session)
        for index in range(125):
            await repo.create(
                execution_id=str(uuid.uuid4()), tenant_id=tenant, wf_id=source,
                source_type='workflow', source_id=source, initiator_user_id=actor,
                workflow={'private': 'workflow definition'}, inputs={'private': index},
                approvers={}, input_index=index,
            )
    async with session_scope(tenant_id=tenant) as session:
        repo = WorkflowHistoryRepo(session)
        complete = await repo.history(source_type='workflow', source_id=source, limit=1000)
        assert len(complete['items']) == 125
        assert complete['has_more'] is False
        assert all(set(item) == {'id', 'created_at', 'status', 'input_index', 'pending_approvals'}
                   for item in complete['items'])
        first = await repo.history(source_type='workflow', source_id=source, limit=100)
        assert len(first['items']) == 100
        assert first['has_more'] is True
        boundary = first['items'][-1]
        rest = await repo.history(source_type='workflow', source_id=source, limit=1000,
            before=(datetime.fromisoformat(boundary['created_at']), boundary['id']))
        assert rest['has_more'] is False
        assert first['items'] + rest['items'] == complete['items']
        empty = await repo.history(source_type='workflow', source_id=source,
            statuses=['failed'], limit=1000)
        assert empty == {'items': [], 'has_more': False}
