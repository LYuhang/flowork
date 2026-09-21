"""Complete HTTP, worker, and Platform MCP authorization inventory.

This module is deliberately executable instead of a hand-maintained prose
table. Every FastAPI route must compile to one typed policy at application
construction time. Unknown route families fail startup and CI. The manifest is
an inventory in Stage 0; Stage 3 progressively makes each resource policy the
runtime dependency used by the corresponding route.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable

from fastapi import FastAPI
from fastapi.routing import APIRoute, APIWebSocketRoute

try:
    from fastapi.routing import iter_route_contexts
except ImportError:  # pragma: no cover - compatibility with older dev envs
    iter_route_contexts = None

from .types import Action, ResourceType


class AdmissionKind(StrEnum):
    PUBLIC = "public"
    SESSION = "session"
    ORGANIZATION = "organization"
    RESOURCE = "resource"
    SIGNED_CAPABILITY = "signed_capability"
    EXTERNAL_CREDENTIAL = "external_credential"


@dataclass(frozen=True, slots=True)
class PermissionSpec:
    admission: AdmissionKind
    resource_type: ResourceType | None
    action: Action | None
    selector: str
    parent_resolver: str
    resource_type_options: frozenset[ResourceType] = frozenset()


@dataclass(frozen=True, slots=True)
class RoutePermission:
    method: str
    path: str
    endpoint: str
    permission: PermissionSpec


@dataclass(frozen=True, slots=True)
class WorkerPermission:
    task_name: str
    principal: str
    resource_type: ResourceType
    action: Action
    parent_resolver: str


@dataclass(frozen=True, slots=True)
class McpToolPermission:
    server: str
    tool_name: str
    resource_type: ResourceType
    action: Action
    parent_resolver: str


_PUBLIC_ENDPOINTS = frozenset({
    "healthz",
    "metrics_endpoint",
    "version",
    "public_config",
    "register",
    "login",
    "password_reset_request",
    "password_reset_confirm",
    "cancel_delete_account",
    "logout",
    "mcp_oauth_client_metadata",
    # Optional ambient support-cookie probe. Without a valid support Session
    # it returns only {active:false} and reveals no identity or tenant data.
    "privileged_access_cookie_status",
})

_SIGNED_CAPABILITY_ENDPOINTS = frozenset({
    "gateway_vfs_resource",
    "raw_vfs",
    "mcp_oauth_callback",
    "runtime_mcp_request",
    "runtime_model_request",
    "ws_hub",
})

_EXTERNAL_CREDENTIAL_ENDPOINTS = frozenset({
    "invoke_sync",
    "invoke_async",
    "webhook",
})

_OIDC_PUBLIC_ENDPOINTS = frozenset({
    "discover_organization_sso",
    "start_sso_login",
})

_OIDC_CREDENTIAL_ENDPOINTS = frozenset({"complete_sso_login"})

_SCIM_ENDPOINTS = frozenset({
    "service_provider_config",
    "resource_types",
    "schemas",
    "create_user",
    "get_user",
    "list_users",
    "replace_user",
    "patch_user",
    "delete_user",
    "create_group",
    "get_group",
    "list_groups",
    "replace_group",
    "patch_group",
    "delete_group",
})

_EXPLICIT_ACTIONS = {
    # These sub-surfaces deliberately require a permission different from the
    # generic HTTP verb / endpoint-name inference. They mirror the action used
    # by the route's runtime AuthzService check.
    "get_workflow_workspace_identity": Action.MOUNT,
    "get_workflow_sandbox_statuses": Action.INSPECT_RUNS,
    "get_workflow_sandbox_status": Action.INSPECT_RUNS,
    "start_workflow_sandbox": Action.MOUNT,
    "list_versions": Action.VIEW,
    "get_execution_status": Action.INSPECT_RUNS,
    "get_workflow_execution_status": Action.INSPECT_RUNS,
    "list_executions": Action.INSPECT_RUNS,
    "get_chat_sandbox_status": Action.INSPECT_RUNS,
    "get_chat_sandbox_statuses": Action.INSPECT_RUNS,
    "list_chat_hitl_requests": Action.VIEW,
    "decide_hitl_request": Action.RESUME,
    "create_interactive_resource_session": Action.VIEW,
    "list_chat_background_jobs": Action.VIEW,
    "get_agent_run_by_client_request": Action.INSPECT_RUNS,
    "tasks_summary": Action.VIEW_METADATA,
    "list_scheduled_run_executions": Action.INSPECT_RUNS,
    "get_scheduled_run_execution": Action.INSPECT_RUNS,
    "get_scheduled_run_execution_events": Action.INSPECT_RUNS,
    "list_task_events": Action.INSPECT_RUNS,
    "stream_task_events": Action.INSPECT_RUNS,
    "get_credential": Action.MANAGE_SECRET,
    "refresh_mcp_server": Action.MANAGE_SECRET,
    "list_organization_members": Action.VIEW_AUDIT,
    "update_organization_member": Action.MANAGE_MEMBERS,
    "create_group": Action.MANAGE_MEMBERS,
    "set_group_member": Action.MANAGE_MEMBERS,
    "revoke_group_member": Action.MANAGE_MEMBERS,
    "create_preview_resource_session": Action.VIEW,
    "list_platform_eligibilities": Action.MANAGE_POLICY,
    "grant_platform_eligibility": Action.MANAGE_POLICY,
    "review_platform_eligibility": Action.MANAGE_POLICY,
    "revoke_platform_eligibility": Action.MANAGE_POLICY,
}


def _action(method: str, path: str, endpoint: str) -> Action:
    value = f"{path}/{endpoint}".lower()
    endpoint = endpoint.lower()
    explicit = _EXPLICIT_ACTIONS.get(endpoint)
    if explicit is not None:
        return explicit
    if endpoint == "list_service_accounts":
        return Action.VIEW_AUDIT
    if endpoint in {
        "update_service_account_status",
        "rotate_service_account_generation",
    }:
        return Action.MANAGE_POLICY
    if (
        "manage-access" in value
        or "/access" in path.lower()
        or "permission" in value
        or "share" in value
    ):
        return Action.MANAGE_ACCESS
    if (
        "rotate-key" in value
        or "oauth" in value
        or endpoint in {
            "create_credential",
            "update_credential",
        }
    ):
        return Action.MANAGE_SECRET
    if "cancel" in value or "close_" in endpoint:
        return Action.CANCEL
    if "resume" in value or "continue" in value:
        return Action.RESUME
    if (
        "execute" in value
        or "execution" in value and method == "POST"
        or "invoke" in value
        or "run-now" in value
        or endpoint.startswith(("run_", "start_execution", "submit_batch"))
    ):
        return Action.EXECUTE
    if "deploy" in value and method == "POST":
        return Action.DEPLOY
    if "publish" in value:
        return Action.PUBLISH
    if "download" in value or "export" in value:
        return Action.EXPORT
    if method == "DELETE" or endpoint.startswith(("delete_", "archive_")):
        return Action.DELETE
    if endpoint.startswith(("list_", "discover_", "catalog", "enums")):
        return Action.VIEW_METADATA
    if endpoint.startswith(
        (
            "get_",
            "read_",
            "resolve_",
            "check_",
            "stream_",
            "sign_",
        )
    ):
        return Action.VIEW
    if method in {"PUT", "PATCH"}:
        return Action.UPDATE
    if method == "POST":
        if endpoint.startswith(
            (
                "create_",
                "install_",
                "upload_",
                "mkdir_",
                "bootstrap_",
            )
        ):
            return Action.CREATE
        return Action.UPDATE
    if any(
        token in value
        for token in ("/events", "/history", "/metrics", "/runs", "inspect")
    ):
        return Action.INSPECT_RUNS
    return Action.VIEW


def _spec(
    *,
    admission: AdmissionKind,
    resource_type: ResourceType | None,
    action: Action | None,
    selector: str,
    parent_resolver: str,
    resource_type_options: frozenset[ResourceType] = frozenset(),
) -> PermissionSpec:
    return PermissionSpec(
        admission=admission,
        resource_type=resource_type,
        action=action,
        selector=selector,
        parent_resolver=parent_resolver,
        resource_type_options=resource_type_options,
    )


def permission_for_route(
    *,
    method: str,
    path: str,
    endpoint: str,
) -> PermissionSpec | None:
    """Compile one concrete API route to its authorization contract."""
    if endpoint == "exchange_extension_session":
        return _spec(
            admission=AdmissionKind.EXTERNAL_CREDENTIAL,
            resource_type=ResourceType.IDENTITY,
            action=Action.USE,
            selector="body:single_use_exchange_code",
            parent_resolver="session_exchange_code",
        )
    if endpoint in _OIDC_CREDENTIAL_ENDPOINTS:
        return _spec(
            admission=AdmissionKind.EXTERNAL_CREDENTIAL,
            resource_type=ResourceType.IDENTITY,
            action=Action.USE,
            selector="query:single_use_state+authorization_code+nonce",
            parent_resolver="oidc_login_transaction",
        )
    if endpoint in _SCIM_ENDPOINTS and path.startswith("/scim/v2/"):
        return _spec(
            admission=AdmissionKind.EXTERNAL_CREDENTIAL,
            resource_type=ResourceType.ORGANIZATION,
            action=Action.MANAGE_MEMBERS,
            selector="path:provider_id+header:bearer_token",
            parent_resolver="enterprise_identity_provider",
        )
    if endpoint in _OIDC_PUBLIC_ENDPOINTS:
        return _spec(
            admission=AdmissionKind.PUBLIC,
            resource_type=None,
            action=None,
            selector="none",
            parent_resolver="none",
        )
    if endpoint in _PUBLIC_ENDPOINTS:
        return _spec(
            admission=AdmissionKind.PUBLIC,
            resource_type=None,
            action=None,
            selector="none",
            parent_resolver="none",
        )
    if endpoint in _SIGNED_CAPABILITY_ENDPOINTS:
        if endpoint == "mcp_oauth_callback":
            resource_type = ResourceType.MCP_OAUTH_CONNECTION
            action = Action.MANAGE_SECRET
            selector = "signed:oauth_state+expiry"
        elif endpoint in {"runtime_model_request", "runtime_mcp_request"}:
            resource_type = ResourceType.RUNTIME_STATE
            action = Action.EXECUTE
            runtime_scope = (
                "session_generation+chat+turn+"
                if endpoint == "runtime_mcp_request"
                else "chat_session_or_workflow_execution+"
            )
            selector = (
                "signed:audience+organization+"
                + runtime_scope
                + (
                    "mcp_installation+transport+config_revision+"
                    if endpoint == "runtime_mcp_request"
                    else "credential+"
                )
                + "resource+action+authz_generation+expiry"
            )
        elif endpoint == "ws_hub":
            resource_type = ResourceType.BROWSER_BINDING
            action = Action.USE
            selector = "signed:user+organization+workspace+expiry"
        else:
            resource_type = ResourceType.VFS_PATH
            action = Action.VIEW
            selector = "signed:audience+scope+path+operation+expiry"
        return _spec(
            admission=AdmissionKind.SIGNED_CAPABILITY,
            resource_type=resource_type,
            action=action,
            selector=selector,
            parent_resolver=(
                "runtime_mcp_to_chat_and_installation"
                if endpoint == "runtime_mcp_request"
                else "runtime_model_to_chat_or_workflow_execution_and_credential"
                if endpoint == "runtime_model_request"
                else "capability_payload"
            ),
        )
    if endpoint in _EXTERNAL_CREDENTIAL_ENDPOINTS:
        return _spec(
            admission=AdmissionKind.EXTERNAL_CREDENTIAL,
            resource_type=ResourceType.DEPLOYMENT,
            action=Action.EXECUTE,
            selector="path:slug",
            parent_resolver="deployment_slug",
        )

    action = _action(method, path, endpoint)

    if path.startswith("/api/v1/platform-management"):
        return _spec(
            admission=AdmissionKind.SESSION,
            resource_type=ResourceType.PLATFORM_CATALOG,
            action=Action.VIEW_METADATA,
            selector="platform:operations_control_plane",
            parent_resolver="platform_admin_eligibility",
        )
    if path.startswith("/api/v1/auth") or path in {
        "/api/v1/me",
        "/api/v1/enums",
    }:
        return _spec(
            admission=AdmissionKind.SESSION,
            resource_type=ResourceType.IDENTITY,
            action=action,
            selector="session:user_id",
            parent_resolver="identity_self",
        )
    if path.startswith("/api/v1/organizations"):
        if "/groups/" in path or path.endswith("/groups"):
            resource_type = ResourceType.GROUP
            selector = (
                "path:group_id"
                if "{group_id}" in path
                else "path:organization_id"
            )
            parent = "group_to_organization"
        else:
            resource_type = ResourceType.ORGANIZATION
            selector = (
                "path:organization_id"
                if "{organization_id}" in path
                else "session:active_organization_id"
            )
            parent = "organization_membership"
        return _spec(
            admission=AdmissionKind.ORGANIZATION,
            resource_type=resource_type,
            action=action,
            selector=selector,
            parent_resolver=parent,
        )
    if endpoint == "list_shared_resources":
        return _spec(
            admission=AdmissionKind.SESSION,
            resource_type=ResourceType.IDENTITY,
            action=Action.VIEW_METADATA,
            selector="session:user_id",
            parent_resolver="recipient_projection_to_authoritative_resource",
        )
    if path.startswith("/api/v1/resource-access"):
        return _spec(
            admission=AdmissionKind.RESOURCE,
            resource_type=None,
            action=Action.MANAGE_ACCESS,
            selector="path:resource_type+resource_id",
            parent_resolver="shareable_resource_direct",
            resource_type_options=frozenset({
                ResourceType.WORKFLOW,
                ResourceType.TASK,
                ResourceType.DEPLOYMENT,
                ResourceType.KNOWLEDGE_BASE,
            }),
        )
    if path.startswith("/api/v1/workflows"):
        return _spec(
            admission=AdmissionKind.RESOURCE,
            resource_type=ResourceType.WORKFLOW,
            action=action,
            selector=(
                "path:wf_id" if "{wf_id}" in path
                else "session:active_organization_id"
            ),
            parent_resolver="direct_or_collection",
        )
    if path.startswith("/api/v1/executions"):
        return _spec(
            admission=AdmissionKind.RESOURCE,
            resource_type=ResourceType.WORKFLOW_EXECUTION,
            action=action,
            selector="path:exec_id",
            parent_resolver="execution_to_workflow",
        )
    if path.startswith("/api/v1/tasks"):
        if endpoint == "create_scheduled_run" and method == "POST":
            return _spec(
                admission=AdmissionKind.ORGANIZATION,
                resource_type=ResourceType.ORGANIZATION,
                action=Action.CREATE,
                selector="session:active_organization_id",
                parent_resolver="organization_membership",
            )
        return _spec(
            admission=AdmissionKind.RESOURCE,
            resource_type=ResourceType.TASK,
            action=action,
            selector=(
                "path:task_id"
                if "{task_id}" in path
                else "session:active_organization_id"
            ),
            parent_resolver="direct_or_collection",
        )
    if path.startswith("/api/v1/deployments"):
        if endpoint == "create_deployment" and method == "POST":
            return _spec(
                admission=AdmissionKind.RESOURCE,
                resource_type=ResourceType.WORKFLOW,
                action=Action.DEPLOY,
                selector="body:wf_id",
                parent_resolver="direct",
            )
        return _spec(
            admission=AdmissionKind.RESOURCE,
            resource_type=ResourceType.DEPLOYMENT,
            action=action,
            selector=(
                "path:dep_id"
                if "{dep_id}" in path
                else "session:active_organization_id"
            ),
            parent_resolver="direct_or_collection",
        )
    if path == "/api/v1/kb" or path.startswith("/api/v1/kb/"):
        if endpoint == "create_kb" and method == "POST":
            return _spec(
                admission=AdmissionKind.ORGANIZATION,
                resource_type=ResourceType.ORGANIZATION,
                action=Action.CREATE,
                selector="session:active_organization_id",
                parent_resolver="organization_membership",
            )
        if endpoint == "search":
            return _spec(
                admission=AdmissionKind.RESOURCE,
                resource_type=ResourceType.KNOWLEDGE_BASE,
                action=Action.USE,
                selector="body:kb_ids",
                parent_resolver="direct",
            )
        if endpoint == "upload_file":
            action = Action.UPDATE
        elif endpoint == "list_files":
            action = Action.VIEW
        child = "/files/" in path
        return _spec(
            admission=AdmissionKind.RESOURCE,
            resource_type=(
                ResourceType.KNOWLEDGE_BASE_FILE
                if child
                else ResourceType.KNOWLEDGE_BASE
            ),
            action=action,
            selector=(
                "path:file_id"
                if "{file_id}" in path
                else "path:kb_id"
                if "{kb_id}" in path
                else "body:kb_id_or_collection"
            ),
            parent_resolver=(
                "kb_file_to_knowledge_base" if child else "direct_or_collection"
            ),
        )
    if endpoint == "install_catalog_skill" and method == "POST":
        return _spec(
            admission=AdmissionKind.ORGANIZATION,
            resource_type=ResourceType.ORGANIZATION,
            action=Action.CREATE,
            selector="session:active_organization_id",
            parent_resolver="organization_membership",
        )
    if path.startswith("/api/v1/skills/catalog"):
        return _spec(
            admission=AdmissionKind.SESSION,
            resource_type=ResourceType.PLATFORM_CATALOG,
            action=action,
            selector="platform:skill_catalog",
            parent_resolver="platform_policy",
        )
    if path.startswith("/api/v1/skills"):
        if endpoint == "create_custom_skill" and method == "POST":
            return _spec(
                admission=AdmissionKind.ORGANIZATION,
                resource_type=ResourceType.ORGANIZATION,
                action=Action.CREATE,
                selector="session:active_organization_id",
                parent_resolver="organization_membership",
            )
        if endpoint in {
            "get_custom_skill_draft",
            "list_skill_versions",
        }:
            action = Action.VIEW
        elif endpoint == "publish_custom_skill_version":
            action = Action.PUBLISH
        child = "{revision_id}" in path
        return _spec(
            admission=AdmissionKind.RESOURCE,
            resource_type=(
                ResourceType.SKILL_REVISION
                if child
                else ResourceType.SKILL_INSTALLATION
            ),
            action=action,
            selector=(
                "path:revision_id"
                if "{revision_id}" in path
                else "path:skill_id"
                if "{skill_id}" in path
                else "session:user_id"
            ),
            parent_resolver=(
                "skill_revision_to_installation"
                if child
                else "direct_or_collection"
            ),
        )
    if path.startswith("/api/v1/mcp-servers/catalog"):
        return _spec(
            admission=AdmissionKind.SESSION,
            resource_type=ResourceType.PLATFORM_CATALOG,
            action=action,
            selector="platform:mcp_catalog",
            parent_resolver="platform_policy",
        )
    if path.startswith("/api/v1/mcp-servers"):
        child = "/oauth/" in path
        return _spec(
            admission=AdmissionKind.RESOURCE,
            resource_type=(
                ResourceType.MCP_OAUTH_CONNECTION
                if child
                else ResourceType.MCP_INSTALLATION
            ),
            action=action,
            selector=(
                "path:server_id"
                if "{server_id}" in path
                else "session:user_id"
            ),
            parent_resolver=(
                "mcp_oauth_to_installation"
                if child
                else "installation_direct_or_collection"
            ),
        )
    if path.startswith("/api/v1/llm-credentials"):
        return _spec(
            admission=AdmissionKind.RESOURCE,
            resource_type=ResourceType.LLM_CREDENTIAL,
            action=action,
            selector=(
                "path:credential_id"
                if "{credential_id}" in path
                else "session:user_id"
            ),
            parent_resolver="credential_direct_or_collection",
        )
    if path.startswith("/api/v1/storage"):
        if endpoint in {
            "list_storage",
            "read_storage_content",
            "raw_storage_content",
        }:
            action = Action.VIEW
        else:
            # File/directory mutations inherit UPDATE on their logical root;
            # DELETE applies only to deleting the root itself.
            action = Action.UPDATE
        return _spec(
            admission=AdmissionKind.RESOURCE,
            resource_type=ResourceType.VFS_PATH,
            action=action,
            selector="query_or_body:logical_path",
            parent_resolver=(
                "storage_logical_path_to_storage_chat_or_workflow"
            ),
        )
    if path.startswith("/api/v1/vfs"):
        if endpoint == "list_vfs":
            action = Action.VIEW
        elif endpoint in {"upload_file", "delete_vfs"}:
            action = Action.UPDATE
        return _spec(
            admission=AdmissionKind.RESOURCE,
            resource_type=(
                ResourceType.VFS_RUN
                if "{run_id}" in path
                else ResourceType.VFS_PATH
            ),
            action=action,
            selector="path_or_query:run_id+wf_id+path",
            parent_resolver="vfs_scope_to_chat_workflow_or_storage",
        )
    if path.startswith("/api/v1/previews"):
        return _spec(
            admission=AdmissionKind.RESOURCE,
            resource_type=ResourceType.VFS_PATH,
            action=action,
            selector="body_or_query:file_ref",
            parent_resolver="file_ref_to_chat_run_or_storage",
        )

    if (
        path.startswith("/api/v1/chat")
        or path.startswith("/api/v1/chats")
        or path.startswith("/api/v1/hitl-requests")
        or path.startswith("/api/v1/interactive-artifacts")
    ):
        if path.startswith("/api/v1/hitl-requests"):
            resource_type = ResourceType.HITL_REQUEST
            selector = "path:hitl_request_id"
            parent = "hitl_request_to_chat"
        elif path.startswith("/api/v1/interactive-artifacts"):
            resource_type = ResourceType.INTERACTIVE_ARTIFACT
            selector = "path:artifact_id"
            parent = "interactive_artifact_to_chat"
        elif "/background-jobs/{job_id}" in path:
            resource_type = ResourceType.BACKGROUND_JOB
            selector = "path:job_id"
            parent = "background_job_to_chat"
        elif "/turns/" in path or "active-turn" in path:
            resource_type = ResourceType.AGENT_RUN
            selector = "path:turn_id_or_client_request_id"
            parent = "agent_run_to_chat"
        else:
            resource_type = ResourceType.CHAT
            selector = (
                "path:chat_id"
                if "{chat_id}" in path
                else "session:user_authorized_chat_collection"
            )
            parent = "direct_or_collection"
        return _spec(
            admission=AdmissionKind.RESOURCE,
            resource_type=resource_type,
            action=action,
            selector=selector,
            parent_resolver=parent,
        )

    if path.startswith("/api/v1/browser"):
        return _spec(
            admission=AdmissionKind.RESOURCE,
            resource_type=ResourceType.BROWSER_BINDING,
            action=action,
            selector="session:user_id_or_body:chat_id",
            parent_resolver="browser_binding_to_chat_or_identity",
        )
    if path.startswith("/api/v1/agent-runtime"):
        return _spec(
            admission=AdmissionKind.SESSION,
            resource_type=ResourceType.RUNTIME_STATE,
            action=action,
            selector="session:user_id",
            parent_resolver="runtime_setting_to_identity",
        )
    if path.startswith("/api/v1/envs"):
        return _spec(
            admission=AdmissionKind.RESOURCE,
            resource_type=ResourceType.RUNTIME_STATE,
            action=action,
            selector="session:active_organization_id+overlay_key",
            parent_resolver="environment_to_owning_resource",
        )
    if path.startswith("/api/v1/audit"):
        return _spec(
            admission=AdmissionKind.ORGANIZATION,
            resource_type=ResourceType.ORGANIZATION,
            action=Action.VIEW_AUDIT,
            selector="session:active_organization_id",
            parent_resolver="organization_membership",
        )
    return None


def application_route_contexts(app: FastAPI) -> tuple[Any, ...]:
    """Return concrete routes across eager and lazy FastAPI router trees.

    FastAPI 0.141+ keeps included routers as lazy branches. Its route contexts
    carry the effective prefix, dependencies, methods, and endpoint; scanning
    only ``app.routes`` would silently omit those protected routes.
    """
    routes: Iterable[Any]
    if iter_route_contexts is None:
        routes = app.routes
    else:
        routes = iter_route_contexts(app.routes)
    return tuple(routes)


def _effective_route_attribute(
    route: Any,
    original_route: Any,
    name: str,
) -> Any:
    """Read a resolved route field, including lazy WebSocket contexts."""
    value = getattr(route, name, None)
    if value not in (None, ""):
        return value
    route_context = getattr(route, "_route_context", None)
    starlette_route = getattr(route_context, "starlette_route", None)
    value = getattr(starlette_route, name, None)
    if value not in (None, ""):
        return value
    return getattr(original_route, name, None)


def route_permission_manifest(app: FastAPI) -> tuple[RoutePermission, ...]:
    manifest: list[RoutePermission] = []
    for route in application_route_contexts(app):
        original_route = getattr(route, "original_route", route)
        if not isinstance(original_route, (APIRoute, APIWebSocketRoute)):
            continue
        path = _effective_route_attribute(route, original_route, "path")
        endpoint = _effective_route_attribute(route, original_route, "name")
        methods = (
            sorted(route.methods or ())
            if isinstance(original_route, APIRoute)
            else ["WEBSOCKET"]
        )
        for method in methods:
            permission = permission_for_route(
                method=method,
                path=path,
                endpoint=endpoint,
            )
            if permission is None:
                raise RuntimeError(
                    "route permission manifest missing: "
                    f"{method} {path} ({endpoint})"
                )
            if permission.admission is not AdmissionKind.PUBLIC and (
                (
                    permission.resource_type is None
                    and not permission.resource_type_options
                )
                or permission.action is None
                or permission.selector in {"", "none"}
                or permission.parent_resolver in {"", "none"}
            ):
                raise RuntimeError(
                    "protected route has incomplete permission manifest: "
                    f"{method} {path} ({endpoint})"
                )
            manifest.append(RoutePermission(
                method=method,
                path=path,
                endpoint=endpoint,
                permission=permission,
            ))
    return tuple(manifest)


WORKER_PERMISSION_MANIFEST = (
    WorkerPermission(
        "authorization.apply_mutation", "platform_worker",
        ResourceType.ORGANIZATION, Action.MANAGE_POLICY,
        "authorization_projection_inventory",
    ),
    WorkerPermission(
        "authorization.audit", "platform_worker",
        ResourceType.ORGANIZATION, Action.MANAGE_POLICY,
        "authorization_projection_inventory",
    ),
    WorkerPermission(
        "batch_exec", "captured_user", ResourceType.WORKFLOW,
        Action.EXECUTE, "batch_task_to_workflow",
    ),
    WorkerPermission(
        "deployments.concurrency_reconciler", "platform_worker",
        ResourceType.DEPLOYMENT, Action.INSPECT_RUNS,
        "deployment_lease_inventory",
    ),
    WorkerPermission(
        "deployment_invoke", "service_account", ResourceType.DEPLOYMENT,
        Action.EXECUTE, "invocation_to_deployment",
    ),
    WorkerPermission(
        "data_purge.run_due", "platform_worker", ResourceType.ORGANIZATION,
        Action.DELETE, "purge_job_to_account_and_organization",
    ),
    WorkerPermission(
        "deployments.flush_invoke_counters", "platform_worker",
        ResourceType.DEPLOYMENT, Action.INSPECT_RUNS,
        "deployment_metric_inventory",
    ),
    WorkerPermission(
        "kb.gc_sweeper", "platform_worker", ResourceType.KNOWLEDGE_BASE,
        Action.DELETE, "expired_kb_derivative_to_knowledge_base",
    ),
    WorkerPermission(
        "kb.index_file", "captured_user", ResourceType.KNOWLEDGE_BASE_FILE,
        Action.UPDATE, "kb_file_to_knowledge_base",
    ),
    WorkerPermission(
        "kb.orphan_reconciler", "platform_worker",
        ResourceType.KNOWLEDGE_BASE_FILE, Action.DELETE,
        "kb_file_to_knowledge_base",
    ),
    WorkerPermission(
        "background.reconcile_queued", "platform_worker",
        ResourceType.TASK, Action.RESUME, "task_inventory",
    ),
    WorkerPermission(
        "scheduled_runs.dispatch_due", "service_account",
        ResourceType.TASK, Action.EXECUTE, "schedule_to_task",
    ),
    WorkerPermission(
        "scheduled_runs.execute", "service_account", ResourceType.TASK,
        Action.EXECUTE, "schedule_execution_to_task",
    ),
)


def _mcp_action(tool_name: str) -> Action:
    name = tool_name.lower()
    if name.endswith("_list") or name.startswith("list_"):
        return Action.VIEW_METADATA
    if (
        name.endswith("_get")
        or name.startswith(("get_", "check_"))
        or name == "get_config"
    ):
        return Action.VIEW
    if "cancel" in name:
        return Action.CANCEL
    if "resume" in name:
        return Action.RESUME
    if "execute" in name or name.startswith(("run_", "batch_execute")):
        return Action.EXECUTE
    if name.startswith(("task_create_", "create_workflow")):
        return Action.CREATE
    if name == "deployment_create":
        return Action.DEPLOY
    if name.startswith(("task_delete_", "deployment_delete")):
        return Action.DELETE
    return Action.UPDATE


def platform_mcp_permission_manifest() -> tuple[McpToolPermission, ...]:
    """Build the exact tool inventory from the same exported tool lists."""
    from vibecanvas_api.services.platform_mcp.authorization import (
        platform_mcp_tool_action,
        platform_resource_tool_action,
    )
    from vibecanvas_api.services.platform_mcp.build_tools import BUILD_TOOLS
    from vibecanvas_api.services.platform_mcp.build_tools.workflow_context import (
        create_workflow,
        set_workflow,
    )
    from vibecanvas_api.services.platform_mcp.config_tools import CONFIG_TOOLS
    from vibecanvas_api.services.platform_mcp.interactive_tools import (
        INTERACTIVE_TOOLS,
    )
    from vibecanvas_api.services.platform_mcp.resource_tools import (
        DEPLOYMENT_MCP_TOOLS,
        KNOWLEDGE_MCP_TOOLS,
        TASK_MCP_TOOLS,
    )
    from vibecanvas_api.services.platform_mcp.run_tools import RUN_TOOLS
    from vibecanvas_api.services.platform_mcp.workflow_tools import (
        WORKFLOW_MCP_TOOLS,
    )

    definitions = {
        "config": (
            CONFIG_TOOLS,
            ResourceType.PLATFORM_CATALOG,
            "platform_policy",
        ),
        "interactive": (
            INTERACTIVE_TOOLS,
            ResourceType.CHAT,
            "platform_capability_to_chat",
        ),
        "workflow": (
            WORKFLOW_MCP_TOOLS,
            ResourceType.WORKFLOW,
            "tool_argument_or_chat_binding_to_workflow",
        ),
        "task": (
            TASK_MCP_TOOLS,
            ResourceType.TASK,
            "tool_argument_to_task",
        ),
        "deployment": (
            DEPLOYMENT_MCP_TOOLS,
            ResourceType.DEPLOYMENT,
            "tool_argument_to_deployment",
        ),
        "knowledge": (
            KNOWLEDGE_MCP_TOOLS,
            ResourceType.KNOWLEDGE_BASE,
            "tool_argument_or_list_to_knowledge_base",
        ),
        "build": (
            [set_workflow, create_workflow, *BUILD_TOOLS, *RUN_TOOLS],
            ResourceType.WORKFLOW,
            "tool_argument_or_chat_binding_to_workflow",
        ),
    }
    return tuple(
        McpToolPermission(
            server=server,
            tool_name=str(tool.name),
            resource_type=(
                ResourceType.ORGANIZATION
                if server == "build"
                and str(tool.name) == "create_workflow"
                or server == "task"
                and str(tool.name) == "task_create_scheduled_run"
                else ResourceType.WORKFLOW
                if server == "deployment"
                and str(tool.name) == "deployment_create"
                else resource_type
            ),
            action=(
                platform_mcp_tool_action(str(tool.name))
                if server in {"workflow", "build"}
                else platform_resource_tool_action(
                    server,
                    str(tool.name),
                )
                if server in {"task", "deployment", "knowledge"}
                else _mcp_action(str(tool.name))
            ),
            parent_resolver=(
                "platform_capability_to_organization"
                if (
                    server == "build"
                    and str(tool.name) == "create_workflow"
                    or server == "task"
                    and str(tool.name) == "task_create_scheduled_run"
                )
                else "tool_argument_to_workflow"
                if server == "deployment"
                and str(tool.name) == "deployment_create"
                else parent_resolver
            ),
        )
        for server, (tools, resource_type, parent_resolver) in definitions.items()
        for tool in tools
    )
