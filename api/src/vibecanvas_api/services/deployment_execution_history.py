"""Admission-time validation and evidence shared by all deployment entrypoints."""

from __future__ import annotations

from fastapi import HTTPException
from vibecanvas_engine.utils import InputNormalizeError, normalize_inputs_for_fields, start_node_input_fields

from vibecanvas_api.services.workflow_approvers import resolve_workflow_approvers
from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo


async def create_deployment_history(
    session, *, invocation_id, deployment: dict, revision: dict, workflow: dict, inputs: dict
):
    """Caller holds admission locks and inserts the invocation in this transaction.

    No sandbox work or durable queue dispatch happens before validation. The
    assignee is pinned to an account here, not re-resolved after a queue delay.
    """
    tenant_id = str(deployment["tenant_id"])
    try:
        normalize_inputs_for_fields(inputs, start_node_input_fields(workflow))
    except InputNormalizeError as exc:
        raise HTTPException(422, "invalid_workflow_inputs") from exc
    try:
        approvers = await resolve_workflow_approvers(
            session,
            tenant_id=tenant_id,
            workflow=workflow,
            initiator_user_id=str(deployment["user_id"]),
            require_explicit=True,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await WorkflowHistoryRepo(session).create(
        execution_id=str(invocation_id),
        tenant_id=tenant_id,
        wf_id=deployment["wf_id"],
        source_type="deployment",
        source_id=str(deployment["id"]),
        initiator_user_id=str(deployment["user_id"]),
        workflow=workflow,
        inputs=inputs,
        approvers=approvers,
        revision_id=str(revision["id"]),
        workflow_version=(
            f"v{workflow['__meta__']['workflow_version']}.sv{workflow['__meta__']['workflow_subversion']}"
        ),
    )
