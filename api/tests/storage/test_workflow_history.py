"""Database-level evidence for history, tenant boundaries and approval races.

These tests supplement, and never replace, deployed-service acceptance.
"""

import asyncio
import time
import uuid
import shutil
from tempfile import TemporaryDirectory

import pytest
from sqlalchemy import text

from vibecanvas_api.auth.repo import AuthRepo
from vibecanvas_api.services.workflow_approvers import resolve_workflow_approvers
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.workflow_history_repo import HistoryConflict, WorkflowHistoryRepo


async def owner():
    email = f"approval-{uuid.uuid4().hex}@example.com"
    async with session_scope() as session:
        user = await AuthRepo(session).register(email, "unused-password-hash")
        return str(user.tenant_id), str(user.user_id), email


@pytest.mark.asyncio
async def test_dispatch_claim_is_exclusive_and_survives_new_sessions(pg_engine):
    tenant, actor, _ = await owner()
    run_id = str(uuid.uuid4())
    async with session_scope(tenant_id=tenant) as session:
        await WorkflowHistoryRepo(session).create(
            execution_id=run_id, tenant_id=tenant, wf_id='qa-claim',
            source_type='workflow', source_id='qa-claim', initiator_user_id=actor,
            workflow={}, inputs={}, approvers={})

    async def claim():
        try:
            async with session_scope(tenant_id=tenant) as session:
                await WorkflowHistoryRepo(session).claim_dispatch(run_id)
            return 'claimed'
        except HistoryConflict as exc:
            assert str(exc) == 'execution_already_dispatched'
            return 'rejected'

    async with asyncio.timeout(10):
        results = await asyncio.gather(*(claim() for _ in range(8)))
    assert results.count('claimed') == 1
    assert results.count('rejected') == 7
    assert await claim() == 'rejected'
    async with session_scope(tenant_id=tenant) as session:
        await WorkflowHistoryRepo(session).bind_runtime(run_id, 'qa-generation')
    assert await claim() == 'rejected'
    async with session_scope(tenant_id=tenant) as session:
        await WorkflowHistoryRepo(session).fail(run_id, error_code='execution_failed')
    assert await claim() == 'rejected'


@pytest.mark.asyncio
async def test_execution_keeps_server_selected_version_in_private_snapshot(pg_engine):
    tenant, actor, _ = await owner()
    run_id = str(uuid.uuid4())
    async with session_scope(tenant_id=tenant) as session:
        repo = WorkflowHistoryRepo(session)
        await repo.create(execution_id=run_id, tenant_id=tenant, wf_id="wf-version", source_type="workflow",
                          source_id="wf-version", initiator_user_id=actor,
                          workflow={"__meta__": {"workflow_version": 99}}, inputs={}, approvers={},
                          workflow_version="v2.sv4")
    async with session_scope(tenant_id=tenant) as session:
        detail = await WorkflowHistoryRepo(session).detail(run_id)
        assert detail["workflow_version"] == "v2.sv4"
        assert detail["workflow"]["__meta__"]["workflow_version"] == 99


@pytest.mark.asyncio
async def test_cancelled_before_dispatch_never_starts_a_process(pg_engine, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from vibecanvas_api.services.workflow_execution_history import create_execution
    from vibecanvas_api.services.sandbox.session_executions import SessionExecutions, WorkflowRpcPool

    tenant, actor, _ = await owner()
    graph = approval_graph()
    execution_id = await create_execution(
        tenant_id=tenant,
        source_type="workflow",
        source_id="wf-pre-cancel",
        user_id=actor,
        workflow_id="wf-pre-cancel",
        workflow=graph,
        inputs={},
    )
    async with session_scope(tenant_id=tenant) as db:
        await WorkflowHistoryRepo(db).request_cancel(execution_id)
    factory = Mock(side_effect=AssertionError("A cancelled execution must not start a pool"))
    monkeypatch.setattr(WorkflowRpcPool, "for_session", factory)
    owner_runtime = SessionExecutions(SimpleNamespace(tenant_id=tenant, user_id=actor))
    response = await owner_runtime.run(
        group_id=uuid.uuid4().hex,
        execution_id=execution_id,
        workflow=graph,
        inputs={},
        context={},
        capacity=1,
    )
    assert response["status"]["status"] == "cancelled"
    assert response["result"]["error_dict"] == {"__engine__": "execution_cancelled"}
    factory.assert_not_called()
    async with session_scope(tenant_id=tenant) as db:
        detail = await WorkflowHistoryRepo(db).detail(execution_id)
        assert detail["status"] == "cancelled" and detail["generation"] is None


async def waiting_run(tenant, actor):
    run_id = str(uuid.uuid4())
    approval_id = uuid.uuid4().hex
    graph = {
        "node_2": {
            "node_type": "HumanApprovalNode",
            "node_id": "node_2",
            "node_config": {"instruction": "private instruction", "timeout_seconds": 60},
        }
    }
    event = {
        "type": "approval_requested",
        "seq": 1,
        "generation": "generation-a",
        "invocation_id": run_id,
        "approval_id": approval_id,
        "node_id": "node_2",
        "deadline": time.time() + 60,
        "instruction": "private instruction",
        "inputs": {"secret": "payload"},
    }
    async with session_scope(tenant_id=tenant) as session:
        repo = WorkflowHistoryRepo(session)
        await repo.create(
            execution_id=run_id,
            tenant_id=tenant,
            wf_id="wf-test",
            source_type="workflow",
            source_id="wf-test",
            initiator_user_id=actor,
            workflow=graph,
            inputs={"secret": "payload"},
            approvers={"node_2": actor},
        )
        await repo.bind_runtime(run_id, "generation-a")
        assert await repo.persist_events(run_id, "generation-a", [event]) == 1
    return run_id, approval_id, event


@pytest.mark.asyncio
async def test_history_encrypts_payload_and_fences_event_replay(pg_engine):
    tenant, actor, _ = await owner()
    run, approval, event = await waiting_run(tenant, actor)
    async with session_scope(tenant_id=tenant) as session:
        repo = WorkflowHistoryRepo(session)
        assert await repo.persist_events(run, "generation-a", [event]) == 1
        row = await repo.get(run)
        assert row["status"] == "waiting_approval"
        assert "payload" not in row["private_ciphertext"]
        assert (await repo.detail(run))["inputs"] == {"secret": "payload"}
        assert len(await repo.events(run)) == 1
        with pytest.raises(HistoryConflict):
            await repo.persist_events(run, "generation-b", [event])
    async with session_scope(tenant_id=tenant) as session:
        repo = WorkflowHistoryRepo(session)
        await repo.fail(run, error_code="execution_lost", generation="generation-a")
        detail = await repo.detail(run)
        assert detail["status"] == "failed"
        assert detail["approvals"][0]["status"] == "execution_lost"
        assert detail["result"]["error_dict"] == {"__engine__": "execution_lost"}
        with pytest.raises(HistoryConflict):
            await repo.persist_events(run, "generation-a", [{**event, "seq": 2}])
    other_tenant, _, _ = await owner()
    async with session_scope(tenant_id=other_tenant) as session:
        assert await WorkflowHistoryRepo(session).get(run) is None
        assert not await WorkflowHistoryRepo(session).is_assignee(run, actor)


@pytest.mark.asyncio
async def test_competing_decisions_create_one_command(pg_engine):
    tenant, actor, _ = await owner()
    run, approval, _ = await waiting_run(tenant, actor)

    async def decide(value):
        try:
            async with session_scope(tenant_id=tenant) as session:
                return await WorkflowHistoryRepo(session).request_decision(
                    run, approval, actor_user_id=actor, approved=value
                )
        except HistoryConflict:
            return None

    results = await asyncio.gather(decide(True), decide(False))
    assert sum(item is not None for item in results) == 1
    chosen = next(item for item in results if item is not None)["requested_decision"]
    async with session_scope(tenant_id=tenant) as session:
        repo = WorkflowHistoryRepo(session)
        commands = await repo.pending_commands(run)
        assert len(commands) == 1
        assert commands[0]["requested_decision"] == chosen
        assert (await repo.request_decision(run, approval, actor_user_id=actor, approved=chosen))[
            "requested_decision"
        ] == chosen
        with pytest.raises(KeyError):
            await repo.request_decision(run, approval, actor_user_id=str(uuid.uuid4()), approved=True)
        await session.execute(
            text("UPDATE workflow_execution_approvals SET deadline=now()-interval '1 second' WHERE id=:id"),
            {"id": approval},
        )
        with pytest.raises(HistoryConflict):
            await repo.request_decision(run, approval, actor_user_id=actor, approved=chosen)


@pytest.mark.asyncio
async def test_assignees_use_email_active_membership_and_default_initiator(pg_engine):
    tenant, actor, email = await owner()
    other_tenant, other_actor, other_email = await owner()
    graph = {"__meta__": {}, "node_2": {"node_id": "node_2", "node_type": "HumanApprovalNode", "node_config": {}}}
    async with session_scope(tenant_id=tenant) as session:

        async def resolve(explicit=False):
            return await resolve_workflow_approvers(
                session, tenant_id=tenant, workflow=graph, initiator_user_id=actor, require_explicit=explicit
            )

        assert await resolve() == {"node_2": actor}
        with pytest.raises(ValueError, match="approval_email_required"):
            await resolve(True)
        graph["node_2"]["node_config"]["approver_email"] = email
        assert await resolve(True) == {"node_2": actor}
        graph["node_2"]["node_config"]["approver_email"] = other_email
        with pytest.raises(ValueError, match="approval_assignee_unavailable"):
            await resolve(True)
        graph["node_2"]["node_config"]["approver_email"] = email
        await session.execute(
            text("UPDATE org_memberships SET status='suspended' WHERE user_id=:id AND tenant_id=:tenant"),
            {"id": uuid.UUID(actor), "tenant": uuid.UUID(tenant)},
        )
        with pytest.raises(ValueError, match="approval_assignee_unavailable"):
            await resolve(True)


def approval_graph():
    def node(identifier, name, kind, inputs, outputs, config, children):
        return {
            "node_id": identifier,
            "node_name": name,
            "node_type": kind,
            "node_description": name,
            "input_fields": inputs,
            "output_fields": outputs,
            "node_config": config,
            "children": children,
        }

    boolean = {"approved": {"type": "boolean", "description": "Decision"}}
    return {
        "node_1": node("node_1", "__start__", "StartNode", {}, {}, {}, ["node_2"]),
        "node_2": node(
            "node_2",
            "review",
            "HumanApprovalNode",
            {},
            boolean,
            {"instruction": "Review", "timeout_seconds": 30},
            ["node_3"],
        ),
        "node_3": node(
            "node_3",
            "__end__",
            "EndNode",
            {"approved": {"type": "boolean", "value": False, "reference": "review.approved"}},
            boolean,
            {},
            [],
        ),
    }


@pytest.mark.asyncio
async def test_sandbox_rpc_driver_continues_the_original_execution(pg_engine, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    # This fixture isolates RPC ownership and persistence from the identity
    # provider. Original-principal fences are covered in test_workflow_resume.
    monkeypatch.setattr(
        "vibecanvas_api.services.sandbox.workflow_execution_driver.refresh_execution_context",
        AsyncMock(return_value={"llm_credentials": {}, "workflow_resources": {}}),
    )
    from vibecanvas_api.services import workflow_artifacts
    from vibecanvas_api.services.object_store import FilesystemObjectStore
    from vibecanvas_api.storage.vfs_run_repo import VfsRunRepo
    from vibecanvas_api.services.sandbox.bubblewrap import BubblewrapProvider
    from vibecanvas_api.services.sandbox.workflow_execution_driver import WorkflowExecutionDriver
    from vibecanvas_api.services.sandbox.workflow_rpc_slot import WorkflowRpcWorker, WorkflowInvocationSlot

    if not shutil.which("bwrap"):
        pytest.skip("bubblewrap is required for sandbox integration")
    tenant, actor, _ = await owner()
    run_id = str(uuid.uuid4())
    graph = approval_graph()
    async with session_scope(tenant_id=tenant) as session:
        await WorkflowHistoryRepo(session).create(
            execution_id=run_id,
            tenant_id=tenant,
            wf_id="wf-driver",
            source_type="workflow",
            source_id="wf-driver",
            initiator_user_id=actor,
            workflow=graph,
            inputs={},
            approvers={"node_2": actor},
        )
    with TemporaryDirectory(prefix="fw-rpc-") as root:
        worker = WorkflowRpcWorker(
            provider=BubblewrapProvider(shutil.which("bwrap")), root=root, revision="v1", workflow=graph
        )
        slot = WorkflowInvocationSlot(worker)
        saved_artifacts = []
        store = FilesystemObjectStore(root=str(tmp_path / "objects"))
        monkeypatch.setattr(workflow_artifacts, "get_object_store", lambda: store)

        async def save_artifacts():
            await workflow_artifacts.persist_workflow_artifacts(
                root=str(slot.artifacts), tenant_id=tenant, execution_id=run_id, wf_id="wf-driver"
            )
            saved_artifacts.append(run_id)

        task = None
        try:
            await slot.start()
            (slot.artifacts / "report.txt").write_text("retained report")
            private_file = tmp_path / "host-private.txt"
            private_file.write_text("must never be exported")
            (slot.artifacts / "unsafe-link.txt").symlink_to(private_file)
            pid = slot.handle.proc.pid
            driver = WorkflowExecutionDriver(
                tenant_id=tenant, execution_id=run_id, slot=slot, persist_artifacts=save_artifacts
            )
            task = asyncio.create_task(driver.run(inputs={}, context={}))
            async with asyncio.timeout(10):
                while True:
                    async with session_scope(tenant_id=tenant) as session:
                        detail = await WorkflowHistoryRepo(session).detail(run_id)
                    if detail["status"] == "waiting_approval":
                        break
                    if task.done():
                        await task
                        raise AssertionError("execution finished before approval")
                    await asyncio.sleep(0.05)
            async with session_scope(tenant_id=tenant) as session:
                # Evidence is already durable before the reviewer decides.
                artifacts = VfsRunRepo(session, store, tenant)
                assert await artifacts.read_bytes(run_id=run_id, path="/run/report.txt") == b"retained report"
                assert await artifacts.read(run_id=run_id, path="/run/unsafe-link.txt") is None
                await WorkflowHistoryRepo(session).request_decision(
                    run_id, detail["approvals"][0]["id"], actor_user_id=actor, approved=True
                )
            result = await asyncio.wait_for(task, 10)
            assert result["final_outputs"]["__end__"] == {"approved": True}
            assert saved_artifacts == [run_id, run_id]
            assert slot.alive and slot.handle.proc.pid == pid
            assert slot.invocation_id is None
            async with session_scope(tenant_id=tenant) as session:
                repo = WorkflowHistoryRepo(session)
                final = await repo.detail(run_id)
                assert final["status"] == "succeeded"
                assert final["approvals"][0]["status"] == "approved"
                frames = await repo.events(run_id)
                assert sum(e.get("node_id") == "node_1" and e.get("status") == "running" for e in frames) == 1
                artifacts = VfsRunRepo(session, store, tenant)
                assert await artifacts.read_bytes(run_id=run_id, path="/run/report.txt") == b"retained report"
                assert await artifacts.read(run_id=run_id, path="/run/unsafe-link.txt") is None
                await artifacts.release(run_id=run_id, retain=False)
                assert await artifacts.purge_workflow_runs(wf_id="wf-driver") == 0
                assert await artifacts.read_bytes(run_id=run_id, path="/run/report.txt") == b"retained report"
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await worker.close()


@pytest.mark.asyncio
async def test_lost_approval_process_is_not_resumed_and_capacity_is_reusable(pg_engine, monkeypatch):
    from unittest.mock import AsyncMock

    # This fixture isolates RPC ownership and persistence from the identity
    # provider. Original-principal fences are covered in test_workflow_resume.
    monkeypatch.setattr(
        "vibecanvas_api.services.sandbox.workflow_execution_driver.refresh_execution_context",
        AsyncMock(return_value={"llm_credentials": {}, "workflow_resources": {}}),
    )
    from vibecanvas_api.services.sandbox.bubblewrap import BubblewrapProvider
    from vibecanvas_api.services.sandbox.workflow_execution_driver import WorkflowExecutionDriver
    from vibecanvas_api.services.sandbox.workflow_rpc_pool import WorkflowRpcPool, WorkflowPoolFull
    from vibecanvas_api.services.sandbox.workflow_rpc_slot import WorkflowRpcWorker

    if not shutil.which("bwrap"):
        pytest.skip("bubblewrap is required")
    tenant, actor, _ = await owner()
    graph = approval_graph()

    async def artifacts():
        pass

    with TemporaryDirectory(prefix="fw-loss-") as root:
        pool = WorkflowRpcPool(
            capacity=1,
            factory=lambda index: WorkflowRpcWorker(
                provider=BubblewrapProvider(shutil.which("bwrap")),
                root=f"{root}/{index}",
                revision="v1",
                workflow=graph,
            ),
        )
        generations = []
        task = None
        try:
            for kill in (True, False):
                execution_id = str(uuid.uuid4())
                async with session_scope(tenant_id=tenant) as session:
                    await WorkflowHistoryRepo(session).create(
                        execution_id=execution_id,
                        tenant_id=tenant,
                        wf_id="wf-loss",
                        source_type="workflow",
                        source_id="wf-loss",
                        initiator_user_id=actor,
                        workflow=graph,
                        inputs={},
                        approvers={"node_2": actor},
                    )

                async def execute():
                    async with pool.acquire(execution_id) as slot:
                        generations.append(slot.client.generation)
                        return await WorkflowExecutionDriver(
                            tenant_id=tenant,
                            execution_id=execution_id,
                            slot=slot,
                            persist_artifacts=artifacts,
                        ).run(inputs={}, context={})

                task = asyncio.create_task(execute())
                async with asyncio.timeout(10):
                    while True:
                        async with session_scope(tenant_id=tenant) as session:
                            detail = await WorkflowHistoryRepo(session).detail(execution_id)
                        if detail["status"] == "waiting_approval":
                            break
                        if task.done():
                            await task
                            pytest.fail("execution ended before requesting approval")
                        await asyncio.sleep(0.05)
                approval_id = detail["approvals"][0]["id"]
                with pytest.raises(WorkflowPoolFull):
                    async with pool.acquire(str(uuid.uuid4())):
                        pytest.fail("approval must retain its worker slot")
                if kill:
                    await pool._workers[0].close()
                else:
                    async with session_scope(tenant_id=tenant) as session:
                        await WorkflowHistoryRepo(session).request_decision(
                            execution_id,
                            approval_id,
                            actor_user_id=actor,
                            approved=True,
                        )
                result = await asyncio.wait_for(task, 10)
                assert not pool.busy
                async with session_scope(tenant_id=tenant) as session:
                    history = WorkflowHistoryRepo(session)
                    detail = await history.detail(execution_id)
                    if kill:
                        assert detail["status"] == "failed" and detail["error_code"] == "execution_lost"
                        assert detail["approvals"][0]["status"] == "execution_lost"
                        with pytest.raises(HistoryConflict):
                            await history.request_decision(
                                execution_id, approval_id, actor_user_id=actor, approved=True
                            )
                    else:
                        assert result["final_outputs"]["__end__"] == {"approved": True}
            assert generations[0] != generations[1]
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [None, False])
async def test_approval_timeout_is_not_a_rejection(pg_engine, decision):
    tenant, actor, _ = await owner()
    run, approval, _ = await waiting_run(tenant, actor)
    events = [
        {"type": "approval_resolved", "seq": 2, "generation": "generation-a",
         "invocation_id": run, "approval_id": approval, "reason": "timeout",
         "approved": decision, "decided_at": time.time()},
        {"type": "result", "seq": 3, "generation": "generation-a", "invocation_id": run,
         "status": "timed_out", "error_code": "approval_timeout", "final_outputs": {},
         "error_dict": {"__engine__": "approval_timeout"}, "execution_time": 1},
    ]
    if decision is False:
        with pytest.raises(HistoryConflict, match="invalid_approval_resolution"):
            async with session_scope(tenant_id=tenant) as session:
                await WorkflowHistoryRepo(session).persist_events(run, "generation-a", events)
        return
    async with session_scope(tenant_id=tenant) as session:
        await WorkflowHistoryRepo(session).persist_events(run, "generation-a", events)
    async with session_scope(tenant_id=tenant) as session:
        detail = await WorkflowHistoryRepo(session).detail(run)
        assert detail["status"] == "timed_out" and detail["error_code"] == "approval_timeout"
        assert detail["approvals"][0]["status"] == "timeout"
        assert detail["approvals"][0]["approved"] is None
        assert detail["result"]["final_outputs"] == {}
        with pytest.raises(HistoryConflict):
            await WorkflowHistoryRepo(session).request_decision(run, approval, actor_user_id=actor, approved=True)
