import pytest
from vibecanvas_api.services.sandbox import get_sandbox_provider, _gvisor_runnable, EngineRunResult
from vibecanvas_engine.nodes import ENGINE_PURE_NODE_TYPES

gvisor = pytest.mark.skipif(not _gvisor_runnable(), reason="rootless gVisor not runnable here")

# PromptNode excluded (needs LLM credentials, per user). All other engine types attempted.
_TYPES = sorted(ENGINE_PURE_NODE_TYPES - {"PromptNode"})


def _minimal_node(node_type: str) -> dict:
    base = {"node_id": "node_1", "node_name": "n", "node_type": node_type,
            "node_description": "d", "input_fields": {}, "output_fields": {},
            "node_config": {}, "children": []}
    if node_type == "CodeNode":
        base["node_config"] = {"programming_language": "python",
                               "process_fn": "def process_fn(inputs):\n    return {'ok': 1}"}
        base["output_fields"] = {"ok": {"type": "integer", "description": "ok"}}
    return base


@gvisor
@pytest.mark.parametrize("node_type", _TYPES)
def test_single_node_runs_in_sandbox(node_type, tmp_path):
    """Each engine node type (minus PromptNode) executes as a single node INSIDE
    gVisor and its outcome propagates OUT — the engine never crashes (__engine__)."""
    res = get_sandbox_provider().run_node(
        run_dir=str(tmp_path), node=_minimal_node(node_type), inputs={},
        run_id=f"n-{node_type}", timeout=120.0,
    )
    assert isinstance(res, EngineRunResult)
    assert "__engine__" not in res.error_dict, (
        f"{node_type}: engine crashed in-sandbox: {res.error_dict.get('__engine__')!r}\n"
        f"stderr={getattr(res.sandbox, 'stderr', '')[-800:]}")
    if node_type == "CodeNode":
        assert res.final_outputs.get("node_1", {}).get("ok") == 1


@gvisor
def test_node_error_propagates_out_of_sandbox(tmp_path):
    """A CodeNode that raises → the node ERROR is carried OUT (no engine crash)."""
    bad = _minimal_node("CodeNode")
    bad["node_config"]["process_fn"] = "def process_fn(inputs):\n    raise Exception('kaboom')"
    res = get_sandbox_provider().run_node(
        run_dir=str(tmp_path), node=bad, inputs={}, run_id="n-err", timeout=120.0)
    assert "__engine__" not in res.error_dict
    assert "kaboom" in (res.error_dict.get("node_1", "") or "")


@pytest.fixture(autouse=True)
def select_gvisor_backend(monkeypatch):
    """These guarded tests exercise the explicitly selected gVisor backend."""
    from vibecanvas_api.config import config
    monkeypatch.setattr(config, "sandbox_runtime", "gvisor")
