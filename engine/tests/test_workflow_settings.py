"""Workflow Settings (#484): __meta__.settings.timeouts → CodeNode/HTTP/workflow.

Verifies the engine reads ``__meta__.settings`` in ``Workflow.__init__`` and
applies the TIMEOUTS to the freshly-built node instances + the execution
timeout, with:
  * Code: settings > engine default; HTTP also supports per-node override
  * absent-settings back-compat that is byte-identical to today's behavior

CodeNode third-party libraries are not applied directly through settings:
they come from the content-addressed dependency overlay on the worker's
PYTHONPATH. Library settings keys (``code_requirements`` / ``code_libraries`` /
``code_index_url``) are accepted but ignored by ``_apply_settings``.
"""

from __future__ import annotations

from vibecanvas_engine import Workflow
from vibecanvas_engine.nodes.config import _CODE_TIMEOUT
from vibecanvas_engine.nodes.http_request import _HTTP_TIMEOUT


def _wf(settings: dict | None = None, *, code_node_config: dict | None = None,
        http_node_config: dict | None = None) -> dict:
    meta = {"workflow_id": "wf", "workflow_name": "t", "workflow_version": 1,
            "workflow_subversion": 0}
    if settings is not None:
        meta["settings"] = settings
    wf = {
        "__meta__": meta,
        "node_1": {
            "node_id": "node_1",
            "node_name": "__start__",
            "node_type": "StartNode",
            "node_description": "start",
            "input_fields": {},
            "output_fields": {},
            "node_config": {},
            "children": ["node_2"],
            "__attributes__": {"x": 0, "y": 0},
        },
        "node_2": {
            "node_id": "node_2",
            "node_name": "code",
            "node_type": "CodeNode",
            "node_description": "code",
            "input_fields": {},
            "output_fields": {},
            "node_config": {
                "programming_language": "python",
                "process_fn": "def process_fn(inputs):\n    return {}",
                **(code_node_config or {}),
            },
            "children": ["node_3"],
            "__attributes__": {"x": 100, "y": 0},
        },
        "node_3": {
            "node_id": "node_3",
            "node_name": "http",
            "node_type": "HTTPRequestNode",
            "node_description": "http",
            "input_fields": {},
            "output_fields": {},
            "node_config": {
                "method": "GET",
                "url": "https://example.com",
                **(http_node_config or {}),
            },
            "children": ["node_4"],
            "__attributes__": {"x": 200, "y": 0},
        },
        "node_4": {
            "node_id": "node_4",
            "node_name": "__end__",
            "node_type": "EndNode",
            "node_description": "end",
            "input_fields": {},
            "output_fields": {},
            "node_config": {},
            "children": [],
            "__attributes__": {"x": 300, "y": 0},
        },
    }
    return wf


# ---- back-compat: absent settings == today's behavior, byte-identical -------

def test_no_settings_is_byte_identical_to_engine_defaults():
    wf = Workflow(_wf(settings=None))
    code = wf.id2node["node_2"]
    http = wf.id2node["node_3"]

    # timeouts == engine hard-coded defaults
    assert code._default_timeout == _CODE_TIMEOUT
    assert http._default_timeout == _HTTP_TIMEOUT
    # workflow timeout == constructor default
    assert wf._execution_timeout == 3600.0


def test_empty_settings_dict_is_also_back_compat():
    wf = Workflow(_wf(settings={}))
    assert wf.id2node["node_2"]._default_timeout == _CODE_TIMEOUT
    assert wf._execution_timeout == 3600.0


# ---- library settings are accepted but ignored (overlay owns libs now) ------

def test_code_requirements_setting_is_accepted_and_ignored():
    # Libraries come from the overlay/PYTHONPATH, not direct injection. A
    # ``code_requirements`` declaration (+ optional index URL) must NOT crash
    # the build, and it must NOT mutate any per-instance engine state. Other
    # settings (timeouts) keep working alongside it.
    wf = Workflow(
        _wf(
            settings={
                "code_requirements": "pandas==2.2.0\nrequests",
                "code_index_url": "https://m/simple",
                "timeouts": {"code": 12},
            }
        )
    )
    code = wf.id2node["node_2"]
    # the timeouts branch still applies...
    assert code._default_timeout == 12.0
    # ...and the engine no longer carries a CodeNode lib-config attribute.
    assert not hasattr(code, "_libraries_config")


def test_legacy_code_libraries_setting_is_accepted_and_ignored():
    wf = Workflow(_wf(settings={"code_libraries": ["pandas", "not_a_lib"]}))
    code = wf.id2node["node_2"]
    assert not hasattr(code, "_libraries_config")
    # default timeout untouched (no timeouts in settings)
    assert code._default_timeout == _CODE_TIMEOUT


# ---- timeouts: settings override + node_config wins -------------------------

def test_settings_timeouts_override_defaults():
    wf = Workflow(_wf(settings={"timeouts": {"workflow": 120, "code": 10, "http": 5}}))
    assert wf._execution_timeout == 120.0
    assert wf.id2node["node_2"]._default_timeout == 10.0
    assert wf.id2node["node_3"]._default_timeout == 5.0


def test_legacy_code_timeout_does_not_override_settings(monkeypatch):
    from unittest.mock import Mock
    from vibecanvas_engine.nodes.code import CodeNode

    graph = _wf(settings={"timeouts": {"code": 10}}, code_node_config={"timeout": 2})
    assert "timeout" not in CodeNode.CONFIG_SCHEMA["properties"]
    assert CodeNode.check(graph["node_2"])["status"] == "success"
    code = Workflow(graph).id2node["node_2"]
    pool = Mock()
    pool.run.return_value = {"status": "success", "output": {}}
    monkeypatch.setattr(code, "_get_run_pool", lambda extra: pool)
    assert code({}, {}, {})["status"] == "success"
    assert pool.run.call_args.args[2] == 10
    assert graph["node_2"]["node_config"]["timeout"] == 2  # history unchanged


def test_per_node_config_timeout_beats_settings_for_http():
    wf = Workflow(
        _wf(settings={"timeouts": {"http": 5}}, http_node_config={"timeout": 77}),
    )
    http = wf.id2node["node_3"]
    assert http._default_timeout == 5.0
    used = float(http.node_config.get("timeout", http._default_timeout))
    assert used == 77.0


def test_code_default_reads_current_engine_configuration(monkeypatch):
    from vibecanvas_engine.nodes import config

    monkeypatch.setattr(config, "_CODE_TIMEOUT", 75.0)
    workflow = Workflow(_wf())
    assert workflow.id2node["node_2"]._default_timeout == 75.0
