"""The authorization inventory must cover every external execution surface."""

from fastapi.routing import APIRoute, APIWebSocketRoute

from vibecanvas_api.app import build_app
from vibecanvas_api.authorization.manifest import (
    AdmissionKind,
    WORKER_PERMISSION_MANIFEST,
    application_route_contexts,
    platform_mcp_permission_manifest,
    route_permission_manifest,
)
from vibecanvas_api.authorization.types import Action


def test_every_api_route_has_a_complete_typed_permission_manifest():
    app = build_app()
    routes = [
        route
        for route in application_route_contexts(app)
        if isinstance(
            getattr(route, "original_route", route),
            (APIRoute, APIWebSocketRoute),
        )
    ]
    manifest = route_permission_manifest(app)
    assert len(manifest) == sum(
        len(route.methods or ())
        if isinstance(getattr(route, "original_route", route), APIRoute)
        else 1
        for route in routes
    )
    assert len(manifest) >= 200
    assert all(
        item.permission.admission is AdmissionKind.PUBLIC
        or (
            (
                item.permission.resource_type is not None
                or item.permission.resource_type_options
            )
            and item.permission.action is not None
            and item.permission.selector not in {"", "none"}
            and item.permission.parent_resolver not in {"", "none"}
        )
        for item in manifest
    )


def test_share_target_resolver_manifest_has_exact_dynamic_resource_allowlist():
    by_endpoint = {
        item.endpoint: item.permission
        for item in route_permission_manifest(build_app())
    }
    permission = by_endpoint["resolve_share_target"]
    assert permission.resource_type is None
    assert {item.value for item in permission.resource_type_options} == {
        "workflow",
        "task",
        "deployment",
        "knowledge_base",
    }
    assert permission.action is Action.MANAGE_ACCESS


def test_every_session_protected_http_route_resolves_current_user():
    from vibecanvas_api.auth.deps import current_user

    app = build_app()
    manifest = {
        (item.method, item.path, item.endpoint): item.permission
        for item in route_permission_manifest(app)
    }

    def dependency_calls(dependant) -> set[object]:
        calls: set[object] = set()
        for child in dependant.dependencies:
            calls.add(child.call)
            calls.update(dependency_calls(child))
        return calls

    for route in application_route_contexts(app):
        if not isinstance(getattr(route, "original_route", route), APIRoute):
            continue
        calls = dependency_calls(route.dependant)
        for method in route.methods or ():
            permission = manifest[(method, route.path, route.name)]
            if permission.admission in {
                AdmissionKind.SESSION,
                AdmissionKind.ORGANIZATION,
                AdmissionKind.RESOURCE,
            }:
                assert current_user in calls, (
                    f"{method} {route.path} is protected in the manifest "
                    "but does not resolve current_user"
                )


def test_worker_manifest_matches_registered_background_workflows():
    from vibecanvas_api.background_workflows import (
        SCHEDULE_WORKFLOWS,
        TASK_WORKFLOWS,
    )

    expected = {item.task_name for item in WORKER_PERMISSION_MANIFEST}
    registered = set(TASK_WORKFLOWS) | set(SCHEDULE_WORKFLOWS)
    assert expected == registered
    assert all(item.parent_resolver for item in WORKER_PERMISSION_MANIFEST)


def test_platform_mcp_manifest_has_one_policy_per_exported_tool():
    manifest = platform_mcp_permission_manifest()
    keys = {(item.server, item.tool_name) for item in manifest}
    assert len(keys) == len(manifest)
    assert {server for server, _ in keys} == {
        "build",
        "config",
        "deployment",
        "interactive",
        "knowledge",
        "task",
        "workflow",
    }
    assert all(item.parent_resolver for item in manifest)
    actions = {
        (item.server, item.tool_name): item.action
        for item in manifest
    }
    assert actions[("workflow", "list_workflows")] is Action.VIEW_METADATA
    assert actions[("workflow", "get_workflow")] is Action.VIEW
    assert actions[("task", "task_cancel")] is Action.CANCEL
    assert actions[("task", "task_resume")] is Action.RESUME
    assert actions[("task", "task_collect_diagnostics")] is Action.INSPECT_RUNS
    assert actions[("deployment", "deployment_create")] is Action.DEPLOY
    assert actions[("deployment", "deployment_delete")] is Action.DELETE
    assert (
        actions[("deployment", "deployment_collect_diagnostics")]
        is Action.INSPECT_RUNS
    )
    assert (
        actions[("knowledge", "knowledge_list")]
        is Action.VIEW_METADATA
    )
    assert actions[("knowledge", "knowledge_get")] is Action.USE
    assert actions[("knowledge", "knowledge_create")] is Action.CREATE
    assert actions[("knowledge", "knowledge_update")] is Action.UPDATE
    assert actions[("knowledge", "knowledge_delete")] is Action.DELETE
    assert actions[("knowledge", "knowledge_search")] is Action.USE
    assert actions[("build", "run_workflow")] is Action.EXECUTE
    assert all(server != "browser" for server, _tool in actions)


def test_subresource_manifest_matches_runtime_authorization_semantics():
    """Generic naming inference must not weaken sensitive child surfaces."""
    by_endpoint = {
        item.endpoint: item.permission
        for item in route_permission_manifest(build_app())
    }
    expected_actions = {
        "get_workflow_sandbox_status": Action.INSPECT_RUNS,
        "list_versions": Action.VIEW,
        "get_execution_status": Action.INSPECT_RUNS,
        "list_executions": Action.INSPECT_RUNS,
        "get_agent_run_by_client_request": Action.INSPECT_RUNS,
        "decide_hitl_request": Action.RESUME,
        "list_scheduled_run_executions": Action.INSPECT_RUNS,
        "get_scheduled_run_execution_events": Action.INSPECT_RUNS,
        "list_task_events": Action.INSPECT_RUNS,
        "stream_task_events": Action.INSPECT_RUNS,
        "get_credential": Action.MANAGE_SECRET,
        "refresh_mcp_server": Action.MANAGE_SECRET,
        "list_organization_members": Action.VIEW_AUDIT,
        "update_organization_member": Action.MANAGE_MEMBERS,
        "create_preview_resource_session": Action.VIEW,
    }
    assert {
        endpoint: by_endpoint[endpoint].action
        for endpoint in expected_actions
    } == expected_actions

    # These endpoints authorize their parent directly and scope child lookup by
    # that parent. The inventory must not claim an id absent from the request.
    assert by_endpoint["list_versions"].resource_type is not None
    assert by_endpoint["list_versions"].resource_type.value == "workflow"
    assert by_endpoint["list_scheduled_run_executions"].resource_type is not None
    assert (
        by_endpoint["list_scheduled_run_executions"].resource_type.value
        == "task"
    )
    assert by_endpoint["history"].resource_type is not None
    assert by_endpoint["history"].resource_type.value == "deployment"
