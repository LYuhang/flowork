"""CLI syntax/output tests, not a substitute for side-panel Agent acceptance."""

import json
from pathlib import Path
import re

import pytest

from vibecanvas_api.flowork_cli import browser_cli, cli


def minimal(name):
    result = {}
    for key, spec in browser_cli.COMMANDS[name][1].items():
        if not spec["required"]:
            continue
        value = spec["choices"][0] if spec["choices"] else {
            str: "example", float: 1.0, int: 1, bool: True,
        }[spec["kind"]]
        result[key] = [value] if spec["repeat"] else value
    if name in browser_cli.REQUIRED_TARGET or name == "highlight":
        result["ref"] = "e15"
    if name == "wait-for":
        result["text"] = "Ready"
    if name == "eval":
        result["expression"] = "document.title"
    if name == "run-code":
        result["code"] = "async page => await page.title()"
    if name == "drop":
        result["file"] = ["/data/file.bin"]
    if name == "goto":
        result["url"] = "https://example.com"
    return result


@pytest.mark.parametrize("name", browser_cli.COMMANDS)
def test_every_command_has_help_and_validated_named_arguments(name, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.parser().parse_args(["browser", name, "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert f"flowork-cli browser {name}" in help_text
    # argparse may wrap a hyphenated word (e.g. tab-\nseparated). Check that
    # every description character survives formatting, independent of wrapping.
    assert "".join(browser_cli.COMMANDS[name][0].split()) in "".join(help_text.split())
    arguments = minimal(name)
    argv = ["browser", name]
    for key, value in arguments.items():
        for item in value if isinstance(value, list) else [value]:
            argv.extend(["--" + key.replace("_", "-"), str(item)])
    parsed = cli.parser().parse_args(argv)
    values = {key: getattr(parsed, key) for key in browser_cli.COMMANDS[name][1] if getattr(parsed, key) is not None}
    assert cli.validate_arguments("browser." + name, values) == browser_cli.validate("browser." + name, arguments)


@pytest.mark.parametrize("operation,arguments", [
    ("click", {"tab_id": "t"}),
    ("click", {"tab_id": "t", "ref": "e1", "locator": "button"}),
    ("click", {"tab_id": "t", "ref": "e1", "timeout": float("nan")}),
    ("click", {"tab_id": "t", "ref": "e1", "timeout": -1}),
    ("click", {"tab_id": "t", "ref": "e1", "force": True}),
    ("click", {"tab_id": "t", "ref": "e1", "modifiers": "Shift"}),
    ("snapshot", {"tab_id": "t", "boxes": "false"}),
    ("snapshot", {"tab_id": "t", "depth": True}),
    ("screenshot", {"tab_id": "t", "ref": "e1", "full_page": True}),
    ("wait-for", {"tab_id": "t", "ref": "e1", "text": "Ready"}),
    ("wait-for", {"tab_id": "t", "text": ""}),
    ("eval", {"tab_id": "t"}),
    ("eval", {"tab_id": "t", "expression": "1", "file": "/data/x.js"}),
    ("run-code", {"tab_id": "t", "code": " ", "file": "/data/x.js"}),
    ("select", {"tab_id": "t", "ref": "e1", "value": []}),
    ("drop", {"tab_id": "t", "ref": "e1"}),
    ("drop", {"tab_id": "t", "ref": "e1", "data": ["not-mime"]}),
    ("drop", {"tab_id": "t", "ref": "e1", "data": ["text/plain=x"], "file": ["x"]}),
    ("upload", {"tab_id": "t", "file": "/data/file.bin"}),
    ("upload", {"tab_id": "t", "file": []}),
    ("download", {"tab_id": "t", "ref": "e1"}),
    ("cookie-export", {"tab_id": "t", "file": "/data/c", "format": "plain"}),
    ("cookie-export", {"tab_id": "t", "file": "/data/cookies.json"}),
    ("cookie-export", {"tab_id": "t", "file": "../cookies.json"}),
    ("goto", {"tab_id": "t", "url": "javascript:alert(1)"}),
    ("goto", {"tab_id": "t", "url": "file:///etc/passwd"}),
    ("tab-list", {"tab_id": "t"}),
    ("tab-info", {"tab_id": 2}),
    ("tab-info", {"tab_id": "t\x00"}),
    ("highlight", {"tab_id": "t"}),
])
def test_invalid_arguments_rejected_before_execution(operation, arguments):
    with pytest.raises(cli.CliUsageError):
        cli.validate_arguments("browser." + operation, arguments)


def test_no_current_tab_or_abbreviated_or_positional_target():
    for argv in (["connect"], ["disconnect"], ["tab-select", "0"],
                 ["click", "e1"], ["click", "--tab", "t", "--ref", "e1"]):
        with pytest.raises(cli.CliUsageError):
            cli.parser().parse_args(["browser", *argv])


def test_repeatable_values_and_empty_fill_are_preserved():
    assert browser_cli.validate("browser.select", {"tab_id": "t", "ref": "e1", "value": ["", "blue"]})["value"] == ["", "blue"]
    assert browser_cli.validate("browser.fill", {"tab_id": "t", "ref": "e1", "text": ""})["text"] == ""
    assert browser_cli.validate("browser.highlight", {"tab_id": "t", "hide": True})["hide"] is True
    assert browser_cli.validate("browser.wait-for", {"tab_id": "t", "text": "Done", "timeout": 0})["timeout"] == 0


@pytest.mark.parametrize("name,phrases", [
    ("type", ("current caret/selection", "press --key End")),
    ("wait-for", ("exact visible-text match", "exact:false")),
    ("eval", ("Nested undefined", "explicit null", "poll the same session")),
    ("run-code", ("setInputFiles", "transfer sandbox bytes", "filechooser", "null for missing fields", "poll the same session")),
    ("download", ("Ending the Agent turn cancels", "does not mean success")),
    ("cookie-export", ("{url, cookies: [...]}", "not a top-level array", "Inspect only metadata/counts")),
])
def test_help_explains_real_agent_trial_pitfalls(name, phrases, capsys):
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["browser", name, "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    for phrase in phrases:
        assert phrase in help_text


def test_cli_error_is_json_stdout_not_traceback(capsys, monkeypatch):
    monkeypatch.delenv("FLOWORK_CLI_SOCKET", raising=False)
    assert cli.main(["browser", "tab-list"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["error"] == "runtime_unavailable"
    assert cli.main(["browser", "click", "--tab-id", "t"]) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["error"] == "invalid_arguments"


def test_relative_files_resolve_in_the_cli_sandbox(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    seen = []
    def request(endpoint, arguments, *, operation):
        seen.append((endpoint, operation, arguments))
        return {"status": "succeeded", "message": "Files uploaded."}
    monkeypatch.setattr(cli, "request", request)
    assert cli.main(["browser", "upload", "--tab-id", "t", "--file", "a.bin", "--file", "b.bin"], socket_path="socket") == 0
    assert seen[0][2]["file"] == [str(tmp_path / "a.bin"), str(tmp_path / "b.bin")]
    assert json.loads(capsys.readouterr().out)["status"] == "succeeded"


@pytest.mark.parametrize("name,expected", [("click", "unknown"), ("eval", "unknown"), ("download", "unknown"), ("snapshot", "failed")])
def test_transport_failure_does_not_claim_mutation_failed(name, expected, monkeypatch, capsys):
    def interrupted(*args, **kwargs):
        raise ConnectionError("lost")
    monkeypatch.setattr(cli, "request", interrupted)
    args = minimal(name)
    argv = ["browser", name]
    for key, value in args.items():
        argv.extend(["--" + key.replace("_", "-"), str(value)])
    assert cli.main(argv, socket_path="socket") == 1
    assert json.loads(capsys.readouterr().out)["status"] == expected


def test_mutating_script_operations_never_classified_as_reads():
    for name in ("eval", "run-code", "download", "upload", "cookie-export", "tab-new"):
        assert "browser." + name in cli.WRITE_OPERATIONS
    assert browser_cli.OPERATIONS <= cli.OPERATIONS


def test_command_inventory_matches_node_dispatch_and_cli_help(capsys):
    """Catch migration omissions; this does not prove browser behavior."""
    root = Path(__file__).resolve().parents[3]
    actions = (root / "api/playwright-runtime/browser-actions.cjs").read_text()
    handlers = set(re.findall(r'case "([a-z-]+)":', actions))
    handlers.update(re.findall(r'if \(name === "([a-z-]+)"\) return', actions))
    assert handlers == set(browser_cli.COMMANDS)

    with pytest.raises(SystemExit) as exc:
        cli.parser().parse_args(["browser", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    for name in browser_cli.COMMANDS:
        assert re.search(r"(?<![a-z-])" + re.escape(name) + r"(?![a-z-])", help_text)

    runtime = (root / "api/playwright-runtime/browser-runtime.cjs").read_text()
    reads = re.search(r"const READS = new Set\((\[[^;]+\])\);", runtime)
    assert reads is not None
    assert set(json.loads(reads.group(1))) == browser_cli.READ_COMMANDS


@pytest.mark.parametrize("name", ["snapshot", "find", "screenshot", "tab-new", "mousemove", "dialog-dismiss"])
@pytest.mark.parametrize("timeout", [0, 0.1, 60])
def test_observation_commands_accept_explicit_timeout(name, timeout):
    arguments = minimal(name)
    assert browser_cli.validate("browser." + name, arguments)["timeout"] == 30
    arguments["timeout"] = timeout
    assert browser_cli.validate("browser." + name, arguments)["timeout"] == timeout
    for invalid in [-1, float("inf"), float("nan")]:
        arguments["timeout"] = invalid
        with pytest.raises(ValueError):
            browser_cli.validate("browser." + name, arguments)


@pytest.mark.parametrize("name,expected", [
    ("tab-new", "Whole tab-new deadline"),
    ("download", "Download-start wait"),
    ("snapshot", "Not a whole CLI duration limit"),
])
def test_timeout_help_matches_command_scope(name, expected, capsys):
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["browser", name, "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert expected in help_text
    if name in {"tab-new", "download"}:
        assert "Not a whole CLI duration limit" not in help_text


def test_url_download_target_is_exclusive_and_not_navigation():
    args = {"tab_id": "tab_test", "url": "https://cdn.example.com/a.png?sig=123", "file": "/data/a.png"}
    assert browser_cli.validate("browser.download", args)["url"] == args["url"]
    for extra in [{"ref": "r1"}, {"locator": "img"}]:
        with pytest.raises(ValueError, match="Exactly one"):
            browser_cli.validate("browser.download", {**args, **extra})
    for url in ["file:///etc/passwd", "blob:https://example.com/id", "data:image/png,x", "https://", "https://u:p@example.com/a"]:
        with pytest.raises(ValueError, match="HTTP"):
            browser_cli.validate("browser.download", {**args, "url": url})
