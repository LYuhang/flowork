"""Document CLI, trusted evidence, snapshot and all-page rendering contracts."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from reportlab.pdfgen import canvas

from vibecanvas_api.flowork_cli import cli
from vibecanvas_api.document_runtime import rendering
from vibecanvas_api.services.agent_runtime.cli_gateway import CliGateway
from vibecanvas_api.services.agent_runtime.codex import (
    _ToolCompletionEvidence, _record_document_cli_completion,
    _document_visual_coverage, _missing_command_completion_tools,
)


def pdf_file(path, pages=1):
    pdf = canvas.Canvas(str(path))
    for number in range(pages):
        pdf.drawString(72, 760, f"Acceptance page {number + 1}")
        pdf.showPage()
    pdf.save()


@pytest.mark.parametrize("arguments", [
    ["review"], ["render", "--file", "x", "--dpi", "95"],
    ["render", "--file", "x", "--pages", "0"],
    ["render", "--file", "x", "--pages", "3-1"],
    ["render", "--file", "x", "--pages", "1,,2"],
    ["render", "--file", "x", "--max_pages", "20"],
])
def test_invalid_arguments(arguments, capsys):
    assert cli.main(["document", *arguments]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_arguments"


@pytest.mark.parametrize("action", ["review", "render"])
def test_leaf_help(action, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["document", action, "--help"])
    assert exc.value.code == 0
    assert "source_hash" in capsys.readouterr().out


@pytest.fixture
def fake_rasterizer(monkeypatch):
    monkeypatch.setattr(rendering, "_required_command", lambda *_: "pdftoppm")
    calls = []

    def run(arguments, **kwargs):
        assert "timeout" not in kwargs
        calls.append(arguments)
        Path(arguments[-1]).with_suffix(".png").write_bytes(b"image" + arguments[arguments.index("-f") + 1].encode())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(rendering.subprocess, "run", run)
    return calls


def test_all_pages_no_twenty_page_cap(tmp_path, fake_rasterizer):
    source = tmp_path / "long.pdf"
    pdf_file(source, 23)
    progress = []
    result = rendering.render_document(str(source), output_dir=str(tmp_path / "output"), progress=progress.append)
    assert result["status"] == "succeeded"
    assert result["complete"] and result["rendered_pages"] == result["total_pages"] == 23
    assert len(fake_rasterizer) == 23
    assert len([row for row in progress if "page" in row]) == 23


def test_selected_pages_and_exclusive_output(tmp_path, fake_rasterizer):
    source = tmp_path / "report.pdf"
    pdf_file(source, 5)
    output = tmp_path / "images"
    result = rendering.render_document(str(source), pages="1-3,2,5", output_dir=str(output))
    assert [item["page"] for item in result["images"]] == [1, 2, 3, 5]
    assert result["complete"] is False
    assert rendering.render_document(str(source), output_dir=str(output))["error"] == "invalid_arguments"
    assert rendering.render_document(str(source), pages="6", output_dir=str(tmp_path / "other"))["error"] == "invalid_arguments"
    assert not (tmp_path / "other").exists()


def test_snapshot_and_partial_failure(tmp_path, fake_rasterizer, monkeypatch):
    source = tmp_path / "report.pdf"
    pdf_file(source, 3)
    original = source.read_bytes()
    real = rendering._run

    def run(arguments, **kwargs):
        assert Path(arguments[-2]).read_bytes() == original
        if arguments[arguments.index("-f") + 1] == "2":
            raise RuntimeError("Renderer failed on page two.")
        real(arguments, **kwargs)

    monkeypatch.setattr(rendering, "_run", run)
    result = rendering.render_document(str(source), output_dir=str(tmp_path / "images"), progress=lambda _: source.write_bytes(b"changed"))
    assert result["source_hash"] == "sha256:" + hashlib.sha256(original).hexdigest()
    assert result["status"] == "failed" and result["rendered_pages"] == 1
    assert Path(result["images"][0]["file"]).exists()


def test_coverage_gate_and_revision_invalidation(tmp_path):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"source")
    digest = hashlib.sha256(b"source").hexdigest()
    request = SimpleNamespace(command_context=SimpleNamespace(activated_this_turn=["document"]))
    evidence = {}
    assert "flowork-cli document review" in _missing_command_completion_tools(request, evidence)
    review = {"file": str(source), "source_hash": "sha256:" + digest, "status": "passed"}
    _record_document_cli_completion(evidence, "document.review", {}, review)
    for page in (1, 2):
        image = tmp_path / f"page-{page}.png"
        image.write_bytes(bytes([page]))
        image_hash = hashlib.sha256(image.read_bytes()).hexdigest()
        result = {**review, "status": "succeeded", "total_pages": 2, "images": [{"page": page, "file": str(image)}], "_image_hashes": {str(image): image_hash}}
        _record_document_cli_completion(evidence, "document.render", {}, result)
        assert _document_visual_coverage(evidence, str(source), digest)[1] is False
        evidence.setdefault("view_image", []).append(_ToolCompletionEvidence({}, str(image), image_hash))
    assert _document_visual_coverage(evidence, str(source), digest) == (True, True)
    evidence["render_preview"] = [_ToolCompletionEvidence({}, str(source), digest)]
    assert _missing_command_completion_tools(request, evidence) == ()
    source.write_bytes(b"edited")
    assert "flowork-cli document review" in _missing_command_completion_tools(request, evidence)
    assert "flowork-cli document render" in _missing_command_completion_tools(request, evidence)


def test_forged_or_failed_evidence_does_not_pass(tmp_path):
    source = tmp_path / "report.txt"
    source.write_text("x")
    digest = hashlib.sha256(b"x").hexdigest()
    request = SimpleNamespace(command_context=SimpleNamespace(activated_this_turn=["document"]))
    forged = _ToolCompletionEvidence({"status": "passed"}, str(source), digest)
    assert "flowork-cli document review" in _missing_command_completion_tools(request, {"review_document": [forged], "shell": [forged]})
    evidence = {}
    _record_document_cli_completion(evidence, "document.review", {}, {"file": str(source), "source_hash": digest, "status": "failed"})
    assert "flowork-cli document review" in _missing_command_completion_tools(request, evidence)


@pytest.mark.parametrize("valid,format_name", [(True, "xlsx"), (True, "docx"), (False, "xlsx")])
def test_document_worker_reports_review_scope_and_next_step(monkeypatch, capsys, valid, format_name):
    from io import StringIO
    from vibecanvas_api.document_runtime import worker

    monkeypatch.setattr(worker.signal, "signal", lambda *_: None)
    monkeypatch.setattr(worker.sys, "stdin", StringIO(json.dumps({
        "operation": "document.review", "arguments": {"file": "/data/test." + format_name},
    })))
    monkeypatch.setattr(worker, "review_document", lambda path: {
        "path": path, "valid": valid, "format": format_name,
        "errors": [] if valid else ["Invalid structure"],
    })
    worker.main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == ("passed" if valid else "failed")
    assert "Structural checks" in result["message"]
    if valid:
        assert "not validated" in result["message"]
        assert "inspect every image" in result["hint"]
        assert ("metric labels, units and comparison periods" in result["hint"]) == (format_name == "xlsx")
        assert ("summaries and charts" in result["hint"]) == (format_name == "xlsx")
    else:
        assert "rerun document review" in result["hint"]


@pytest.mark.asyncio
async def test_real_gateway_records_worker_not_shell(tmp_path):
    source = tmp_path / "brief.md"
    source.write_text("# Brief\nDocument CLI test.")
    gateway = CliGateway()
    recorded = []

    async def unexpected(*_):
        raise AssertionError("Local document operation must not reach Host.")

    env = await gateway.activate(unexpected, document_complete=lambda *args: recorded.append(args))
    try:
        result = await asyncio.to_thread(cli.request, env["FLOWORK_CLI_SOCKET"], {"file": str(source)}, operation="document.review")
        assert result["status"] == "passed"
        assert "Structural checks passed" in result["message"]
        assert "not validated" in result["message"]
        assert "independently check calculations" in result["hint"]
        assert len(recorded) == 1
        assert recorded[0][2]["source_hash"] == "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
        assert (Path(env["FLOWORK_CLI_SOCKET"]).parent / "document_cli.py").is_file()
        process = await asyncio.create_subprocess_exec(
            "/bin/bash", "-lc", "flowork-cli document review --help",
            env={**os.environ, **env, "PATH": "/usr/bin:/bin"},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        assert b"source_hash" in stdout
    finally:
        await gateway.close()
    assert gateway._document_complete is None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,file", [("document.review", "/data/any.md"), ("diagram.render", "/data/any.drawio")])
async def test_gateway_disconnect_terminates_document_worker(monkeypatch, operation, file):
    from vibecanvas_api.services.agent_runtime import cli_gateway as module
    original = asyncio.create_subprocess_exec
    processes = []

    async def launch(*args, **kwargs):
        process = await original(args[0], "-c", "import sys,time; sys.stdin.read(); time.sleep(120)", **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", launch)
    gateway = CliGateway()
    recorded = []

    async def unexpected(*_):
        raise AssertionError("Host must not execute sandbox document reads.")

    env = await gateway.activate(unexpected, document_complete=lambda *args: recorded.append(args))
    try:
        _, writer = await asyncio.open_unix_connection(env["FLOWORK_CLI_SOCKET"])
        writer.write(json.dumps({"operation": operation, "arguments": {"file": file}}).encode() + b"\n")
        await writer.drain()
        for _ in range(100):
            if processes:
                break
            await asyncio.sleep(0.01)
        assert processes
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(processes[0].wait(), timeout=4)
        assert not recorded
        with pytest.raises(ProcessLookupError):
            os.kill(processes[0].pid, 0)
    finally:
        await gateway.close()
