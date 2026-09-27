# -*- coding: utf-8 -*-
"""Stable Workflow CLI batch-result serialization contract."""



def test_jsonl_record_preserves_success_and_error_shapes():
    from vibecanvas_api.services.agent_resources.batch_results import _jsonl_record

    success = _jsonl_record(
        2,
        {"prompt": "hello"},
        {
            "final_outputs": {"__end__": {"answer": "ok"}, "n1": {"x": 1}},
            "error_dict": {},
            "execution_time": 1.23456,
        },
    )
    assert success == {
        "index": 2,
        "status": "success",
        "input": {"prompt": "hello"},
        "output": {"answer": "ok"},
        "node_outputs": {"__end__": {"answer": "ok"}, "n1": {"x": 1}},
        "errors": {},
        "execution_time": 1.235,
    }

    failed = _jsonl_record(3, {"prompt": "bad"}, None, "worker stopped")
    assert failed["status"] == "error"
    assert failed["errors"] == {"_row_error": "worker stopped"}
    assert failed["output"] is None
