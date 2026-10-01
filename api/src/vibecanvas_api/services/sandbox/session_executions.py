"""History-backed workflow execution groups owned by one sandbox session.

Batch and CLI callers reserve their logical workers before calling this layer.
Each group caches one graph; each row gets a private RPC slot and history ID.
Closing a group fences late dispatches and confirms process termination before
returning, without interrupting another group in the same session.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from vibecanvas_api.services.deployment_completion import complete_before_cancelling
from vibecanvas_api.services.workflow_artifacts import persist_workflow_artifacts
from vibecanvas_api.storage.db import short_session_scope
from vibecanvas_api.storage.workflow_history_repo import TERMINAL_STATUSES, WorkflowHistoryRepo

from .workflow_execution_driver import WorkflowExecutionDriver
from .workflow_guard import classify_workflow
from .workflow_rpc_pool import WorkflowRpcPool


@dataclass
class ExecutionGroup:
    pool: WorkflowRpcPool
    tasks: dict[str, asyncio.Task] = field(default_factory=dict)


class SessionExecutions:
    def __init__(self, session):
        self.session = session
        self.groups: dict[str, ExecutionGroup] = {}
        self.closed: set[str] = set()
        self.shutting_down = False

    @complete_before_cancelling
    async def run(
        self,
        *,
        group_id: str,
        execution_id: str,
        workflow: dict,
        inputs: dict,
        context: dict,
        capacity: int,
        node_id: str | None = None,
    ):
        if not group_id or len(group_id) != 32 or any(c not in "0123456789abcdef" for c in group_id):
            raise ValueError("invalid execution group ID")
        if type(capacity) is not int or not 1 <= capacity <= 16:
            raise ValueError("invalid execution capacity")
        classify_workflow(workflow)
        async with short_session_scope(tenant_id=self.session.tenant_id) as db:
            history = WorkflowHistoryRepo(db)
            detail = await history.detail(execution_id)
            if (
                detail is None
                or detail["initiator_user_id"] != str(self.session.user_id)
                or detail["workflow"] != workflow
                or detail["inputs"] != inputs
                or detail.get("node_id") != node_id
            ):
                raise PermissionError("execution_history_scope_mismatch")
            if detail["status"] in TERMINAL_STATUSES or detail["generation"] is not None:
                raise RuntimeError("execution_already_dispatched")
            await history.claim_dispatch(execution_id)
            if self.shutting_down or group_id in self.closed or detail["cancel_requested_at"] is not None:
                await history.request_cancel(execution_id)
                await history.confirm_cancelled(execution_id)
                result = (await history.result_detail(execution_id))["result"]
                return {"status": {"status": "cancelled"}, "result": result}
        # No await between group creation/reservation and task registration, so
        # close() cannot miss a concurrent dispatch that passed the fence above.
        if self.shutting_down or group_id in self.closed:
            async with short_session_scope(tenant_id=self.session.tenant_id) as db:
                history = WorkflowHistoryRepo(db)
                await history.request_cancel(execution_id)
                await history.confirm_cancelled(execution_id)
                result = (await history.result_detail(execution_id))["result"]
            return {"status": {"status": "cancelled"}, "result": result}
        group = self.groups.get(group_id)
        if group is None:
            group = ExecutionGroup(
                WorkflowRpcPool.for_session(
                    session=self.session,
                    revision=group_id,
                    workflow=workflow,
                    capacity=capacity,
                    node_id=node_id,
                )
            )
            self.groups[group_id] = group
        if group.pool.workflow != workflow or group.pool.capacity != capacity or group.pool.node_id != node_id:
            raise RuntimeError("execution_group_contract_changed")
        if execution_id in group.tasks:
            raise RuntimeError("execution_already_dispatched")
        task = asyncio.create_task(self._execute(group, execution_id, inputs, context, detail["wf_id"]))
        group.tasks[execution_id] = task
        try:
            return await asyncio.shield(task)
        finally:
            group.tasks.pop(execution_id, None)

    async def _execute(self, group, execution_id, inputs, context, wf_id):
        try:
            async with group.pool.acquire(execution_id) as slot:

                async def artifacts():
                    await persist_workflow_artifacts(
                        root=str(slot.root / "artifacts"),
                        tenant_id=self.session.tenant_id,
                        execution_id=execution_id,
                        wf_id=wf_id,
                    )
                    await self.session._sync_mount_folder()

                result = await WorkflowExecutionDriver(
                    tenant_id=self.session.tenant_id,
                    execution_id=execution_id,
                    slot=slot,
                    persist_artifacts=artifacts,
                ).run(inputs=inputs, context=context)
            async with short_session_scope(tenant_id=self.session.tenant_id) as db:
                run = await WorkflowHistoryRepo(db).get(execution_id)
            return {"status": {"status": "cancelled" if run["status"] == "cancelled" else "success"}, "result": result}
        except BaseException:
            # acquire() has stopped any unconfirmed process before releasing
            # its reservation. Finalize startup failures as well as running loss.
            async with short_session_scope(tenant_id=self.session.tenant_id) as db:
                history = WorkflowHistoryRepo(db)
                run = await history.get(execution_id)
                if run["cancel_requested_at"] is not None:
                    await history.confirm_cancelled(execution_id)
                else:
                    await history.fail(execution_id, error_code="execution_dispatch_failed")
            raise

    @complete_before_cancelling
    async def close(self, group_id: str, *, cancel: bool = True):
        self.closed.add(group_id)
        group = self.groups.get(group_id)
        if group is None:
            return
        try:
            if cancel:
                async with short_session_scope(tenant_id=self.session.tenant_id) as db:
                    history = WorkflowHistoryRepo(db)
                    for execution_id in tuple(group.tasks):
                        run = await history.get(execution_id)
                        if run["status"] not in TERMINAL_STATUSES:
                            await history.request_cancel(execution_id)
        finally:
            # Losing the history database must not prevent stopping side
            # effects or leave an approval waiter running without an observer.
            # A kill failure still retains the group/reservation for its owner.
            await group.pool.close()
            await asyncio.gather(*tuple(group.tasks.values()), return_exceptions=True)
            self.groups.pop(group_id, None)

    async def shutdown(self):
        self.shutting_down = True
        for group_id in tuple(self.groups):
            await self.close(group_id, cancel=False)
