"""Real isolated PostgreSQL identity fences used by the Browser CDP endpoint.

These fixtures create users only in pytest's disposable database. This is not
a second-user extension/UI acceptance test and never touches the native app DB.
"""

from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import text

from vibecanvas_api.config import config
from vibecanvas_api.services.agent_runtime.model_capability import authorization_model_generation
from vibecanvas_api.services.agent_resources.context import resolve_identity


@pytest.mark.asyncio
async def test_real_identity_rejects_cross_user_org_membership_and_stale_generations(client, pg_engine):
    identities = []
    for index in range(2):
        client.cookies.clear()
        registered = await client.post("/api/v1/auth/register", json={
            "email": f"cdp_fence_{uuid.uuid4().hex}@example.com",
            "username": f"CDP isolated fixture {index}", "password": "fixturepw123",
        })
        assert registered.status_code in (200, 201)
        response = await client.get("/api/v1/auth/me", headers={
            "Authorization": f"Bearer {registered.json()['session_token']}",
        })
        assert response.status_code == 200
        me = response.json()
        identities.append(SimpleNamespace(
            organization_id=me["active_organization_id"], user_id=me["user_id"],
            session_id=me["session"]["session_id"], session_generation=me["session"]["generation"],
            membership_id=me["membership"]["membership_id"],
            authorization_generation=authorization_model_generation(model_id=config.openfga_authorization_model_id),
        ))

    own, foreign = identities
    # Positive controls make it impossible to pass solely because identity
    # resolution or the test's database setup is globally unavailable.
    for capability in identities:
        resolved = await resolve_identity(capability)
        assert resolved.user_id == capability.user_id
        assert resolved.active_organization_id == capability.organization_id

    for field, value in [
        ("user_id", foreign.user_id),
        ("organization_id", foreign.organization_id),
        ("session_id", foreign.session_id),
        ("membership_id", foreign.membership_id),
        ("session_generation", own.session_generation + 1),
        ("authorization_generation", "stale-model-generation"),
    ]:
        mixed = SimpleNamespace(**{**vars(own), field: value})
        with pytest.raises(PermissionError):
            await resolve_identity(mixed)

    # Revoke a previously valid identity by changing durable state, not by
    # configuring a mock validator to return False.
    async with pg_engine.begin() as connection:
        changed = await connection.execute(text(
            "UPDATE sessions SET generation=generation+1 WHERE session_id=:id"
        ), {"id": uuid.UUID(own.session_id)})
        assert changed.rowcount == 1
    with pytest.raises(PermissionError, match="identity has been revoked"):
        await resolve_identity(own)
    current = SimpleNamespace(**{**vars(own), "session_generation": own.session_generation + 1})
    assert (await resolve_identity(current)).user_id == own.user_id
    assert (await resolve_identity(foreign)).user_id == foreign.user_id
