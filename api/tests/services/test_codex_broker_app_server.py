from __future__ import annotations

import os

import pytest

from vibecanvas_api.services.agent_runtime.codex_app_server import CodexAppServer
from vibecanvas_api.services.codex_cli import resolve_codex_executable


@pytest.mark.asyncio
async def test_real_codex_app_server_starts_with_broker_provider_and_no_auth_cache(
    tmp_path,
):
    executable = resolve_codex_executable()
    if executable is None:
        pytest.skip("Codex CLI is not installed")
    home = tmp_path / ".codex"
    home.mkdir(mode=0o700)
    token_file = tmp_path / "model-capability"
    token_file.write_text("turn-scoped-capability", encoding="utf-8")
    token_file.chmod(0o600)
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    env["CODEX_HOME"] = str(home)
    env["CODEX_SQLITE_HOME"] = str(home)
    client = CodexAppServer(executable=executable, env=env, cwd=str(tmp_path))
    try:
        await client.start()
        result = await client.request(
            "thread/start",
            {
                "model": "gpt-broker-test",
                "modelProvider": "vibecanvas_runtime_model",
                "cwd": str(tmp_path),
                "approvalPolicy": "never",
                "sandbox": "danger-full-access",
                "config": {
                    "model_provider": "vibecanvas_runtime_model",
                    "model_providers": {
                        "vibecanvas_runtime_model": {
                            "name": "Flowork Runtime Model Broker",
                            "base_url": "http://127.0.0.1:9/api/internal/runtime-model/v1",
                            "wire_api": "responses",
                            "namespace_tools": False,
                            "auth": {
                                "command": "/bin/cat",
                                "args": [str(token_file)],
                                "timeout_ms": 1_000,
                                "refresh_interval_ms": 1,
                            },
                        }
                    },
                },
            },
            timeout_s=30.0,
        )
        assert result["thread"]["modelProvider"] == "vibecanvas_runtime_model"
        assert result["thread"]["id"]
        assert not (home / "auth.json").exists()
    finally:
        await client.close()
