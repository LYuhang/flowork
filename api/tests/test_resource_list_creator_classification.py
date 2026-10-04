"""Company shares must not classify the creator as a recipient."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid
import pytest
from vibecanvas_api.routes import resource_access

@pytest.mark.asyncio
async def test_company_grant_does_not_make_own_knowledge_a_received_share(monkeypatch):
    creator = uuid.uuid4()
    row = SimpleNamespace(name='Company resource', description='', user_id=creator,
                          updated_at=datetime.now(timezone.utc), created_at=None)
    monkeypatch.setattr(resource_access, 'KbRepo', lambda session: SimpleNamespace(get_active=AsyncMock(return_value=row)))
    monkeypatch.setattr(resource_access, 'decision_allows_content', lambda decision: True)
    provenance = SimpleNamespace(build=AsyncMock())
    result = await resource_access._shared_resource_card(
        object(), resource_type='knowledge_base', resource_id=str(uuid.uuid4()),
        decision=object(), provenance_builder=provenance, recipient_user_id=str(creator),
    )
    assert result is None
    provenance.build.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_pagination_skips_invisible_candidates_before_paging(monkeypatch):
    from vibecanvas_api.storage import shared_resource_locator
    from vibecanvas_api.schemas.access import SharedResourceOut
    calls = []
    workspace = str(uuid.uuid4())
    async def roots(user_id, *, active_organization_id, resource_type, limit, offset):
        assert active_organization_id == workspace
        calls.append(offset)
        return list(range(205))[offset:offset + limit]
    def card(id):
        return SharedResourceOut(resource_type='workflow', resource_id=str(id), name=str(id),
            description='', updated_at=datetime.now(timezone.utc),
            access={'capabilities': ['view'], 'effective_role': 'viewer', 'source': 'shared'},
            provenance={'ownership_scope': 'organization', 'origin_type': 'created',
                        'owner': {'type': 'organization', 'display_name': 'Company'}})
    async def authorized(request, auth, candidates):
        # First 200 locators are revoked, deleted, or the recipient's own.
        return [card(i) for i in candidates if i >= 200]
    monkeypatch.setattr(shared_resource_locator, 'shared_resource_roots', roots)
    monkeypatch.setattr(resource_access, '_authorized_shared_cards', authorized)
    page = await resource_access.list_shared_resources(
        request=object(), auth=SimpleNamespace(user_id=str(uuid.uuid4()), active_organization_id=workspace),
        session=object(), resource_type='workflow', limit=2, offset=1,
    )
    assert [item.resource_id for item in page.items] == ['201', '202']
    assert page.next_offset == 3
    assert calls == [0, 100, 200]
