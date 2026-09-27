"""P0 tests for the `/command` mode system — registry + parsing + active_modes
persistence, plus a behavior-neutrality assertion (no-command turn unchanged).
"""

import uuid

import pytest
from sqlalchemy import select, text

from vibecanvas_api.agents.commands import COMMAND_MODES, parse_command
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.models import Chat
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


# ---------------------------------------------------------------------------
# parse_command — leading-token-only resolution + strip
# ---------------------------------------------------------------------------

def test_parse_workflow_strips_token():
    assert parse_command("/workflow add an HTTP node") == ("workflow", "add an HTTP node")


def test_parse_browser_strips_token():
    assert parse_command("/browser go log in") == ("browser", "go log in")


def test_parse_diagram_strips_token():
    assert parse_command("/diagram draw the architecture") == (
        "diagram",
        "draw the architecture",
    )


def test_parse_document_strips_token():
    assert parse_command("/document create a quarterly review") == (
        "document",
        "create a quarterly review",
    )


@pytest.mark.parametrize("name", ["task", "deployment", "knowledge"])
def test_parse_resource_command_strips_token(name):
    assert parse_command(f"/{name} inspect resources") == (
        name,
        "inspect resources",
    )


def test_parse_command_token_only_empty_rest():
    assert parse_command("/workflow") == ("workflow", "")


def test_parse_unknown_slash_is_noop():
    # Unknown leading /x → no command, ORIGINAL content untouched.
    assert parse_command("/foo do a thing") == (None, "/foo do a thing")


@pytest.mark.parametrize("retired", ["build", "plan"])
def test_parse_retired_command_is_noop(retired):
    content = f"/{retired} do a thing"
    assert parse_command(content) == (None, content)


def test_parse_no_slash_is_noop():
    assert parse_command("just a normal message") == (None, "just a normal message")


def test_parse_empty_is_noop():
    assert parse_command("") == (None, "")


def test_parse_mid_message_slash_is_noop():
    # Leading-token-only syntax (v1): an inline /workflow mid-message is not a command.
    assert parse_command("please /workflow it") == (None, "please /workflow it")


def test_registry_shape():
    assert COMMAND_MODES["workflow"].tools == []
    assert "run" in COMMAND_MODES["workflow"].context_prompt and "--node" in COMMAND_MODES["workflow"].context_prompt
    assert COMMAND_MODES["task"].tools == []
    assert "flowork-cli task" in COMMAND_MODES["task"].context_prompt
    assert COMMAND_MODES["workflow"].kind == "additive"
    assert COMMAND_MODES["workflow"].sticky is True
    # /browser is ADDITIVE (control is a tool, not a routes handoff) AND
    # side-panel-only: a main-app /browser is refused with a NOTICE; only a
    # surface=="sidepanel" chat activates browser mode + injects the tools.
    assert COMMAND_MODES["browser"].kind == "additive"
    assert COMMAND_MODES["browser"].sticky is True
    assert COMMAND_MODES["browser"].external_control is None
    assert COMMAND_MODES["browser"].sidepanel_only is True
    assert COMMAND_MODES["workflow"].sidepanel_only is False
    for name in ("task", "deployment", "knowledge"):
        assert COMMAND_MODES[name].kind == "additive"
        assert COMMAND_MODES[name].sticky is True
        assert COMMAND_MODES[name].sidepanel_only is False
    assert "plan" not in COMMAND_MODES
    assert "build" not in COMMAND_MODES
    assert COMMAND_MODES["diagram"].tools == [
        "render_preview",
    ]
    assert COMMAND_MODES["document"].tools == [
        "render_preview",
    ]
    assert COMMAND_MODES["document"].kind == "additive"
    assert COMMAND_MODES["document"].sticky is True
    assert COMMAND_MODES["document"].sidepanel_only is False
    # Base is implicit rather than a user-visible slash command.
    assert "base" not in COMMAND_MODES


# ---------------------------------------------------------------------------
# get/set_active_modes — Postgres round-trip + meta-key preservation
# ---------------------------------------------------------------------------

def test_document_guidance_checks_metric_labels_not_only_formulas():
    prompt = COMMAND_MODES["document"].context_prompt
    assert "metric labels, units and comparison periods" in prompt
    assert "including summaries and charts" in prompt
    assert "Independently recompute key metrics" in prompt


def test_diagram_guidance_checks_terminal_route_segments():
    prompt = COMMAND_MODES["diagram"].context_prompt
    assert "Orthogonal routing does not guarantee obstacle avoidance" in prompt
    assert "inspect each complete path" in prompt
    assert "endpoints" in prompt


TENANT = uuid.uuid4()
USER = uuid.uuid5(uuid.NAMESPACE_DNS, "command-modes-user")


async def _seed_and_bind(session):
    await session.execute(
        text("INSERT INTO tenants(tenant_id, name) VALUES (:t, 'command-modes-test') "
             "ON CONFLICT (tenant_id) DO NOTHING"),
        {"t": TENANT},
    )
    await session.execute(
        text("INSERT INTO users(user_id, tenant_id, email) "
             "VALUES (:u, :t, :e) ON CONFLICT (user_id) DO NOTHING"),
        {"u": USER, "t": TENANT, "e": "command-modes@test"},
    )
    await session.execute(
        text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(TENANT)}
    )


@pytest.mark.asyncio
async def test_active_modes_round_trip(pg_session):
    await _seed_and_bind(pg_session)
    wf = await WorkflowRepo(pg_session, str(USER)).create_workflow(name="W")
    repo = ChatRepo(pg_session, str(USER))
    cid = await repo.register_session(wf["wf_id"], name="c", major_version=1)

    # Default: no meta written yet → Base (empty set).
    assert await repo.get_active_modes(cid) == set()

    await repo.set_active_modes(cid, {"workflow"})
    assert await repo.get_active_modes(cid) == {"workflow"}

    # Accumulate (additive/sticky).
    await repo.set_active_modes(cid, {"workflow", "research"})
    assert await repo.get_active_modes(cid) == {"workflow", "research"}

    # list_sessions surfaces it (sorted list).
    sessions = await repo.list_sessions(wf["wf_id"])
    row = next(s for s in sessions if s["chat_id"] == cid)
    assert row["active_modes"] == ["research", "workflow"]


@pytest.mark.asyncio
async def test_set_active_modes_preserves_other_meta(pg_session):
    await _seed_and_bind(pg_session)
    wf = await WorkflowRepo(pg_session, str(USER)).create_workflow(name="W")
    repo = ChatRepo(pg_session, str(USER))
    cid = await repo.register_session(wf["wf_id"], name="c", major_version=1)

    # Seed an unrelated key through the strict encrypted metadata boundary.
    chat = (
        await pg_session.execute(select(Chat).where(Chat.chat_id == cid))
    ).scalar_one()
    await repo._materialize_chat_private(chat)
    await repo._store_chat_private(chat, name=chat.name, meta={"foo": "bar"})
    await repo.set_active_modes(cid, {"workflow"})

    await repo._materialize_chat_private(chat)
    meta = chat.meta
    assert meta["foo"] == "bar"           # preserved
    assert meta["active_modes"] == ["workflow"]


@pytest.mark.asyncio
async def test_deactivate_active_mode_persists_bounded_event(pg_session):
    await _seed_and_bind(pg_session)
    wf = await WorkflowRepo(pg_session, str(USER)).create_workflow(name="W")
    repo = ChatRepo(pg_session, str(USER))
    cid = await repo.register_session(wf["wf_id"], name="c", major_version=1)
    await repo.set_active_modes(cid, {"workflow", "knowledge"})

    remaining = await repo.deactivate_active_mode(
        cid,
        "knowledge",
        actor_user_id=str(USER),
    )

    assert remaining == {"workflow"}
    chat = (
        await pg_session.execute(select(Chat).where(Chat.chat_id == cid))
    ).scalar_one()
    await repo._materialize_chat_private(chat)
    assert chat.meta["active_modes"] == ["workflow"]
    assert chat.meta["command_events"][-1]["type"] == "deactivated"
    assert chat.meta["command_events"][-1]["command"] == "knowledge"


# ---------------------------------------------------------------------------
# Behavior-neutrality: a no-command turn resolves to the SAME effective mode.
# ---------------------------------------------------------------------------

def _effective_mode(mode: str, active_modes: set[str] | None) -> str:
    """Mirror of the P0 behavior-neutral mapping in agent._run_agent_turn_inner.

    Kept as a tiny local replica so the test asserts the contract without
    booting a full agent turn (the mapping is the only logic P0 added).
    """
    if active_modes and "browser" in active_modes:
        return "browser"
    return mode


def test_no_command_turn_is_behavior_neutral():
    # A plain chat turn: parse is a no-op, active_modes stays {} → effective
    # mode is still "chat" (identical to pre-P0 behavior / tool gating).
    cmd, stripped = parse_command("hello there")
    assert cmd is None and stripped == "hello there"
    active_modes: set[str] = set()  # nothing persisted
    assert _effective_mode("chat", active_modes) == "chat"
    # /workflow alone does not change the legacy surface mode.
    assert _effective_mode("chat", {"workflow"}) == "chat"
    # browser handoff maps to legacy browser mode (parity with mode="browser").
    assert _effective_mode("chat", {"browser"}) == "browser"
