"""Chat and execution routes call ``run_agent_turn`` without a thread bridge."""
import inspect

from vibecanvas_api.routes import chats, executions


def test_chats_route_does_not_reference_thread_bridge():
    src = inspect.getsource(chats)
    for sym in ("run_sync_agent_in_thread", "stream_buffer_as_sse"):
        assert sym not in src, f"{sym} lingers in chats.py"


def test_executions_route_does_not_reference_thread_bridge():
    src = inspect.getsource(executions)
    for sym in ("run_sync_agent_in_thread", "stream_buffer_as_sse"):
        assert sym not in src, f"{sym} lingers in executions.py"
