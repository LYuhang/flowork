# -*- coding: utf-8 -*-
"""CodeNode runs through a lean per-run ``CodeWorkerPool``.

The old in-process ``PythonSandbox`` jail (AST import-ban, builtins whitelist,
jailed ``open``) is GONE — gVisor is the isolation boundary. These tests pin the
new contract:

  * CodeNode computes the right output (success → user dict in result["output"]).
  * Normal ``import`` works (``import math`` is a NORMAL Python import now).
  * Real ``open`` works against a real path (NO jail).
  * A node ``timeout`` kills the worker → a NORMAL node error_dict entry.
  * The worker env hides host secrets: ``os.environ.get("DATABASE_URL")`` → None.
  * REQUIRES_THREAD_BRIDGE is set; CodeNode flows through the unified dispatch
    (no ``call_async`` special-case).
"""

from __future__ import annotations

import os


from vibecanvas_engine.nodes.code import CodeNode


def _node(process_fn: str, timeout: float | None = None) -> CodeNode:
    n = CodeNode.__new__(CodeNode)
    n._default_timeout = timeout if timeout is not None else 60.0
    cfg = {"programming_language": "python", "process_fn": process_fn}
    n.node_config = cfg
    return n


def _run(node: CodeNode, inputs: dict, extra: dict | None = None) -> dict:
    # __call__ is SYNC now (runs off-loop via the thread bridge in the engine);
    # call it directly in the test thread.
    return node(inputs, {}, extra or {})


def test_codenode_computes_output(tmp_path):
    code = "def process_fn(inputs):\n    return {'answer': inputs['a'] + inputs['b']}"
    res = _run(_node(code), {"a": 40, "b": 2}, {"run_dir": str(tmp_path)})
    assert res["status"] == "success", res.get("error_message")
    assert res["output"] == {"answer": 42}


def test_codenode_normal_import_works(tmp_path):
    # No jail: a plain `import` is allowed Python now.
    code = (
        "import math\n"
        "def process_fn(inputs):\n"
        "    return {'r': math.sqrt(inputs['x'])}"
    )
    res = _run(_node(code), {"x": 16.0}, {"run_dir": str(tmp_path)})
    assert res["status"] == "success", res.get("error_message")
    assert res["output"] == {"r": 4.0}


def test_codenode_real_open_works(tmp_path):
    # Real open() against a real absolute path under the run dir — no jail mapping.
    target = os.path.join(str(tmp_path), "x.txt")
    code = (
        "def process_fn(inputs):\n"
        f"    with open({target!r}, 'w') as f:\n"
        "        f.write('hi')\n"
        f"    with open({target!r}) as f:\n"
        "        return {'r': f.read()}"
    )
    res = _run(_node(code), {}, {"run_dir": str(tmp_path)})
    assert res["status"] == "success", res.get("error_message")
    assert res["output"] == {"r": "hi"}
    assert open(target).read() == "hi"


def test_codenode_timeout_becomes_node_error(tmp_path):
    code = (
        "import time\n"
        "def process_fn(inputs):\n"
        "    time.sleep(30)\n"
        "    return {}"
    )
    res = _run(_node(code, timeout=1.0), {}, {"run_dir": str(tmp_path)})
    assert res["status"] == "error"
    assert "timed out" in res["error_message"].lower()


def test_codenode_worker_env_hides_db_secret(tmp_path, monkeypatch):
    # The worker env is EXPLICIT-MINIMAL — host secrets must NOT leak in.
    monkeypatch.setenv("DATABASE_URL", "postgres://secret@host/db")
    code = (
        "import os\n"
        "def process_fn(inputs):\n"
        "    return {'db': os.environ.get('DATABASE_URL')}"
    )
    res = _run(_node(code), {}, {"run_dir": str(tmp_path)})
    assert res["status"] == "success", res.get("error_message")
    assert res["output"] == {"db": None}


def test_codenode_requires_thread_bridge():
    assert getattr(CodeNode, "REQUIRES_THREAD_BRIDGE", False) is True


def test_codenode_user_error_becomes_node_error(tmp_path):
    code = "def process_fn(inputs):\n    raise ValueError('boom')"
    res = _run(_node(code), {}, {"run_dir": str(tmp_path)})
    assert res["status"] == "error"
    assert "boom" in res["error_message"]


def test_codenode_pool_created_in_extra(tmp_path):
    # The run's pool is lazily created in extra and reused across CodeNode calls.
    extra: dict = {"run_dir": str(tmp_path)}
    code = "def process_fn(inputs):\n    return {'ok': True}"
    _run(_node(code), {}, extra)
    pool = extra.get("_code_pool")
    assert pool is not None
    # second call reuses the same pool object
    _run(_node(code), {}, extra)
    assert extra["_code_pool"] is pool
    pool.close()
