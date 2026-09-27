"""CLI contract tests; no platform account or deployed sandbox required."""

import json

import pytest

from vibecanvas_api.flowork_cli import cli


@pytest.mark.parametrize("command,extra", [
    (["get"], []), (["update"], ["--name", "Renamed"]), (["delete"], []),
    (["download"], ["--major", "v2"]),
    (["upload"], ["--major", "v2", "--file", "graph.json"]),
    (["operation"], ["--major", "v2", "--node_remove", "node_2"]),
    (["check"], ["--major", "v2"]), (["version", "list"], []),
    (["version", "create"], ["--major", "v2"]),
    (["run"], ["--major", "v2"]),
    (["run-batch"], ["--major", "v2", "--input-file", "rows.jsonl"]),
])
@pytest.mark.parametrize("selector", [["--workflow_id", "wf"], ["--workflow-id", "wf"], ["wf"]])
def test_workflow_named_targets_and_legacy_positionals_select_same_id(command, extra, selector):
    parsed = cli.parser().parse_args(["workflow", *command, *selector, *extra])
    assert parsed.workflow_id == "wf"


@pytest.mark.parametrize("ids", [
    ["wf", "--workflow_id", "other"], ["--workflow-id", "wf", "wf"],
    ["--workflow_id", "wf", "--workflow-id", "other"],
    ["--workflow_id", "wf", "--workflow_id", "wf"],
])
def test_workflow_rejects_ambiguous_named_and_positional_targets(ids):
    with pytest.raises(cli.CliUsageError):
        cli.parser().parse_args(["workflow", "get", *ids])


@pytest.mark.parametrize("selector", [["--workflow_id", "wf"], ["wf"], []])
@pytest.mark.parametrize("destination", [["--output", "graph.json"], ["--output=graph.json"]])
def test_download_wrong_destination_reports_option_not_workflow_conflict(selector, destination, monkeypatch, capsys):
    monkeypatch.setattr(cli, "request", lambda *_a, **_kw: pytest.fail("invalid request dispatched"))
    assert cli.main(["workflow", "download", *selector, "--major", "v1", *destination], socket_path="socket") == 2
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "invalid_arguments"
    assert result["message"] == "Unrecognized option: --output"
    assert "--file PATH" in result["hint"]


def test_target_option_validation_preserves_negative_values_and_literal_options():
    parsed = cli.parser().parse_args(["workflow", "operation", "--workflow_id", "wf", "--major", "v1", "--node_update", "/node/config/temperature", "-1"])
    assert parsed.edits[-1][1][-1] == "-1"
    assert cli.parser().parse_args(["workflow", "update", "wf", "--description=--literal"]).description == "--literal"
    assert cli.parser().parse_args(["workflow", "get", "--", "--literal"]).workflow_id == "--literal"


@pytest.mark.parametrize("flag", ["--workflow_id", "--workflow-id"])
def test_config_and_layout_accept_the_same_named_workflow_flag(flag):
    assert cli.parser().parse_args(["config", "get", "--scope", "workflow", flag, "wf", "--major", "v1"]).workflow_id == "wf"
    assert cli.parser().parse_args(["workflow", "layout", flag, "wf", "--major", "v1"]).workflow_id == "wf"

def test_layout_dispatch_and_stdout(monkeypatch, capsys):
    reply = {"id": "wf", "version": "v2.sv5", "changed": True, "moved_nodes": 2, "message": "Layout saved as a new subversion."}
    def request(endpoint, arguments, **kwargs):
        assert kwargs["operation"] == "workflow.layout"
        assert arguments == {"workflow_id": "wf", "major": "v2"}
        return reply
    monkeypatch.setattr(cli, "request", request)
    assert cli.main(["workflow", "layout", "--workflow_id", "wf", "--major", "v2"], socket_path="socket") == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == reply and captured.err == ""


@pytest.mark.parametrize("args", [[], ["wf", "--major", "v2"], ["--workflow_id", "wf"],
                                  ["--workflow_id", "wf", "--major", "v0"]])
def test_layout_requires_named_target_and_valid_major(args, monkeypatch, capsys):
    monkeypatch.setattr(cli, "request", lambda *_a, **_kw: pytest.fail("invalid request dispatched"))
    assert cli.main(["workflow", "layout", *args], socket_path="socket") == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_arguments"


def test_batch_help_requires_a_live_terminal_not_detached_shell_exit(capsys):
    with pytest.raises(SystemExit) as exited:
        cli.parser().parse_args(["workflow", "run-batch", "--help"])
    assert exited.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "managed long-running terminal session" in help_text
    assert "check the original process/session handle" in help_text
    assert "do not automatically retry" in help_text

@pytest.mark.parametrize("command,expected", [
    (["--help"], ["result_unknown", "exit", "stdout"]),
    (["workflow", "--help"], ["stateless", "--major", "get-spec"]),
    (["workflow", "get-spec", "--help"], ["node_schema", "config get", "top-level object", "children", '"nodes"']),
    (["workflow", "create", "--help"], ["top-level object", '"nodes"', "not a complete executable graph"]),
    (["workflow", "upload", "--help"], ["top-level object", "separate edges array"]),
    (["workflow", "check", "--help"], ["top-level object", "reserved metadata"]),
    (["workflow", "get", "--help"], ["metadata", "global HEAD"]),
    (["workflow", "layout", "--help"], ["--workflow_id", "--major", "only if positions change", "moved_nodes", "render_preview"]),
    (["workflow", "operation", "--help"], ["successful prefix", "json:PATH", "~1", "saved"]),
    (["workflow", "run", "--help"], ["--node", "hard-kills", "durable", "result_unknown"]),
])
def test_on_demand_help_owns_usage_and_recovery_details(command, expected, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.parser().parse_args(command)
    assert exc.value.code == 0
    output = " ".join(capsys.readouterr().out.lower().split())
    for text in expected:
        assert text.lower() in output


def test_run_node_flag_is_optional_and_batch_rejects_it():
    assert cli.parser().parse_args(["workflow", "run", "wf", "--major", "v1"]).node is None
    parsed = cli.parser().parse_args(["workflow", "run", "wf", "--major", "v1", "--node", "node_2", "--file", "wf.json", "--inputs", "{}"])
    assert parsed.node == "node_2" and parsed.file == "wf.json"
    args = {"workflow_id": "wf", "major": "v1", "run_id": "a" * 32, "node": "node_2", "inputs": {}}
    assert cli.validate_arguments("workflow.run", args) == args
    with pytest.raises(cli.CliUsageError):
        cli.parser().parse_args(["workflow", "run-batch", "--input-file", "rows.json", "--node", "node_2"])
    with pytest.raises(cli.CliUsageError):
        cli.validate_arguments("workflow.run-batch", args)


@pytest.mark.parametrize("node", ["", " ", "__meta__", None, 42])
def test_run_rejects_invalid_node_selector(node):
    with pytest.raises(cli.CliUsageError):
        cli.validate_arguments("workflow.run", {"run_id": "a" * 32, "node": node})


@pytest.mark.parametrize("args,operation,arguments,reply", [
    (["get-spec", "--list-types"], "workflow.get-spec", {"list_types": True}, {"types": ["CodeNode"]}),
    (["get-spec", "--type", " CodeNode,PromptNode ", "--type", "CodeNode"],
     "workflow.get-spec", {"node_types": ["CodeNode", "PromptNode"]}, {"specs": []}),
])
def test_spec_and_disconnect_contract(args, operation, arguments, reply, monkeypatch, capsys):
    def request(endpoint, actual, **kwargs):
        assert actual == arguments and kwargs["operation"] == operation
        return reply
    monkeypatch.setattr(cli, "request", request)
    assert cli.main(["workflow", *args], socket_path="socket") == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == reply and captured.err == ""


@pytest.mark.parametrize("args", [
    ["disconnect", "wf"], ["disconnect", "--force"], ["get-spec"],
    ["get-spec", "--list-types", "--type", "CodeNode"],
    ["get-spec", "--type", "CodeNode,"], ["get-spec", "--type", " "],
])
def test_spec_and_disconnect_usage_errors(args, monkeypatch, capsys):
    monkeypatch.setattr(cli, "request", lambda *_a, **_kw: pytest.fail("must not dispatch"))
    assert cli.main(["workflow", *args], socket_path="socket") == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_arguments"


@pytest.mark.parametrize("args,code", [
    (["delete", "wf"], "result_unknown"), (["get-spec", "--list-types"], "request_timeout"),
])
def test_spec_and_disconnect_timeout_semantics(args, code, monkeypatch, capsys):
    def timeout(*_a, **_kw):
        raise TimeoutError()
    monkeypatch.setattr(cli, "request", timeout)
    assert cli.main(["workflow", *args], socket_path="socket") == 1
    assert json.loads(capsys.readouterr().out)["error"] == code


def test_download_without_file_outputs_only_graph(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    graph = {"__meta__": {"workflow_version": 1, "workflow_subversion": 8}, "error": {"node_type": "CodeNode"}}
    monkeypatch.setattr(cli, "request", lambda *_a, **_kw: {"id": "wf", "version": "v1.sv8", "node_count": 1, "workflow": graph})
    monkeypatch.setattr(cli, "save_download", lambda *_a, **_kw: pytest.fail("must not write files"))
    assert cli.main(["workflow", "download", "wf", "--major", "v1"], socket_path="socket") == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == graph and captured.err == ""
    assert list(tmp_path.iterdir()) == []


def test_download_overwrite_without_file_is_usage_error(monkeypatch, capsys):
    monkeypatch.setattr(cli, "request", lambda *_a, **_kw: pytest.fail("must not dispatch"))
    assert cli.main(["workflow", "download", "wf", "--major", "v1", "--overwrite"], socket_path="socket") == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_arguments"


@pytest.mark.parametrize("args,operation,arguments", [
    (["list", "wf"], "workflow.version.list", {"workflow_id": "wf"}),
    (["list", "--workflow_id", "wf"], "workflow.version.list", {"workflow_id": "wf"}),
    (["list", "--workflow-id", "wf"], "workflow.version.list", {"workflow_id": "wf"}),
    (["create", "wf", "--major", "v1"], "workflow.version.create", {"workflow_id": "wf", "major": "v1", "note": ""}),
    (["create", "wf", "--major", "v1", "--note", "Milestone"], "workflow.version.create", {"workflow_id": "wf", "major": "v1", "note": "Milestone"}),
])
def test_version_commands_send_only_approved_arguments(args, operation, arguments, monkeypatch, capsys):
    reply = {"id": "wf", "version": "v1.sv8"}
    def request(endpoint, actual, *, operation: str):
        assert operation == "workflow.version." + args[0]
        assert actual == arguments
        return reply
    monkeypatch.setattr(cli, "request", request)
    assert cli.main(["workflow", "version", *args], socket_path="socket") == 0
    assert json.loads(capsys.readouterr().out) == reply


@pytest.mark.parametrize("args", [[], ["set"], ["set", "1"], ["set", "v0"], ["set", "v1.sv8"], ["set", "v-1"], ["create", "--file", "x.json"], ["list", "--workflow-id", "other", "--workflow_id", "conflicting"]])
def test_version_usage_errors_do_not_dispatch(args, monkeypatch, capsys):
    monkeypatch.setattr(cli, "request", lambda *_a, **_kw: pytest.fail("must not dispatch"))
    assert cli.main(["workflow", "version", *args], socket_path="socket") == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_arguments"


@pytest.mark.parametrize("args,code", [(["list", "wf"], "request_timeout"), (["create", "wf", "--major", "v1"], "result_unknown")])
def test_version_timeouts_distinguish_reads_and_writes(args, code, monkeypatch, capsys):
    def timeout(*_a, **_kw):
        raise TimeoutError()
    monkeypatch.setattr(cli, "request", timeout)
    assert cli.main(["workflow", "version", *args], socket_path="socket") == 1
    assert json.loads(capsys.readouterr().out)["error"] == code


def test_check_defaults_to_saved_workflow_without_reading_a_file(monkeypatch, capsys):
    expected = {"valid": True, "id": "wf", "version": "v1.sv3", "node_count": 2}
    monkeypatch.setattr(cli, "read_workflow", lambda *_: pytest.fail("must not read a file"))

    def request(endpoint, arguments, *, operation):
        assert operation == "workflow.check" and arguments == {"workflow_id": "wf", "major": "v1"}
        return expected

    monkeypatch.setattr(cli, "request", request)
    assert cli.main(["workflow", "check", "wf", "--major", "v1"], socket_path="socket") == 0
    assert json.loads(capsys.readouterr().out) == expected


@pytest.mark.parametrize("reply,exit_code", [
    ({"valid": True, "node_count": 2}, 0),
    ({"valid": True, "node_count": 2, "warnings": [{"node_id": "start", "message": "Unused output."}]}, 0),
    ({"valid": False, "node_count": 2, "errors": [{"node_id": "start", "message": "Missing child."}]}, 1),
])
def test_check_file_output_and_exit_codes(reply, exit_code, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "workflow.json"
    original = '{"__meta__":{"workflow_id":"unconnected"}}'
    source.write_text(original)

    def request(endpoint, arguments, *, operation):
        assert operation == "workflow.check"
        assert arguments == {"workflow": json.loads(original)}
        return reply

    monkeypatch.setattr(cli, "request", request)
    assert cli.main(["workflow", "check", "--file", "workflow.json"], socket_path="socket") == exit_code
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {**reply, "path": str(source)}
    assert captured.err == ""
    assert source.read_text() == original


@pytest.mark.parametrize("contents,code", [(None, "file_not_found"), ('{"a":1,"a":2}', "invalid_workflow_file")])
def test_check_local_errors_do_not_dispatch(contents, code, monkeypatch, tmp_path, capsys):
    source = tmp_path / "workflow.json"
    if contents is not None:
        source.write_text(contents)
    monkeypatch.setattr(cli, "request", lambda *_a, **_kw: pytest.fail("must not contact Host"))
    assert cli.main(["workflow", "check", "--file", str(source)], socket_path="socket") == 2
    assert json.loads(capsys.readouterr().out)["error"] == code


@pytest.mark.parametrize("arguments", [{"workflow": None}, {"workflow_id": "other"}, {"file": "/host/private"}, {"fix": True}])
def test_check_rejects_unsupported_arguments(arguments):
    with pytest.raises(cli.CliUsageError):
        cli.validate_arguments("workflow.check", arguments)


def test_large_download_is_not_rejected(tmp_path):
    workflow = {"value": "x" * (9 * 1024 * 1024)}
    target = tmp_path / "large.json"
    result = cli.save_download({"id": "wf", "version": "v1.sv0", "node_count": 0, "workflow": workflow}, str(target), overwrite=False)
    assert result["path"] == str(target)
    assert json.loads(target.read_text()) == workflow


def test_download_is_atomic_and_requires_explicit_overwrite(monkeypatch, tmp_path, capsys):
    destination = tmp_path / "workflow.json"
    destination.write_text("existing edits")
    reply = {"id": "wf", "version": "v1.sv3", "node_count": 0, "workflow": {"__meta__": {"workflow_id": "wf"}}}
    monkeypatch.setattr(cli, "request", lambda *a, **kw: reply)
    command = ["workflow", "download", "wf", "--major", "v1", "--file", str(destination)]
    assert cli.main(command, socket_path="socket") == 1
    assert json.loads(capsys.readouterr().out)["error"] == "file_exists"
    assert destination.read_text() == "existing edits"
    assert cli.main([*command, "--overwrite"], socket_path="socket") == 0
    assert json.loads(destination.read_text()) == reply["workflow"]
    assert json.loads(capsys.readouterr().out) == {"id": "wf", "version": "v1.sv3", "node_count": 0, "path": str(destination)}
    assert list(tmp_path.glob(".flowork-download-*")) == []


def test_download_failed_publish_preserves_destination(monkeypatch, tmp_path):
    target = tmp_path / "workflow.json"
    target.write_text("local edits")
    def fail(*args):
        raise OSError("disk error")
    monkeypatch.setattr(cli.os, "replace", fail)
    result = cli.save_download({"id": "wf", "version": "v1.sv1", "node_count": 0, "workflow": {}}, str(target), overwrite=True)
    assert result["error"] == "download_failed"
    assert target.read_text() == "local edits"
    assert list(tmp_path.glob(".flowork-download-*")) == []


def test_upload_sends_graph_not_host_path_and_preserves_source(monkeypatch, tmp_path, capsys):
    source = tmp_path / "workflow.json"
    source.write_text('{"__meta__":{"workflow_id":"wf","workflow_subversion":0}}')
    original = source.read_bytes()
    def request(endpoint, arguments, *, operation):
        assert operation == "workflow.upload"
        assert arguments == {"workflow_id": "wf", "major": "v1", "workflow": json.loads(original), "note": "Update"}
        return {"id": "wf", "version": "v1.sv9", "node_count": 0}
    monkeypatch.setattr(cli, "request", request)
    assert cli.main(["workflow", "upload", "wf", "--major", "v1", "--file", str(source), "--note", "Update"], socket_path="socket") == 0
    assert json.loads(capsys.readouterr().out)["version"] == "v1.sv9"
    assert source.read_bytes() == original


def test_upload_unknown_result_does_not_encourage_retry(monkeypatch, tmp_path, capsys):
    source = tmp_path / "workflow.json"
    source.write_text('{}')
    def fail(*args, **kwargs):
        raise TimeoutError()
    monkeypatch.setattr(cli, "request", fail)
    assert cli.main(["workflow", "upload", "wf", "--major", "v1", "--file", str(source)], socket_path="socket") == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "result_unknown"
    assert "Do not automatically repeat" in result["hint"]


@pytest.mark.parametrize("arguments", [{"workflow": {}, "if_version": "v1.sv0"}, {"workflow": {}, "force": True}, {"workflow": {}, "workflow_id": "other"}, {"workflow": []}, {"workflow": {}, "note": 1}])
def test_upload_rejects_unapproved_host_arguments(arguments):
    with pytest.raises(cli.CliUsageError):
        cli.validate_arguments("workflow.upload", arguments)


def test_list_prints_business_data_only(monkeypatch, capsys):
    expected = {"workflows": [{"id": "wf_shared", "name": "Shared", "description": "", "version": "v1.sv2"}], "next_offset": 40}
    calls = []

    def request(endpoint, arguments, *, operation):
        assert operation == "workflow.list"
        calls.append((endpoint, arguments))
        return expected

    monkeypatch.setattr(cli, "request", request)
    assert cli.main(["workflow", "list", "--offset", "20"], socket_path="/test/socket") == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == expected
    assert captured.err == ""
    assert calls == [("/test/socket", {"limit": 20, "offset": 20})]


@pytest.mark.parametrize("args", [[], ["workflow"], ["workflow", "connect"],
    ["workflow", "list", "--limit", "0"], ["workflow", "list", "--limit", "101"],
    ["workflow", "list", "--offset", "-1"], ["workflow", "list", "--limit", "bad"],
    ["workflow", "list", "--offset", "2147483648"], ["workflow", "list", "--user", "other"],
])
def test_usage_errors_are_json_on_stdout(args, capsys):
    assert cli.main(args) == 2
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert set(result) == {"error", "message", "hint"}
    assert result["error"] == "invalid_arguments"
    assert captured.err == ""


def test_help_needs_no_connection(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["workflow", "list", "--help"])
    assert exc.value.code == 0
    assert "--offset" in capsys.readouterr().out


def test_missing_runtime_is_not_an_empty_list(monkeypatch, capsys):
    monkeypatch.delenv("FLOWORK_CLI_SOCKET", raising=False)
    assert cli.main(["workflow", "list"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "runtime_unavailable"


@pytest.mark.parametrize("exception,code", [
    (TimeoutError(), "request_timeout"), (OSError(), "runtime_unavailable"),
    (ValueError(), "invalid_response"),
])
def test_transport_errors_are_actionable_json(exception, code, monkeypatch, capsys):
    def fail(*_args, **_kwargs):
        raise exception

    monkeypatch.setattr(cli, "request", fail)
    assert cli.main(["workflow", "list"], socket_path="/test/socket") == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"] == code
    assert captured.err == ""


def test_create_import_reads_file_without_modifying_it(monkeypatch, tmp_path, capsys):
    source = tmp_path / "workflow.json"
    original = '{"__meta__":{"workflow_id":"old"},"node":{}}'
    source.write_text(original)
    calls = []

    def request(endpoint, arguments, *, operation):
        calls.append((operation, arguments))
        return {"id": "new"}

    monkeypatch.setattr(cli, "request", request)
    assert cli.main(["workflow", "create", "--name", " New ", "--tag", "one", "--tag", "one", "--file", str(source)], socket_path="socket") == 0
    assert calls == [("workflow.create", {"name": "New", "description": "", "tags": ["one"], "workflow": json.loads(original)})]
    assert source.read_text() == original
    assert "connected" not in json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("contents", ["[]", "not json", '{"a":1,"a":2}', '{"x":NaN}', '{"x":1e999}'])
def test_bad_import_is_rejected_before_transport(contents, monkeypatch, tmp_path, capsys):
    source = tmp_path / "bad.json"
    source.write_text(contents)
    monkeypatch.setattr(cli, "request", lambda *_args, **_kwargs: pytest.fail("must not contact Host"))
    assert cli.main(["workflow", "create", "--name", "New", "--file", str(source)], socket_path="socket") == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_arguments"


@pytest.mark.parametrize("args", [
    ["workflow", "create"], ["workflow", "create", "--name", " "],
    ["workflow", "create", "--name", "W", "--tag", " "],
    ["workflow", "connect", " "], ["workflow", "connect", "id", "--file", "x"],
])
def test_mutation_requires_valid_flags(args, capsys):
    assert cli.main(args) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_arguments"


@pytest.mark.parametrize("args", [["workflow", "create", "--name", "W"], ["workflow", "delete", "wf_1"]])
@pytest.mark.parametrize("exception", [TimeoutError(), OSError(), ValueError(), KeyboardInterrupt()])
def test_interrupted_mutation_never_promises_no_changes(args, exception, monkeypatch, capsys):
    def fail(*_args, **_kwargs):
        raise exception

    monkeypatch.setattr(cli, "request", fail)
    assert cli.main(args, socket_path="socket") == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "result_unknown"
    assert "Do not automatically" in result["hint"]


def test_large_files_are_allowed_but_input_must_be_regular(tmp_path):
    source = tmp_path / "large.json"
    value = {"value": "x" * (9 * 1024 * 1024)}
    source.write_text(json.dumps(value))
    assert cli.read_workflow(str(source)) == value
    assert cli.validate_arguments("workflow.check", {"workflow": value}) == {"workflow": value}
    with pytest.raises(cli.CliUsageError):
        cli.read_workflow(str(tmp_path))
    with pytest.raises(cli.CliUsageError):
        cli.read_workflow(str(tmp_path / "missing.json"))


@pytest.mark.parametrize("command", ["create", "update"])
def test_comma_and_repeated_tags_share_normalization(command, monkeypatch, capsys):
    calls = []

    def request(endpoint, arguments, *, operation):
        calls.append((operation, arguments))
        return {"id": "wf"}

    monkeypatch.setattr(cli, "request", request)
    assert cli.main(["workflow", command, *(["wf"] if command == "update" else []), "--name", "N", "--tag", " 数据,生产, 数据 ", "--tag", "示例,生产"], socket_path="socket") == 0
    assert calls[0][0] == f"workflow.{command}"
    assert calls[0][1]["tags"] == ["数据", "生产", "示例"]
    assert "connected" not in json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("flags,expected", [
    (["--name", " N "], {"name": "N"}),
    (["--description", ""], {"description": ""}),
    (["--clear-tags"], {"tags": []}),
    (["--tag", "a,b"], {"tags": ["a", "b"]}),
])
def test_update_sends_only_explicit_changes(flags, expected, monkeypatch, capsys):
    def request(endpoint, arguments, *, operation):
        assert operation == "workflow.update"
        assert arguments == {**expected, "workflow_id": "wf"}
        return {"id": "wf"}

    monkeypatch.setattr(cli, "request", request)
    assert cli.main(["workflow", "update", "wf", *flags], socket_path="socket") == 0
    assert json.loads(capsys.readouterr().out) == {"id": "wf"}


@pytest.mark.parametrize("args", [
    ["update"], ["update", "wf_123", "--name", " "], ["update", "--file", "w.json"],
    ["update", "--name", " "], ["update", "--clear-tags", "--tag", "a"],
    ["update", "--tag", ""], ["update", "--tag", "a,,b"], ["update", "--tag", "a, "],
    ["create", "--name", "N", "--tag", ",a"], ["status", "wf_123"], ["status", "--name", "N"],
])
def test_metadata_command_usage_errors_never_dispatch(args, monkeypatch, capsys):
    monkeypatch.setattr(cli, "request", lambda *_a, **_kw: pytest.fail("invalid input reached Host"))
    assert cli.main(["workflow", *args], socket_path="socket") == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_arguments"


@pytest.mark.parametrize("args", [["connect", "wf"], ["disconnect"], ["status"], ["version", "set", "v1"]])
def test_retired_state_commands_are_rejected(args, monkeypatch, capsys):
    monkeypatch.setattr(cli, "request", lambda *_a, **_kw: pytest.fail("must not dispatch"))
    assert cli.main(["workflow", *args], socket_path="socket") == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_arguments"


@pytest.mark.parametrize("command,expected", [("get", "request_timeout"), ("check", "request_timeout"), ("update", "result_unknown")])
def test_read_and_write_timeouts_are_distinct(command, expected, monkeypatch, capsys):
    def fail(*_a, **_kw):
        raise TimeoutError()

    monkeypatch.setattr(cli, "request", fail)
    args = ["workflow", command, "wf"] + (["--name", "N"] if command == "update" else ["--major", "v1"] if command == "check" else [])
    assert cli.main(args, socket_path="socket") == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == expected
    assert "connected" not in result
