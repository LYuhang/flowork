"""Ordered flags, strict JSON values and sandbox-only file materialization."""
import json

import pytest

from vibecanvas_api.flowork_cli import cli


def test_interleaved_repeated_flags_keep_global_order(monkeypatch, capsys):
    calls = []
    def request(endpoint, arguments, **kwargs):
        calls.append(arguments)
        assert kwargs["operation"] == "workflow.operation"
        return {"id": "wf", "version": "v1.sv1", "applied": 4, "total": 4}
    monkeypatch.setattr(cli, "request", request)
    assert cli.main(["workflow", "operation", "wf", "--major", "v1", "--node-add", '{"node_id":"a","node_type":"StartNode"}',
                     "--node-remove", "old", "--node-add", '{"node_id":"b","node_type":"EndNode"}',
                     "--edge-add", "a", "b"], socket_path="socket") == 0
    assert [item["op"] for item in calls[0]["operations"]] == ["node_add", "node_remove", "node_add", "edge_add"]
    assert json.loads(capsys.readouterr().out)["applied"] == 4


@pytest.mark.parametrize("value", [0, -1, 0.2, True, False, None, "", "123", "text\nnext", [], {"a": [1]}])
def test_update_value_keeps_json_type(value):
    arguments = cli.operation_arguments([("node_update", ["/a/node_config/value", json.dumps(value)])], "")
    actual = arguments["operations"][0]["value"]
    assert type(actual) is type(value) and actual == value


def test_text_and_json_files_materialize_values_and_preserve_newlines(tmp_path):
    text_file = tmp_path / "code.py"
    text_file.write_bytes(b"first\r\nsecond\n")
    json_file = tmp_path / "value.json"
    json_file.write_text('[1,true,null,"text"]', encoding="utf-8")
    node_file = tmp_path / "node.json"
    node_file.write_text('{"node_id":"a","node_type":"StartNode"}', encoding="utf-8")
    result = cli.operation_arguments([
        ("node_update_file", ["/a/node_config/code", str(text_file)]),
        ("node_update_file", ["/a/node_config/value", "json:" + str(json_file)]),
        ("node_add_file", [str(node_file)]),
    ], "Note")
    assert result["operations"][0]["value"] == "first\r\nsecond\n"
    assert result["operations"][1]["value"] == [1, True, None, "text"]
    assert result["operations"][2]["node"]["node_id"] == "a"
    assert str(tmp_path) not in json.dumps(result)


@pytest.mark.parametrize("bad_value", ["plain text", "NaN", "1e999", '{"a":1,"a":2}'])
def test_any_bad_json_prevents_dispatch(bad_value, monkeypatch, capsys):
    monkeypatch.setattr(cli, "request", lambda *_a, **_kw: pytest.fail("must not dispatch"))
    assert cli.main(["workflow", "operation", "wf", "--major", "v1", "--node-remove", "old", "--node-update", "/a/node_config/x", bad_value], socket_path="socket") == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_arguments"


def test_missing_later_file_prevents_every_edit(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "request", lambda *_a, **_kw: pytest.fail("must not dispatch"))
    assert cli.main(["workflow", "operation", "wf", "--major", "v1", "--node-remove", "old", "--node-update-file", "/a/node_config/code", str(tmp_path / "missing")], socket_path="socket") == 2
    assert json.loads(capsys.readouterr().out)["error"] == "file_not_found"


def test_atomic_failure_feedback_is_preserved_and_exits_nonzero(monkeypatch, capsys):
    result = {"id": "wf", "version": "v2.sv4", "applied": 0, "total": 3, "skipped": 1,
              "failed_index": 1, "error": "node_not_found", "message": "Node does not exist.",
              "results": [{"index": 0, "op": "node_remove", "status": "not_saved"},
                          {"index": 1, "op": "node_remove", "status": "failed"}]}
    monkeypatch.setattr(cli, "request", lambda *_a, **_kw: result)
    assert cli.main(["workflow", "operation", "wf", "--major", "v1", "--node-remove", "old"], socket_path="socket") == 1
    assert json.loads(capsys.readouterr().out) == {**result, "command_status": "failed", "event": "error"}


def test_operation_requires_at_least_one_flag():
    with pytest.raises(cli.CliUsageError):
        cli.validate_arguments("workflow.operation", {"operations": []})
