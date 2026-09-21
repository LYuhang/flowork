# -*- coding: utf-8 -*-
"""SubAgentNode — run a bounded tool-using agent as a workflow node."""
from __future__ import annotations

import asyncio
import glob as _glob
import jsonschema
import os
import re
import subprocess
from copy import deepcopy
from pathlib import Path

from ..register import node_registry
from ..utils import safe_call_with_args
from .base import BaseNode
from .prompt import PromptNode


class _LocalRunSession:
    """Minimal session adapter for agent fs/data/bash tools inside a workflow run."""

    def __init__(self, run_dir: str | None):
        self.run_dir = os.path.abspath(run_dir or os.getcwd())
        roots = [self.run_dir]
        for p in ("/run", "/mount", "/data", "/memory", "/logs"):
            if os.path.exists(p):
                roots.append(os.path.abspath(p))
        self.roots = tuple(dict.fromkeys(roots))

    def _resolve(self, path: str) -> str:
        raw = path or "."
        candidate = raw if os.path.isabs(raw) else os.path.join(self.run_dir, raw)
        resolved = os.path.abspath(candidate)
        if not any(resolved == root or resolved.startswith(root + os.sep) for root in self.roots):
            raise ValueError("path_outside_roots")
        return resolved

    async def read_file(self, path: str) -> dict:
        try:
            resolved = self._resolve(path)
            if not os.path.exists(resolved):
                return {"ok": False, "error": "not_found"}
            try:
                text = await asyncio.to_thread(Path(resolved).read_text, encoding="utf-8")
                return {"ok": True, "kind": "text", "content": text}
            except UnicodeDecodeError:
                return {"ok": True, "kind": "binary", "size": os.path.getsize(resolved)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def write_file(self, path: str, text: str) -> dict:
        try:
            resolved = self._resolve(path)
            os.makedirs(os.path.dirname(resolved), exist_ok=True)
            await asyncio.to_thread(Path(resolved).write_text, text or "", encoding="utf-8")
            return {"ok": True, "bytes": len((text or "").encode("utf-8"))}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def read_bytes(self, path: str) -> dict:
        try:
            resolved = self._resolve(path)
            if not os.path.exists(resolved):
                return {"ok": False, "error": "not_found"}
            data = await asyncio.to_thread(Path(resolved).read_bytes)
            return {"ok": True, "data": data}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def write_bytes(self, path: str, payload: bytes) -> dict:
        try:
            resolved = self._resolve(path)
            os.makedirs(os.path.dirname(resolved), exist_ok=True)
            await asyncio.to_thread(Path(resolved).write_bytes, payload or b"")
            return {"ok": True, "bytes": len(payload or b"")}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def grep(self, pattern: str, prefix: str = "/", glob_filter: str = "", context: int = 0) -> dict:
        try:
            base = self._resolve(prefix or ".")
            files = []
            if os.path.isfile(base):
                files = [base]
            else:
                glob_pat = glob_filter or "**/*"
                files = [p for p in _glob.glob(os.path.join(base, glob_pat), recursive=True)
                         if os.path.isfile(p)]
            try:
                rx = re.compile(pattern)
            except re.error:
                return {"ok": False, "error": "invalid_regex"}
            matches = []
            for fp in files[:500]:
                try:
                    lines = Path(fp).read_text(encoding="utf-8").splitlines()
                except Exception:
                    continue
                for i, line in enumerate(lines, start=1):
                    if rx.search(line):
                        matches.append(f"{fp}:{i}:{line}")
                        if len(matches) >= 200:
                            return {
                                "ok": True,
                                "matches": matches,
                                "match_count": len(matches),
                                "truncated": True,
                            }
            return {"ok": True, "matches": matches, "match_count": len(matches), "truncated": False}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def run_command(self, command: str, timeout_s: int = 60) -> dict:
        def _run():
            return subprocess.run(
                command,
                shell=True,
                cwd=self.run_dir,
                text=True,
                capture_output=True,
                timeout=max(1, int(timeout_s or 60)),
            )
        try:
            cp = await asyncio.to_thread(_run)
            return {
                "stdout": cp.stdout or "",
                "stderr": cp.stderr or "",
                "exit_code": cp.returncode,
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "stdout": exc.stdout or "",
                "stderr": (exc.stderr or "") + "\ncommand timed out",
                "exit_code": 124,
            }


@node_registry.register()
class SubAgentNode(BaseNode):
    """SubAgentNode — delegate a bounded, tool-using subtask inside a workflow."""

    REQUIRES_THREAD_BRIDGE: bool = True

    CONFIG_SCHEMA = {
        "type": "object",
        "required": ["task_template", "model_name"],
        "properties": {
            "task_template": {
                "type": "string",
                "description": (
                    "Delegated task template. Supports {{field}} interpolation."
                ),
            },
            "model_name": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "The exact public model_id key returned by "
                    "get_config(scope='global'). Fetch it in the current build "
                    "turn and copy one enabled key verbatim; never use the chat "
                    "Agent model, a provider model id, or a guessed name."
                ),
            },
            "max_iterations": {
                "type": "integer",
                "minimum": 1,
                "description": "Maximum tool-use iterations.",
            },
        },
        "additionalProperties": False,
    }

    AGENT_SPEC = {
        "summary": "Run a bounded tool-using worker and return structured output.",
        "when_to_use": (
            "Subtasks that need file/data/media/web/bash tools or several intermediate steps before producing output."
        ),
        "when_not_to_use": (
            "Use PromptNode for a single LLM call with no tool use. Use CodeNode for deterministic logic."
        ),
        "constraints": [
            "Mandatory model-discovery gate: in the current build turn, call get_config(scope='global') before writing any SubAgentNode, then copy one enabled models key exactly into model_name.",
            "Never use the chat Agent's runtime model id, a provider model id, or a guessed/familiar model name. If global config returns no model, do not create this node; ask the user to configure an API model first.",
        ],
        "config_guide": {
            "model_name": (
                "Exact enabled key from get_config(scope='global'). Fetch it in "
                "this build turn and copy it verbatim; never guess or substitute "
                "the Agent runtime model."
            ),
            "task_template": (
                "Readable task brief for the worker. Include what to inspect, what to do, and what to return."
            ),
        },
        "examples": [
            {
                "scenario": "Read a file and summarize it",
                "node_dict": {
                    "node_id": "node_4",
                    "node_name": "file_summarizer",
                    "node_type": "SubAgentNode",
                    "node_description": "Read a file with tools and produce a concise summary",
                    "input_fields": {
                        "file_path": {"type": "string", "value": "", "reference": "__start__.file_path"}
                    },
                    "output_fields": {
                        "summary": {"type": "string", "description": "Concise file summary"}
                    },
                    "node_config": {
                        "task_template": "# Task\nRead {{file_path}} and summarize its key points.\n\n# Instructions\nUse file tools to read the file, identify the important information, and produce a concise summary.\n\n# Output\nReturn the requested summary field.",
                        "model_name": "<model-name-from-get_config>",
                        "max_iterations": 10,
                    },
                    "children": ["node_5"],
                    "__attributes__": {"x": 300, "y": 0},
                },
            }
        ],
        "display": {
            "name": {"en": "SubAgentNode", "zh": "子智能体节点"},
            "description": {"en": "Run a tool-using sub-agent", "zh": "运行可调用工具的子智能体"},
            "icon": "agent",
            "category": {"en": "AI Inference", "zh": "AI 推理"},
        },
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    @staticmethod
    @safe_call_with_args(prefix="[SubAgentNode Check]: ")
    def check(node_dict: dict) -> bool:
        jsonschema.validate(instance=node_dict, schema=BaseNode.GENERAL_NODE_SCHEMA)
        specific_schema = deepcopy(BaseNode.GENERAL_NODE_SCHEMA)
        specific_schema["properties"]["node_config"] = SubAgentNode.CONFIG_SCHEMA
        jsonschema.validate(instance=node_dict, schema=specific_schema)
        jsonschema.validate(
            instance=node_dict,
            schema={
                "type": "object",
                "properties": {
                    "node_type": {"const": "SubAgentNode"},
                    "children": {"type": "array", "maxItems": 1},
                },
            },
        )
        assert node_dict.get("output_fields"), "SubAgentNode requires at least one output field."

    @staticmethod
    def _agent_cfg(model_name: str, extra: dict | None) -> dict:
        """Build the model descriptor for this in-sandbox worker.

        Workflow execution injects a short-lived Runtime Model Broker
        capability, not the provider credential. Unlike the host Chat Runtime,
        this worker constructs its model client directly inside the workflow
        sandbox, so the broker URL and capability must reach that constructor.
        Passing the entry through ``merge_agent_settings_override`` is wrong at
        this boundary: that helper deliberately strips connection fields for a
        later Chat-Runtime dispatch step which does not exist here.
        """
        injected = (extra or {}).get("llm_credentials") or {}
        if model_name in injected:
            entry = injected[model_name]
            provider = str(entry.get("provider") or "").strip()
            provider_model = str(entry.get("model_name") or "").strip()
            broker_url = str(entry.get("api_url") or "").strip()
            capability = str(entry.get("api_key") or "").strip()
            if not provider or not provider_model or not broker_url or not capability:
                raise RuntimeError(
                    f"Injected Runtime Model Broker configuration for "
                    f"'{model_name}' is incomplete."
                )
            agent_cfg = {
                "model": f"{provider}:{provider_model}",
                "base_url": broker_url,
                # This is a short-lived broker capability, never the provider
                # API key. The broker re-authorizes every upstream request.
                "api_key": capability,
            }
            if entry.get("model_context_tokens"):
                agent_cfg["model_context_tokens"] = int(
                    entry["model_context_tokens"]
                )
            if entry.get("timeout"):
                agent_cfg["timeout"] = int(entry["timeout"])
            return agent_cfg
        return {"model": model_name}

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are a bounded workflow sub-agent. Complete only the delegated "
            "task from the user message. Use tools when they are necessary to "
            "inspect or modify workspace files, and avoid unrelated exploration. "
            "When the task is complete, call set_output exactly once with the "
            "requested structured fields."
        )

    async def _call_async(self, inputs: dict, extra: dict | None) -> dict:
        from vibecanvas_api.services.platform_mcp.tool_runtime import AgentContext
        from vibecanvas_api.services.workflow_subagent import build_workflow_chat_model
        from vibecanvas_api.agents.tools.subagent.core import run_bounded_agent
        from vibecanvas_api.agents.tools.subagent.toolset import build_agent_subagent_tools

        cfg = self.node_config
        task, _images, _videos, _audios = PromptNode.format_prompt_template(
            cfg["task_template"], inputs, unpack_multimodal=False
        )
        agent_cfg = self._agent_cfg(cfg["model_name"], extra)
        model = build_workflow_chat_model(agent_cfg)
        ctx = AgentContext(
            wf_id=(extra or {}).get("run_id") or "",
            run_id=(extra or {}).get("run_id") or "",
            agent_cfg=agent_cfg,
            stop_event=(extra or {}).get("stop_event"),
        )
        ctx._attached_session = _LocalRunSession((extra or {}).get("run_dir"))

        result = await run_bounded_agent(
            model=model,
            tools=build_agent_subagent_tools(),
            system_prompt=self._system_prompt(),
            user_input=task,
            output_fields=self.output_fields,
            max_iterations=int(cfg.get("max_iterations") or 25),
            context=ctx,
            checkpointer=None,
            thread_id=None,
        )
        if result.status != "done":
            raise RuntimeError(result.error or f"SubAgentNode ended with status {result.status}")
        return result.output

    @safe_call_with_args(prefix="[SubAgentNode Call]: ")
    def __call__(self, inputs: dict, previous_outputs: dict, extra: dict = None) -> dict:
        stop_event = (extra or {}).get("stop_event")
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError("SubAgentNode cancelled before agent call.")
        # ``safe_call_with_args`` owns the standard node-result envelope. Return
        # only the declared fields here, exactly like PromptNode/CodeNode;
        # returning another envelope would persist
        # ``{status, output: {analysis_summary: ...}}`` as the node output and
        # break downstream references such as ``worker.analysis_summary``.
        return asyncio.run(self._call_async(inputs, extra or {}))
