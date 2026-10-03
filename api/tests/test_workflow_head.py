"""Branch pointer polling stays isolated and authorized without loading graphs."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from vibecanvas_api.routes import workflows
from vibecanvas_api.authorization.types import Decision


@pytest.mark.asyncio
async def test_head_uses_explicit_major_and_checks_view_permission(monkeypatch):
    authorize = AsyncMock(return_value=SimpleNamespace(decision=Decision(
        allowed=True, capabilities=frozenset({workflows.Action.VIEW, workflows.Action.UPDATE}),
        effective_role='editor',
    )))
    monkeypatch.setattr(workflows, '_authorize_workflow', authorize)
    repo = SimpleNamespace(get_meta=AsyncMock(return_value={'active_v': 3, 'updated_at': 123.5}),
                           max_subversion=AsyncMock(return_value=7))
    result = await workflows.get_workflow_head('wf', None, major=2, repo=repo, auth=None, service=None)
    assert result.model_dump(mode='json') == {
        'major': 2, 'sub': 7, 'tree_revision': 123.5,
        'access': {'capabilities': ['update', 'view'], 'effective_role': 'editor', 'source': 'computed'},
    }
    repo.max_subversion.assert_awaited_once_with('wf', 2)
    assert authorize.await_args.kwargs['action'] == workflows.Action.VIEW
    result = await workflows.get_workflow_head('wf', None, major=None, repo=repo, auth=None, service=None)
    assert result.major == 3
    # Revocation changes access even when the branch and tree are unchanged.
    authorize.return_value = SimpleNamespace(decision=Decision(
        allowed=True, capabilities=frozenset({workflows.Action.VIEW}), effective_role='viewer',
    ))
    result = await workflows.get_workflow_head('wf', None, major=2, repo=repo, auth=None, service=None)
    assert result.sub == 7 and result.tree_revision == 123.5
    assert result.access.capabilities == [workflows.Action.VIEW]
    assert result.access.effective_role == 'viewer'
    authorize.side_effect = HTTPException(403)
    repo.get_meta.reset_mock()
    with pytest.raises(HTTPException) as exc:
        await workflows.get_workflow_head('wf', None, major=2, repo=repo, auth=None, service=None)
    assert exc.value.status_code == 403
    repo.get_meta.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('meta,sub', [(None, 0), ({'active_v': 1}, -1)])
async def test_missing_head_is_not_reported_as_subversion_zero(monkeypatch, meta, sub):
    monkeypatch.setattr(workflows, '_authorize_workflow', AsyncMock())
    repo = SimpleNamespace(get_meta=AsyncMock(return_value=meta), max_subversion=AsyncMock(return_value=sub))
    with pytest.raises(HTTPException) as exc:
        await workflows.get_workflow_head('wf', None, major=2, repo=repo, auth=None, service=None)
    assert exc.value.status_code == 404
