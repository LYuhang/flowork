from uuid import uuid4

import pytest

from vibecanvas_api.services.agent_runtime.workflow_mcp_capability import (
    mint_workflow_mcp_capability, verify_workflow_mcp_capability,
)
from vibecanvas_api.services.agent_runtime.workflow_model_capability import verify_runtime_workflow_model_capability


def claims():
    user = str(uuid4())
    return dict(organization_id=str(uuid4()), user_id=user, workflow_id="wf-test",
        execution_id="run-test", execution_resource_type="agent_run", principal_type="user",
        principal_id=user, principal_generation=0, authorization_generation="generation",
        server_id=str(uuid4()), tools_fingerprint="a" * 64)


def test_mcp_capability_binds_server_identity_expiry_and_audience():
    value = claims()
    token = mint_workflow_mcp_capability(**value, secret="secret", ttl_s=60, now=100)
    verified = verify_workflow_mcp_capability(token, secret="secret", server_id=value["server_id"], now=101)
    assert verified and verified.execution_id == "run-test"
    assert verify_workflow_mcp_capability(token, secret="secret", server_id=str(uuid4()), now=101) is None
    assert verify_workflow_mcp_capability(token, secret="other", server_id=value["server_id"], now=101) is None
    assert verify_workflow_mcp_capability(token, secret="secret", server_id=value["server_id"], now=160) is None
    assert verify_runtime_workflow_model_capability(token, secret="secret", now=101) is None


@pytest.mark.parametrize("patch", [
    {"principal_id": str(uuid4())}, {"principal_type": "service_account", "principal_generation": 0},
    {"execution_resource_type": "chat"}, {"tools_fingerprint": "anything"}, {"expires_at": 0},
])
def test_invalid_or_escalated_claims_are_rejected(patch):
    with pytest.raises((ValueError, TypeError)):
        mint_workflow_mcp_capability(**{**claims(), **patch}, secret="secret", ttl_s=60, now=100)


def test_background_service_account_generation_is_preserved():
    value = {**claims(), "principal_type": "service_account", "principal_id": str(uuid4()),
             "principal_generation": 3, "execution_resource_type": "deployment_invocation"}
    token = mint_workflow_mcp_capability(**value, secret="secret", ttl_s=60, now=100)
    assert verify_workflow_mcp_capability(token, secret="secret", server_id=value["server_id"], now=101).principal_generation == 3
