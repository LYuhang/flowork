"""ExecutionRepo Postgres backend — per-node granularity + cross-instance
cancellation.

The 3rd test pins the mandated deviation-from-verbatim: the stop-event
registry MUST be process-shared (module-level), not per-DI-instance, or
cancellation silently never works across the start / cancel / SSE-producer
ExecutionRepo instances (each built fresh by ``Depends``).
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from vibecanvas_api.storage.workflow_repo import WorkflowRepo
from vibecanvas_api.storage.execution_repo import ExecutionRepo

# WorkflowRepo/ExecutionRepo stamp rows with the authenticated user id, which
# in production is a UUID string (AuthContext.user_id = str(sessions.user_id))
# and is an FK to users.user_id. The workflow/execution tenant_id columns
# resolve from the `app.tenant_id` GUC server-default. So a repo test must
# seed a tenant + the user row and bind the session's tenant GUC.
TENANT = uuid.uuid4()
USER = uuid.uuid5(uuid.NAMESPACE_DNS, "exec-repo-user")


async def _seed_and_bind(session):
    """Seed the tenant + user (idempotent) and bind ``app.tenant_id`` on this
    session so the tenant_id server-default resolves.

    pg_session/pg_engine connect as the RLS-bypassing superuser, so a plain
    INSERT works; the GUC is still required because tenant_id's DEFAULT reads
    ``current_setting('app.tenant_id')``.
    """
    await session.execute(
        text("INSERT INTO tenants(tenant_id, name) VALUES (:t, 'exec-repo-test') "
             "ON CONFLICT (tenant_id) DO NOTHING"),
        {"t": TENANT},
    )
    await session.execute(
        text("INSERT INTO users(user_id, tenant_id, email) "
             "VALUES (:u, :t, :e) ON CONFLICT (user_id) DO NOTHING"),
        {"u": USER, "t": TENANT, "e": "exec-repo@test"},
    )
    # Mirror db.py session_scope: set_config (NOT `SET`) accepts a bound param.
    # is_local=false → the GUC survives across this session's commits (the
    # concurrent tests commit mid-transaction).
    await session.execute(
        text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(TENANT)}
    )


@pytest.mark.asyncio
async def test_per_node_then_finish(pg_session):
    private_marker = "SENSITIVE_WORKFLOW_RUN_MARKER_92af"
    await _seed_and_bind(pg_session)
    wf = await WorkflowRepo(pg_session, str(USER)).create_workflow(name="W")
    repo = ExecutionRepo(pg_session, str(USER))
    await repo.start_execution(wf["wf_id"], (1, 0), "e1",
                               target_node_id=None)
    await repo.update_node_execution("e1", "node_1", status="success",
                                     result_overwrite=private_marker, duration=0.12)
    await repo.finish_execution("e1", status="success")
    rec = await repo.get_execution("e1")
    assert rec["status"] == "success"
    assert rec["per_node"]["node_1"]["status"] == "success"
    assert rec["per_node"]["node_1"]["duration"] == 0.12
    stored = (
        await pg_session.execute(
            text(
                "SELECT s.private_ciphertext, "
                "string_agg(e.payload_ciphertext, '') AS event_ciphertext "
                "FROM workflow_run_state s JOIN workflow_run_events e "
                "ON e.wf_id=s.wf_id WHERE s.wf_id=:wf_id "
                "GROUP BY s.private_ciphertext"
            ),
            {"wf_id": wf["wf_id"]},
        )
    ).mappings().one()
    old_columns = (
        await pg_session.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND (("
                "table_name='workflow_run_state' AND column_name IN ("
                "'node_states','error')) OR (table_name='workflow_run_events' "
                "AND column_name='payload'))"
            )
        )
    ).all()
    assert private_marker not in stored["private_ciphertext"]
    assert private_marker not in stored["event_ciphertext"]
    assert old_columns == []


@pytest.mark.asyncio
async def test_finish_execution_closes_running_nodes(pg_session):
    await _seed_and_bind(pg_session)
    wf = await WorkflowRepo(pg_session, str(USER)).create_workflow(name="W")
    repo = ExecutionRepo(pg_session, str(USER))
    await repo.start_execution(wf["wf_id"], (1, 0), "e1_close",
                               target_node_id=None)
    await repo.update_node_execution("e1_close", "node_1", status="running")

    await repo.finish_execution("e1_close", status="success")

    rec = await repo.get_execution("e1_close")
    assert rec["status"] == "success"
    assert rec["per_node"]["node_1"]["status"] == "completed"


@pytest.mark.asyncio
async def test_failed_execution_still_recorded(pg_session):
    await _seed_and_bind(pg_session)
    wf = await WorkflowRepo(pg_session, str(USER)).create_workflow(name="W")
    repo = ExecutionRepo(pg_session, str(USER))
    await repo.start_execution(wf["wf_id"], (1, 0), "e2",
                               target_node_id="node_1")
    await repo.update_node_execution("e2", "node_1", status="error", error="node boom")
    await repo.finish_execution("e2", status="error", error="boom")
    rec = await repo.get_execution("e2")
    assert rec["status"] == "error" and rec["error"] == "boom"
    assert rec["per_node"]["node_1"]["error"] == "node boom"


@pytest.mark.asyncio
async def test_cross_instance_cancellation(pg_session):
    """Stop-event registry is process-shared, not per-DI-instance.

    Instance A starts the execution; a SEPARATE instance B (mirroring the
    cancel request's fresh ``Depends`` repo) must see the same stop Event
    and be able to set it so A's engine-side poll observes the signal.
    """
    await _seed_and_bind(pg_session)
    wf = await WorkflowRepo(pg_session, str(USER)).create_workflow(name="W")
    a = ExecutionRepo(pg_session, str(USER))
    await a.start_execution(wf["wf_id"], (1, 0), "e3", target_node_id=None)

    b = ExecutionRepo(pg_session, str(USER))
    assert b.get_stop_event("e3") is not None
    await b.stop_execution("e3")

    assert a.get_stop_event("e3").is_set()
    assert (await a.get_execution("e3"))["status"] == "stopped"


@pytest.mark.asyncio
async def test_stop_execution_closes_running_nodes(pg_session):
    await _seed_and_bind(pg_session)
    wf = await WorkflowRepo(pg_session, str(USER)).create_workflow(name="W")
    repo = ExecutionRepo(pg_session, str(USER))
    await repo.start_execution(wf["wf_id"], (1, 0), "e3_close",
                               target_node_id=None)
    await repo.update_node_execution("e3_close", "node_done", status="success")
    await repo.update_node_execution("e3_close", "node_running", status="running")

    await repo.stop_execution("e3_close")

    rec = await repo.get_execution("e3_close")
    assert rec["status"] == "stopped"
    assert rec["per_node"]["node_done"]["status"] == "success"
    assert rec["per_node"]["node_running"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_concurrent_node_updates_no_lost_update(pg_session, pg_engine):
    """Two parallel branch nodes finishing near-simultaneously, each in
    its own short session (§5.3 producer pattern), must NOT clobber each
    other's ``per_node`` entry.

    This fails against the old whole-``per_node`` read-modify-write (both
    sessions read the same blob, add their own key, write the whole blob
    back → last writer wins, the other node's entry is lost) and passes
    with the atomic ``with_for_update`` row-locked RMW (the 2nd UPDATE
    re-reads the 1st's committed ``per_node``).
    """
    await _seed_and_bind(pg_session)
    wf = await WorkflowRepo(pg_session, str(USER)).create_workflow(name="W")
    await ExecutionRepo(pg_session, str(USER)).start_execution(
        wf["wf_id"], (1, 0), "e4", target_node_id=None)
    await pg_session.commit()

    sm = async_sessionmaker(pg_engine, expire_on_commit=False)

    async def upd(node_id: str):
        async with sm() as s:
            await s.execute(
                text("SELECT set_config('app.tenant_id', :t, false)"),
                {"t": str(TENANT)},
            )
            await ExecutionRepo(s, str(USER)).update_node_execution(
                "e4", node_id, status="success")
            await s.commit()

    # Different keys, interleaved — neither may be lost.
    await asyncio.gather(upd("node_a"), upd("node_b"))

    async with sm() as s:
        await s.execute(
            text("SELECT set_config('app.tenant_id', :t, false)"),
            {"t": str(TENANT)},
        )
        rec = await ExecutionRepo(s, str(USER)).get_execution("e4")
    assert rec["per_node"]["node_a"]["status"] == "success"
    assert rec["per_node"]["node_b"]["status"] == "success"

    # Same key, concurrent — the row-lock serializes the two RMWs so the
    # surviving value is consistent (not a lost/torn write).
    async def upd_same(result: str):
        async with sm() as s:
            await s.execute(
                text("SELECT set_config('app.tenant_id', :t, false)"),
                {"t": str(TENANT)},
            )
            await ExecutionRepo(s, str(USER)).update_node_execution(
                "e4", "node_a", status="success", result_overwrite=result)
            await s.commit()

    await asyncio.gather(upd_same("r1"), upd_same("r2"))
    async with sm() as s:
        await s.execute(
            text("SELECT set_config('app.tenant_id', :t, false)"),
            {"t": str(TENANT)},
        )
        rec = await ExecutionRepo(s, str(USER)).get_execution("e4")
    assert rec["per_node"]["node_a"]["status"] == "success"
    assert rec["per_node"]["node_a"]["execution_result"] in ("r1", "r2")
    # The other node's entry survived both same-key updates.
    assert rec["per_node"]["node_b"]["status"] == "success"


@pytest.mark.asyncio
async def test_shared_workflow_keeps_each_users_state_and_events(pg_session):
    await _seed_and_bind(pg_session)
    second_user = uuid.uuid4()
    await pg_session.execute(text('INSERT INTO users(user_id,tenant_id,email) VALUES (:u,:t,:e)'),
                             {'u':second_user, 't':TENANT, 'e':f'{second_user}@example.com'})
    workflow = await WorkflowRepo(pg_session, str(USER)).create_workflow(name='Private interactive state')
    first = ExecutionRepo(pg_session, str(USER))
    second = ExecutionRepo(pg_session, str(second_user))
    await first.start_execution(workflow['wf_id'], (1,0), 'first-user-run')
    await first.update_node_execution('first-user-run', 'node_1', status='success', result_overwrite='first-private')
    await second.start_execution(workflow['wf_id'], (1,0), 'second-user-run')
    assert await second.get_execution('first-user-run') is None
    assert await first.get_execution('second-user-run') is None
    assert (await first.latest_execution(workflow['wf_id']))['exec_id'] == 'first-user-run'
    assert (await second.latest_execution(workflow['wf_id']))['exec_id'] == 'second-user-run'
    before = await pg_session.scalar(text('SELECT count(*) FROM workflow_run_events WHERE wf_id=:w AND creator_user_id=:u'),
                                     {'w':workflow['wf_id'], 'u':USER})
    await second.finish_execution('second-user-run', status='success')
    await second.start_execution(workflow['wf_id'], (1,0), 'second-user-retry')
    after = await pg_session.scalar(text('SELECT count(*) FROM workflow_run_events WHERE wf_id=:w AND creator_user_id=:u'),
                                    {'w':workflow['wf_id'], 'u':USER})
    assert after == before and before > 0
    assert (await first.get_execution('first-user-run'))['per_node']['node_1']['status'] == 'success'
    await first.finish_execution('first-user-run', status='success')
    await second.finish_execution('second-user-retry', status='success')


@pytest.mark.asyncio
async def test_execution_of_foreign_workflow_keeps_callers_tenant(pg_session):
    await _seed_and_bind(pg_session)
    workflow = await WorkflowRepo(pg_session, str(USER)).create_workflow(name='Shared source')
    recipient_tenant, recipient = uuid.uuid4(), uuid.uuid4()
    await pg_session.execute(text("INSERT INTO tenants(tenant_id,name) VALUES (:t,'recipient')"), {'t':recipient_tenant})
    await pg_session.execute(text('INSERT INTO users(user_id,tenant_id,email) VALUES (:u,:t,:e)'),
                             {'u':recipient, 't':recipient_tenant, 'e':f'{recipient}@example.com'})
    await pg_session.execute(text("SELECT set_config('app.tenant_id', :t, false)"), {'t':str(recipient_tenant)})
    repo = ExecutionRepo(pg_session, str(recipient))
    await repo.start_execution(workflow['wf_id'], (1,0), 'foreign-source-run', workflow_tenant_id=str(TENANT))
    row = (await pg_session.execute(text('SELECT tenant_id,workflow_tenant_id,creator_user_id FROM workflow_run_state WHERE turn_id=:id'),
                                    {'id':'foreign-source-run'})).one()
    assert row == (recipient_tenant, TENANT, recipient)
    assert await pg_session.scalar(text("SELECT current_setting('app.tenant_id')")) == str(recipient_tenant)
    await repo.finish_execution('foreign-source-run', status='success')
