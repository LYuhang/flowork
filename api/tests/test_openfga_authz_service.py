from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from vibecanvas_api.authorization.openfga import OpenFgaAuthzService
from vibecanvas_api.authorization.openfga_client import OpenFgaReadPage
from vibecanvas_api.authorization.types import (
    Action,
    AuthorizationCheck,
    AuthzRequestContext,
    ConsistencyPreference,
    PrincipalRef,
    PrincipalType,
    RelationshipBinding,
    RelationshipSubject,
    RelationshipSubjectType,
    ResourceRef,
    ResourceType,
)


class _NoGuard:
    async def denies(self, **_kwargs) -> bool:
        return False

    async def denies_many(self, *, checks):
        return tuple(False for _ in checks)

    async def denied_resource_ids(self, **_kwargs):
        return frozenset()


@dataclass
class _FakeClient:
    allowed_relations: set[str] = field(default_factory=set)
    batch_calls: list = field(default_factory=list)
    listed: tuple[str, ...] = ("wf-1",)

    async def batch_check(self, checks, *, consistency):
        self.batch_calls.append((checks, consistency))
        return tuple(
            relation in self.allowed_relations
            for _, relation, _ in checks
        )

    async def list_objects(self, **_kwargs):
        return self.listed

    async def read(self, **_kwargs):
        return OpenFgaReadPage(())


@dataclass
class _FakeCoordinator:
    calls: list = field(default_factory=list)

    async def request_binding(self, **kwargs):
        self.calls.append(kwargs)


def _context(**overrides) -> AuthzRequestContext:
    values = {
        "active_organization_id": "org-1",
        "membership_status": "active",
        "membership_role": "member",
    }
    values.update(overrides)
    return AuthzRequestContext(**values)


def _service(client, coordinator=None) -> OpenFgaAuthzService:
    service = OpenFgaAuthzService(
        object(),  # root resources do not query the SQL parent resolver
        client,
        mutation_coordinator=coordinator,
    )
    service._revocation_guard = _NoGuard()
    return service


@pytest.mark.asyncio
async def test_single_resource_check_batches_guards_without_weakening_revocation():
    client = _FakeClient({"can_view", "can_update"})
    service = _service(client)

    class Guard:
        calls = 0

        async def denies_many(self, *, checks):
            self.calls += 1
            assert len(checks) > 1
            return tuple(action is Action.VIEW for _, action, _, _ in checks)

    guard = Guard()
    service._revocation_guard = guard
    decision = await service.check(
        PrincipalRef(PrincipalType.USER, "user-1"),
        Action.VIEW,
        ResourceRef(ResourceType.WORKFLOW, "wf-1", "org-1"),
        _context(consistency=ConsistencyPreference.HIGHER_CONSISTENCY),
    )
    assert guard.calls == 1
    assert not decision.allowed
    assert Action.VIEW not in decision.capabilities
    assert Action.UPDATE in decision.capabilities
    assert all(relation != "can_view" for _, relation, _ in client.batch_calls[0][0])
    assert client.batch_calls[0][1] is ConsistencyPreference.HIGHER_CONSISTENCY


@pytest.mark.asyncio
async def test_check_computes_complete_capabilities_and_custom_union():
    client = _FakeClient({
        "can_view_metadata",
        "can_view",
        "can_export",
        "can_update",
        "can_use",
        "can_execute",
        "can_cancel",
        "can_inspect_runs",
        "can_mount",
    })
    service = _service(client)
    decision = await service.check(
        PrincipalRef(PrincipalType.USER, "user-1"),
        Action.UPDATE,
        ResourceRef(ResourceType.WORKFLOW, "wf-1", "org-1"),
        _context(),
    )
    assert decision.allowed is True
    assert {Action.UPDATE, Action.EXECUTE} <= decision.capabilities
    assert Action.MANAGE_ACCESS not in decision.capabilities
    assert decision.effective_role == "custom"
    assert client.batch_calls[0][1] is ConsistencyPreference.MINIMIZE_LATENCY


@pytest.mark.asyncio
async def test_inactive_or_wrong_organization_fails_before_openfga():
    client = _FakeClient({"can_view"})
    service = _service(client)
    inactive = await service.check(
        PrincipalRef(PrincipalType.USER, "user-1"),
        Action.VIEW,
        ResourceRef(ResourceType.WORKFLOW, "wf-1", "org-1"),
        _context(membership_status="suspended"),
    )
    mismatch = await service.check(
        PrincipalRef(PrincipalType.USER, "user-1"),
        Action.VIEW,
        ResourceRef(ResourceType.WORKFLOW, "wf-1", "org-2"),
        _context(),
    )
    assert inactive.reason_code == "inactive_organization_membership"
    assert mismatch.reason_code == "organization_mismatch"
    assert client.batch_calls == []


@pytest.mark.asyncio
async def test_recipient_admission_is_exactly_one_cross_tenant_root():
    client = _FakeClient({"can_view"})
    service = _service(client)
    context = _context(
        admitted_resource_organization_id="org-owner",
        admitted_resource_type="workflow",
        admitted_resource_id="wf-shared",
    )

    shared = await service.check(
        PrincipalRef(PrincipalType.USER, "recipient"),
        Action.VIEW,
        ResourceRef(ResourceType.WORKFLOW, "wf-shared", "org-1"),
        context,
    )
    neighboring = await service.check(
        PrincipalRef(PrincipalType.USER, "recipient"),
        Action.VIEW,
        ResourceRef(ResourceType.WORKFLOW, "wf-neighbor", "org-1"),
        context,
    )

    assert shared.allowed is True
    assert neighboring.allowed is False
    assert neighboring.reason_code == "organization_mismatch"
    assert len(client.batch_calls) == 1


@pytest.mark.asyncio
async def test_batch_check_preserves_denials_and_higher_consistency():
    client = _FakeClient({"can_view"})
    service = _service(client)
    checks = [
        AuthorizationCheck(
            PrincipalRef(PrincipalType.USER, "user-1"),
            Action.VIEW,
            ResourceRef(ResourceType.WORKFLOW, "wf-1", "org-1"),
            _context(),
            ConsistencyPreference.HIGHER_CONSISTENCY,
        ),
        AuthorizationCheck(
            PrincipalRef(PrincipalType.USER, "user-1"),
            Action.VIEW,
            ResourceRef(ResourceType.WORKFLOW, "wf-2", "org-2"),
            _context(),
        ),
    ]
    decisions = await service.batch_check(checks)
    assert [item.allowed for item in decisions] == [True, False]
    assert decisions[1].reason_code == "organization_mismatch"
    assert client.batch_calls[0][1] is ConsistencyPreference.HIGHER_CONSISTENCY


@pytest.mark.asyncio
async def test_batch_check_calls_revocation_guard_once_for_whole_page():
    client = _FakeClient({"can_view"})
    service = _service(client)

    class Guard(_NoGuard):
        calls = 0

        async def denies_many(self, *, checks):
            self.calls += 1
            assert len(checks) == 50
            return tuple(False for _ in checks)

    guard = Guard()
    service._revocation_guard = guard
    checks = [
        AuthorizationCheck(
            PrincipalRef(PrincipalType.USER, "user-1"),
            Action.VIEW,
            ResourceRef(
                ResourceType.WORKFLOW,
                f"wf-{index}",
                "org-1",
            ),
            _context(),
        )
        for index in range(50)
    ]

    decisions = await service.batch_check(checks)

    assert all(item.allowed for item in decisions)
    assert guard.calls == 1
    assert len(client.batch_calls) == 1


@pytest.mark.asyncio
async def test_list_objects_filters_pending_revocation_guards():
    client = _FakeClient(listed=("wf-visible", "wf-revoked"))
    service = _service(client)

    class Guard(_NoGuard):
        async def denied_resource_ids(self, **kwargs):
            assert kwargs["resource_ids"] == ("wf-visible", "wf-revoked")
            assert kwargs["action"] is Action.VIEW_METADATA
            return frozenset({"wf-revoked"})

    service._revocation_guard = Guard()
    listed = await service.list_authorized_ids(
        PrincipalRef(PrincipalType.USER, "user-1"),
        Action.VIEW_METADATA,
        ResourceType.WORKFLOW,
        _context(),
    )

    assert listed == ("wf-visible",)


@pytest.mark.asyncio
async def test_grant_uses_only_durable_mutation_coordinator():
    client = _FakeClient({
        "can_view_metadata",
        "can_view",
        "can_export",
        "can_update",
        "can_delete",
        "can_manage_access",
        "can_use",
        "can_execute",
        "can_cancel",
        "can_inspect_runs",
        "can_deploy",
        "can_mount",
    })
    coordinator = _FakeCoordinator()
    service = _service(client, coordinator)

    async def allow_known_subject(_binding):
        return None

    service._validate_binding_subject = allow_known_subject
    binding = RelationshipBinding(
        subject=RelationshipSubject(
            RelationshipSubjectType.GROUP,
            "group-1",
            "member",
        ),
        relation="viewer",
        resource=ResourceRef(ResourceType.WORKFLOW, "wf-1", "org-1"),
    )
    result = await service.grant(
        PrincipalRef(PrincipalType.USER, "manager-1"),
        binding,
        _context(),
        idempotency_key="share-request-1",
    )
    assert result == binding
    assert coordinator.calls == [{
        "actor": PrincipalRef(PrincipalType.USER, "manager-1"),
        "binding": binding,
        "desired_present": True,
        "idempotency_key": "share-request-1",
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["custom", "catalog", None])
async def test_skill_grant_requires_custom_source_after_authorization(source):
    from types import SimpleNamespace
    client = _FakeClient({"can_manage_access", "can_view_metadata", "can_view"})
    coordinator = _FakeCoordinator()
    service = _service(client, coordinator)

    class Session:
        async def execute(self, statement):
            return SimpleNamespace(scalar_one_or_none=lambda: source)

    service._session = Session()

    async def known_subject(binding):
        return None

    service._validate_binding_subject = known_subject
    binding = RelationshipBinding(
        subject=RelationshipSubject(RelationshipSubjectType.USER, "recipient"),
        relation="viewer",
        resource=ResourceRef(ResourceType.SKILL_INSTALLATION, "00000000-0000-0000-0000-000000000001", "org-1"),
    )
    actor = PrincipalRef(PrincipalType.USER, "manager-1")
    if source == "custom":
        assert await service.grant(actor, binding, _context(), idempotency_key="custom-grant") == binding
        assert len(coordinator.calls) == 1
    else:
        with pytest.raises(ValueError, match="only custom Skills"):
            await service.grant(actor, binding, _context(), idempotency_key="catalog-grant")
        assert coordinator.calls == []
