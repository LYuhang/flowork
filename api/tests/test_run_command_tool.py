from unittest.mock import AsyncMock, MagicMock

import pytest

from vibecanvas_api.agents.tools.sandbox.bash import bash


@pytest.mark.asyncio
async def test_run_command_runs_shell_in_run_and_writes_back(monkeypatch):
    from vibecanvas_api.services.sandbox.manager import SandboxSession
    captured = {}

    async def fake_submit_fileop(op, *, timeout=30.0):
        captured["op"] = op
        captured["timeout"] = timeout
        return {"ok": True, "stdout": "hi\n", "stderr": "", "exit_code": 0}

    sess = SandboxSession(
        tenant_id="t",
        wf_id="w",
        run_dir="/tmp/x",
        overlay_dir="/tmp/o",
        provider=MagicMock(),
        base_binds=[],
    )
    monkeypatch.setattr(sess, "_submit_fileop", fake_submit_fileop)
    wb = AsyncMock()
    monkeypatch.setattr(sess, "writeback_vfs", wb)
    out = await sess.run_command("echo hi", timeout_s=30)
    assert out["exit_code"] == 0
    assert captured["op"] == {
        "op": "exec",
        "command": "echo hi",
        "cwd": "/",
        "timeout": 30.0,
    }
    assert captured["timeout"] == 40.0
    wb.assert_awaited_once()


@pytest.mark.asyncio
async def test_bash_reports_nonzero_exit_and_stderr(tmp_path):
    import json
    content, result = await bash("printf partial; printf problem >&2; exit 7", cwd=str(tmp_path))
    assert json.loads(content) == result
    assert result["exit_code"] == 7
    assert result["stdout"] == "partial"
    assert result["stderr"] == "problem"
    assert result["error"]["code"] == "command_failed"


@pytest.mark.asyncio
async def test_bash_quiet_success_is_not_empty(tmp_path):
    content, result = await bash("true", cwd=str(tmp_path))
    assert content
    assert result["status"] == "success"
    assert result["exit_code"] == 0


@pytest.mark.asyncio
async def test_bash_file_editing_search_and_independent_shell(tmp_path):
    import json
    import shlex
    script = """from pathlib import Path
import json
p = Path('input.json')
p.write_text(json.dumps({'name': 'old', 'count': 1}))
data = json.loads(p.read_text())
data['name'] = "quote' and double\\"quote"
data['count'] += 1
p.write_text(json.dumps(data))
"""
    _content, result = await bash("python3 -c " + shlex.quote(script), cwd=str(tmp_path))
    assert result["exit_code"] == 0, result
    _content, result = await bash("grep count input.json", cwd=str(tmp_path))
    assert json.loads(result["stdout"]) == {"name": "quote' and double\"quote", "count": 2}
    await bash("mkdir nested && cd nested && export ONLY_THIS_CALL=yes", cwd=str(tmp_path))
    _, result = await bash('pwd; printf "%s" "${ONLY_THIS_CALL-unset}"', cwd=str(tmp_path))
    assert result["stdout"] == str(tmp_path) + "\nunset"


@pytest.mark.asyncio
async def test_bash_large_output_is_complete_on_disk(tmp_path):
    from pathlib import Path
    _content, result = await bash("python3 -c \"import sys; print('x'*40000); print('y'*30000, file=sys.stderr)\"", cwd=str(tmp_path))
    assert result["stdout_truncated"] and result["stderr_truncated"]
    assert Path(result["stdout_file"]).read_text() == "x" * 40000 + "\n"
    assert Path(result["stderr_file"]).read_text() == "y" * 30000 + "\n"
    assert "Read it in portions" in result["hint"]


@pytest.mark.asyncio
async def test_bash_explicit_timeout_returns_partial_output(tmp_path):
    _content, result = await bash("echo started; sleep 30", timeout_s=0.15, cwd=str(tmp_path))
    assert result["error"]["code"] == "command_timeout"
    assert result["stdout"] == "started\n"
    assert result["exit_code"] != 0


@pytest.mark.asyncio
async def test_bash_cancellation_terminates_child_process(tmp_path):
    import asyncio
    task = asyncio.create_task(bash("sleep 30 & echo $! > child.pid; wait", cwd=str(tmp_path)))
    pidfile = tmp_path / "child.pid"
    for _ in range(200):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.01)
    assert pidfile.exists()
    pid = int(pidfile.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # A just-killed child can briefly be an unreaped zombie, never running.
    from pathlib import Path
    stat = Path(f"/proc/{pid}/stat")
    assert not stat.exists() or stat.read_text().split()[2] == "Z"
    assert not list(tmp_path.glob(".subagent-*.log"))


@pytest.mark.asyncio
async def test_bash_concurrent_runs_keep_their_cwd(tmp_path):
    import asyncio
    directories = [tmp_path / "a", tmp_path / "b"]
    for path in directories:
        path.mkdir()
    results = await asyncio.gather(*(bash("pwd", cwd=str(path)) for path in directories))
    assert [result[1]["stdout"].strip() for result in results] == [str(p) for p in directories]


@pytest.mark.asyncio
async def test_bash_invalid_working_directory_is_actionable(tmp_path):
    _, result = await bash("true", cwd=str(tmp_path / "absent"))
    assert result["error"]["code"] == "run_failed"
    assert "run directory" in result["error"]["message"]
