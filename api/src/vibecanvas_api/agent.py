"""LangGraph-based agent for Flowork workflow editing.

Provides ``run_agent_turn`` — the single entry point called by
``handle_agent_chat`` in ``app.py``.  Internally manages an agent
singleton (rebuilt when config changes) and streams signals back to
the frontend via the Flowork signal protocol.

Tools live in ``demo/tools/`` (one file per tool).  Cross-tool state
is passed via ``context_schema`` (AgentContext) so tools can read the
current workflow and write back pending vibes without module globals.

Module-level singletons (explained):

- ``_agent`` / ``_agent_fingerprint``: Cached compiled graph.  Rebuilt
  only when the agent config dict changes (model, base_url, etc.).

Conversation history is managed by LangGraph's checkpointer, which is
passed in from the caller.  On the first turn we send [system, user];
on subsequent turns we send only [user] — the checkpointer restores
the previous state automatically.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import traceback
import uuid
from copy import deepcopy
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, TypedDict

import httpx
from langchain.agents import create_agent
from langchain.agents.middleware import ContextEditingMiddleware
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from pydantic import BaseModel, Field, PrivateAttr

from vibecanvas_api.config import AgentConfig
from vibecanvas_api.services.sandbox.manager import get_sandbox_manager
from vibecanvas_api.observability.agent import record_llm_usage
from vibecanvas_api.observability.metrics import AGENT_TURNS_TOTAL, AGENT_TURN_DURATION
from vibecanvas_api.observability.otel import trace as _trace
from vibecanvas_api.storage.sync_session import current_sync_tenant_id
from vibecanvas_api.agents.prompts.compose import build_system_prompt
from vibecanvas_api.agents.tools import build_tools
from vibecanvas_api.agents.middleware.context_prefix_strip import ContextPrefixStripEdit
from vibecanvas_api.agents.middleware.lifecycle_policy import LifecyclePolicyEdit
from vibecanvas_api.agents.middleware.token_record import TokenRecordMiddleware
from vibecanvas_api.agents.middleware.serial_tools import SerialToolExecutionMiddleware
from vibecanvas_api.agents.middleware.runtime_resilience import (
    RuntimeResilienceMiddleware,
)
from vibecanvas_api.agents.middleware.user_approval import (
    UserApprovalMiddleware,
)
from vibecanvas_api.services.agent_runtime.approval import (
    is_pre_tool_approval_candidate,
)
from vibecanvas_api.services.agent_runtime.tool_invocation import (
    finish_tool_invocation,
    start_tool_invocation,
)
from vibecanvas_api.agents.middleware.image_injection import ImageInjectionMiddleware
from vibecanvas_api.agents.middleware.workflow_refresh import WorkflowRefreshMiddleware
from vibecanvas_api.agents.middleware.s2a_compaction import (
    S2A_OVERSIZE_TOKENS_DEFAULT,
    S2aCompactor,
    VfsBodyReader,
    VfsS2aCache,
)
from vibecanvas_api.agents.middleware.s2b_compaction import (
    S2bCompactor,
    VfsS2bCache,
)
from vibecanvas_api.agents.chatml import to_chatml_message
from vibecanvas_api.agents.prefix import build_file_attachment_prefix

# Tracer for the per-turn ``agent.turn`` span.
_agent_tracer = _trace.get_tracer("vibecanvas.agent")


class EmptyModelResponseError(RuntimeError):
    """The provider completed a model step without text or tool calls."""


def _message_content_to_text(content: Any) -> str:
    """Render LangChain message content into SSE-safe text.

    Provider and MCP adapters may return OpenAI-style content blocks as a
    list/dict instead of a plain string. The chat stream protocol still expects
    text, so normalize only at the UI boundary and leave the checkpointed
    message object unchanged.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False)
    except Exception:
        return str(content)


def _is_internal_chat_content(content: Any) -> bool:
    if not isinstance(content, str):
        return False
    text = content.lstrip()
    return (
        text.startswith("<system-reminder>")
        or text.startswith("<conversation summary")
        or text.startswith("<compressed-history-summary")
        or text.startswith("Compacted Conversation Summary")
        or "<hard-context>" in text
    )


def _sanitize_visible_assistant_text(content: str) -> str:
    """Remove platform-only hidden-thinking markers from user-visible text."""
    text = content or ""
    while True:
        start = text.find("<think_never_used_")
        if start < 0:
            break
        end = text.find("</think_never_used_", start)
        if end < 0:
            break
        close = text.find(">", end)
        if close < 0:
            break
        text = text[:start] + text[close + 1:]
    # Some providers have been observed emitting only the closing marker. In
    # that case the preceding line is hidden reasoning, not assistant output.
    lines = []
    for line in text.splitlines():
        if "</think_never_used_" in line or "<think_never_used_" in line:
            continue
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Context schema — injected into every tool via ToolRuntime.context
# ---------------------------------------------------------------------------

class AgentContext(BaseModel):
    """Per-invocation ephemeral data shared between tools and the bridge.

    ``workflow``: the canonical workflow dict at the start of the turn.
    Tools read it; ``update_canvas`` mutates it after a successful import.

    ``pending_vibe``: written by canvas-mutating workflow tools after a
    successful commit so the streaming bridge can yield VIBE_ACTION/META_SYNC.

    ``repo``: WorkflowRepo for version control operations.
    ``username`` / ``wf_id`` / ``chat_id``: identity for ref scoping.

    """
    workflow: dict = {}
    pending_vibe: Optional[dict] = None
    # Set True by any tool that mutates ``workflow`` without persisting itself
    # directly. Cleared by new_version. Used by run_agent_turn's
    # finally-block to auto-commit unpersisted edits at turn end so changes
    # survive a process restart.
    workflow_dirty: bool = False
    repo: Any = None

    vfs: Any = None
    # RE-1 (A0): the run-scope ephemeral binary VFS tier (a PostgresVfsRunStore
    # facade). No production agent path wires this yet — the /run tools reach it
    # via this field + run_id (injected in tests until RE-6 wires the live run).
    vfs_run: Any = None
    username: str = ""
    wf_id: str = ""
    tenant_id: Optional[str] = None
    chat_id: str = ""
    thread_id: str = ""
    turn_id: str = ""
    # Host-only authorization state reconstructed for Platform MCP requests.
    # These values are excluded from model/checkpoint serialization and never
    # cross into the sandbox as agent-visible context.
    authorization_client: Any = Field(default=None, exclude=True)
    authorization_session_id: str = Field(default="", exclude=True)
    authorization_membership_id: str = Field(default="", exclude=True)
    authorization_membership_role: str = Field(default="", exclude=True)
    authorization_membership_status: str = Field(default="", exclude=True)
    authorization_session_generation: int = Field(default=0, exclude=True)
    authorization_generation: str = Field(default="", exclude=True)
    authorization_authentication_strength: str = Field(default="", exclude=True)
    authorization_session_audience: str = Field(default="web", exclude=True)
    authorization_privileged_access_request_id: str = Field(
        default="", exclude=True,
    )
    authorization_privileged_resource_type: str = Field(default="", exclude=True)
    authorization_privileged_resource_id: str = Field(default="", exclude=True)
    authorization_privileged_actions: tuple[str, ...] = Field(
        default=(), exclude=True,
    )
    authorization_privileged_expires_at: Any = Field(default=None, exclude=True)
    runtime_session_id: str = Field(default="", exclude=True)
    surface: str = "chat"
    approval_mode: str = "agent"
    # Identifies the control-plane boundary that constructed this ephemeral
    # context. Platform MCP tools run in the API process while the Agent Runtime
    # owns the resident sandbox; they must not close that sandbox mid-Turn.
    runtime_location: str = "host"
    # Turn-local decisions received from the host Runtime control plane.
    # Middleware only enforces this map at the final execution boundary.
    tool_approval_decisions: Dict[str, str] = Field(default_factory=dict)
    # Runtime-private callback used by LangChain background controls. It sends
    # submit/list/cancel operations to the backend control plane; the callable
    # itself is never checkpointed.
    background_job_submitter: Any = Field(default=None, exclude=True)
    # Stable refs/snapshots for rich UI created by prior turns. Full definitions
    # remain in PostgreSQL; this channel makes the checkpoint itself sufficient
    # to identify the exact durable artifact state without rewriting messages.
    interactive_artifact_refs: Dict[str, dict] = Field(default_factory=dict)
    active_commands: List[str] = Field(default_factory=list)
    available_commands: List[str] = Field(default_factory=list)
    current_workflow_id: Optional[str] = None
    run_id: str = ""
    # Runtime-neutral product-fact inventory for this Turn. Model-native
    # history decisions are appended only to Debug and never checkpointed as
    # durable truth.
    context_manifest: Dict[str, Any] = Field(default_factory=dict)
    runtime_mcp_catalog: List[dict] = Field(default_factory=list)

    # The active model config is inherited by bounded subagents. The stop event
    # lets the runtime cancel long-running model and tool operations.
    agent_cfg: Any = None
    stop_event: Any = None

    # Immutable Skill descriptors supplied by the backend Runtime protocol.
    # The model reads only the mounted root_path with normal filesystem tools;
    # no product database access occurs from inside the sandbox.
    runtime_skill_catalog: List[dict] = Field(default_factory=list)

    # Turn-local copy of the backend-owned structured task list. Each item:
    # {"id": int, "text": str, "status": "pending"|"in_progress"|"done"}.
    # Tool updates are projected to PostgreSQL; this context is not checkpointed.
    todo_items: List[dict] = Field(default_factory=list)

    # read_images: image(s) the agent asked to SEE, staged by the read_images tool
    # and drained by ImageInjectionMiddleware.before_model into one HumanMessage of
    # multimodal image blocks (a tool result can't portably carry images). Each
    # entry: {"mime", "b64", "path", "tokens"} (tokens = pixel-based, w*h/(32*32)).
    pending_images: list = Field(default_factory=list)

    # Plan C C2: a SubAgent's structured result, staged by the TERMINAL set_output
    # tool and read back by SubAgentCore at completion (coerced to output_fields).
    staged_subagent_output: Optional[dict] = None

    # G3 turn-end writeback: the resident session resolved by ``sandbox_session``
    # THIS turn (memoized). Stays None on a pure chat turn that booted no sandbox,
    # so the turn-boundary writeback fires ONLY when a session was actually
    # attached — a chat turn never boots a sandbox just to write back. PrivateAttr
    # (not a model field) so it's excluded from serialization / the schema the
    # tool runtime injects.
    # INVARIANT: the attach-this-turn semantics REQUIRE one AgentContext per turn
    # (lifetime == one turn). _run_agent_turn_inner builds a fresh AgentContext
    # each turn, so this field never leaks a prior turn's session; reusing an
    # AgentContext across turns would make a later pure-chat turn wrongly schedule.
    _attached_session: Any = PrivateAttr(default=None)

    model_config = {"arbitrary_types_allowed": True}

    async def sandbox_session(self):
        """Lazily resolve the resident per-workflow sandbox session.

        Pull-only: nothing calls this on a plain chat turn, so no sandbox is
        booted unless a tool actually needs to run code. The SandboxManager
        owns the per-(tenant, wf) residency; we just hand it the identity.

        Memoizes the resolved session on ``self._attached_session`` so the
        turn-boundary writeback (G3) can tell a session was attached this turn
        (and reuse the same instance across calls within the turn).
        """
        sandbox_wf_id = self.wf_id
        if not self.tenant_id or not sandbox_wf_id:
            raise ValueError(
                "AgentContext.sandbox_session() requires both tenant_id and "
                f"wf_id (got tenant_id={self.tenant_id!r}, wf_id={sandbox_wf_id!r})"
            )
        if self._attached_session is not None:
            return self._attached_session
        if os.environ.get("VIBECANVAS_AGENT_RUNTIME_IN_SANDBOX") == "1":
            from vibecanvas_api.services.agent_runtime.local_session import (
                LocalAgentRuntimeSession,
            )

            self._attached_session = LocalAgentRuntimeSession()
            return self._attached_session
        # ``username`` is the user_id (routes set username=auth.user_id) → the pip
        # overlay is scoped per-USER (shared across this user's workflows / chats).
        manager = get_sandbox_manager()
        if self.runtime_location == "platform_mcp":
            # A Platform MCP call is issued by an already-running Runtime Turn.
            # It must borrow that Turn's exact resident session.  Calling
            # ``get_session(..., expose_runtime=False)`` here would rebuild a
            # Codex session (whose Runtime-private mount is enabled) under the
            # same cache key.  The retired instance could then perform a stale
            # turn-end VFS writeback after a semantic host-side commit.
            session = await manager.get_loaded_session(
                self.tenant_id,
                sandbox_wf_id,
            )
            if session is None:
                raise RuntimeError(
                    "Platform MCP requires the active Runtime sandbox session"
                )
        else:
            session = await manager.get_session(
                self.tenant_id, sandbox_wf_id, self.username or None,
                expose_run=True)
        self._attached_session = session
        return session


class _AgentStateExt(TypedDict, total=False):
    """Extra state channels that persist across turns via the LangGraph checkpointer.

    Unlike ``context_schema`` (AgentContext), fields here are stored in the graph's
    state channels and are saved/restored by the checkpointer on every turn.
    """
    current_workflow_id: Optional[str]
    message_form_overrides: dict[str, dict]
    interactive_artifact_refs: Dict[str, dict]


async def _read_checkpoint_channel(checkpointer: Any, thread_id: str, key: str, default: Any) -> Any:
    """Read a single state channel value from the previous checkpoint.

    Reads directly from the checkpointer before the agent is built, so there is
    no chicken-and-egg dependency on an existing agent object.
    """
    if checkpointer is None:
        return default
    cfg = {"configurable": {"thread_id": thread_id}}
    try:
        if hasattr(checkpointer, "aget_tuple"):
            tup = await checkpointer.aget_tuple(cfg)
        else:
            tup = await asyncio.to_thread(checkpointer.get_tuple, cfg)
        if tup is None:
            return default
        return tup.checkpoint.get("channel_values", {}).get(key, default)
    except Exception as exc:
        print(f"⚠️  [agent] checkpoint read for {key!r} failed: {exc}")
        return default


# ---------------------------------------------------------------------------
# Agent factory — rebuilt every turn for tenant- and chat-scoped tools
# ---------------------------------------------------------------------------
#
# Pre-MCP, this module cached one compiled graph keyed by a JSON fingerprint
# of ``agent_cfg`` (model / base_url / etc.).  MCP T4 invalidates that cache
# entirely:
#
# 1. Tools are now tenant-scoped — two tenants on the same process must NOT
#    share an agent whose tool list was loaded under the other tenant.
# 2. Loaded MCP proxy tools are chat-scoped. Their names come from checkpointed
#    state, while the live MCP sessions are owned by the chat sandbox rather
#    than by the compiled graph.
#
# Result: ``_get_or_create_agent`` is async (it awaits the loader) and
# rebuilds on every call.  The name keeps "or_create" so the public surface
# stays the same; downstream tests that import it still pass.


def _build_chat_model(agent_cfg):
    if isinstance(agent_cfg, AgentConfig):
        kwargs = agent_cfg.to_init_kwargs()
        model_str = agent_cfg.model
    else:
        model_str = agent_cfg.get("model", "google_genai:gemini-2.5-flash")
        kwargs = {}
        if agent_cfg.get("base_url"):
            kwargs["base_url"] = agent_cfg["base_url"]
        if agent_cfg.get("api_key"):
            kwargs["api_key"] = agent_cfg["api_key"]
        if agent_cfg.get("temperature") is not None:
            kwargs["temperature"] = float(agent_cfg["temperature"])
        if agent_cfg.get("max_tokens") is not None:
            kwargs["max_tokens"] = int(agent_cfg["max_tokens"])
        if agent_cfg.get("timeout") is not None:
            kwargs["timeout"] = int(agent_cfg["timeout"])
        if agent_cfg.get("max_retries") is not None:
            kwargs["max_retries"] = int(agent_cfg["max_retries"])
        if agent_cfg.get("extra_body"):
            kwargs["extra_body"] = agent_cfg["extra_body"]
        if agent_cfg.get("use_responses_api") is not None:
            kwargs["use_responses_api"] = bool(agent_cfg["use_responses_api"])
        if agent_cfg.get("reasoning"):
            kwargs["reasoning"] = agent_cfg["reasoning"]
        if agent_cfg.get("output_version"):
            kwargs["output_version"] = agent_cfg["output_version"]

    # OpenAI-family hardening: pass our OWN plain httpx clients so
    # langchain-openai does NOT construct its ``_SyncHttpxClientWrapper`` — a
    # subclass that some openai/httpx import layouts reject at runtime with
    # ``TypeError: Invalid http_client argument; Expected an instance of
    # httpx.Client but got _SyncHttpxClientWrapper`` (the wrapper's class
    # identity ends up not matching the ``httpx.Client`` openai checks against).
    # A vanilla ``httpx.Client`` built here uses THIS process's single httpx, so
    # openai's ``isinstance`` check always passes. Vanilla clients are fine for
    # OpenAI-compatible endpoints (no special headers/keepalive needed).
    provider = (model_str.split(":", 1)[0] if ":" in model_str else "").lower()
    if kwargs and provider in ("openai", "azure_openai") and "http_client" not in kwargs:
        _t = kwargs.get("timeout")
        _timeout = httpx.Timeout(float(_t)) if _t else httpx.Timeout(60.0)
        # Optional outbound proxy (per-credential). Absent → trust_env default
        # (byte-identical to today). When set, route this provider's requests
        # through the user-specified HTTP/HTTPS proxy.
        _proxy = (
            agent_cfg.get("proxy")
            if isinstance(agent_cfg, dict)
            else getattr(agent_cfg, "proxy", None)
        )
        _client_kwargs = {"timeout": _timeout}
        if _proxy:
            _client_kwargs["proxy"] = _proxy
        async def _model_request_started(request: httpx.Request) -> None:
            request.extensions["vibecanvas_started_at"] = time.perf_counter()
            print("⏱️  [model_http_timing] phase=request_dispatched")

        async def _model_response_headers(response: httpx.Response) -> None:
            started = response.request.extensions.get("vibecanvas_started_at")
            elapsed_ms = (
                int((time.perf_counter() - started) * 1000)
                if isinstance(started, (int, float))
                else -1
            )
            print(
                "⏱️  [model_http_timing] "
                f"phase=response_headers elapsed_ms={elapsed_ms} "
                f"status_code={response.status_code}"
            )

        kwargs["http_client"] = httpx.Client(**_client_kwargs)
        kwargs["http_async_client"] = httpx.AsyncClient(
            **_client_kwargs,
            event_hooks={
                "request": [_model_request_started],
                "response": [_model_response_headers],
            },
        )

    # Force SERIAL tool calls (OpenAI-family): the model must emit ONE tool call
    # at a time. Browser + other dependent tools can't be parallelised — the
    # result of one drives the next (e.g. query → click → read), and interleaving
    # over a single browser transport races. `parallel_tool_calls` is a per-request
    # param valid whenever tools are bound (the agent always binds its tools), so
    # it rides `model_kwargs`. Other providers don't accept it → left untouched.
    if provider in ("openai", "azure_openai"):
        mk = dict(kwargs.get("model_kwargs") or {})
        mk.setdefault("parallel_tool_calls", False)
        kwargs["model_kwargs"] = mk

    if kwargs:
        return init_chat_model(model_str, **kwargs)
    return model_str


def _format_agent_init_error(exc: Exception) -> str:
    raw = str(exc)
    lower = raw.lower()
    if (
        "missing credentials" in lower
        or "openai_api_key" in lower
        or "openai_admin_key" in lower
        or "api_key" in lower and "environment variable" in lower
    ):
        return (
            "Agent init error: No model credential is configured. "
            "Connect a compatible model account or API source in Settings, "
            "then select its model in Chat."
        )
    return f"Agent init error: {raw}"


def _cancelled_tool_artifact(tool_name: str, content: str) -> dict:
    return {
        "schema_version": 1,
        "status": "error",
        "error": {
            "code": "user_cancelled",
            "message": content,
            "type": "UserCancelled",
            "info": {"interrupted": True},
        },
        "content": content,
        "content_abstract": content,
        "ref": f"tool://{tool_name or 'tool'}/user_cancelled",
        "artifact": {
            "kind": "tool_error",
            "errors": [{"code": "user_cancelled", "message": content, "info": {"interrupted": True}}],
        },
        "payload": {"kind": "none"},
        "meta": {
            "tool": tool_name or None,
            "content_type": "text/plain",
            "stale_on_reread": False,
            "interrupted": True,
        },
    }


def _cancelled_tool_messages(ai_msg: AIMessage) -> list[ToolMessage]:
    content = "Tool call cancelled by user."
    out: list[ToolMessage] = []
    for call in getattr(ai_msg, "tool_calls", None) or []:
        if not isinstance(call, dict):
            continue
        tcid = call.get("id")
        if not isinstance(tcid, str) or not tcid:
            continue
        name = call.get("name") if isinstance(call.get("name"), str) else "tool"
        out.append(
            ToolMessage(
                content=content,
                tool_call_id=tcid,
                name=name,
                artifact=_cancelled_tool_artifact(name, content),
                response_metadata={"interrupted": True, "finish_reason": "cancelled"},
            )
        )
    return out


async def _safe_update_state(agent, config: dict, update: dict, *, as_node: str | None = None) -> None:
    async_update = getattr(agent, "aupdate_state", None)
    if callable(async_update):
        try:
            kwargs = {"as_node": as_node} if as_node else {}
            await async_update(config, update, **kwargs)
        except TypeError:
            await async_update(config, update)
        return
    try:
        kwargs = {"as_node": as_node} if as_node else {}
        await asyncio.to_thread(agent.update_state, config, update, **kwargs)
    except TypeError:
        await asyncio.to_thread(agent.update_state, config, update)


def _json_dict(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        return {str(k): str(v) for k, v in value.items()}


async def _reconcile_interactive_artifact_refs_before_model(
    agent: Any,
    config: dict,
    *,
    checkpoint_refs: dict[str, dict],
    durable_refs: dict[str, dict],
) -> dict[str, dict]:
    """Make the checkpoint reflect durable interaction decisions before inference.

    The API decision endpoint updates only the Runtime-neutral PostgreSQL rows.
    On the next turn this adapter projects that durable result into its own
    checkpointer *before* the new HumanMessage/model call. Otherwise the model
    can run from a state that the following resume cannot reproduce exactly.
    Historical messages are never rewritten, so their prefix-cache bytes stay
    stable; only the dedicated reference channel is updated.
    """
    merged = {**_json_dict(checkpoint_refs), **_json_dict(durable_refs)}
    if merged == _json_dict(checkpoint_refs):
        return merged
    try:
        await _safe_update_state(
            agent,
            config,
            {"interactive_artifact_refs": merged},
        )
    except Exception as exc:
        # Fail closed: PostgreSQL remains authoritative for the frontend, but an
        # Agent turn must not start from a state that its checkpointer cannot
        # reproduce after a worker switch or reconnect.
        raise RuntimeError(
            "could not reconcile durable interactive state into the Agent checkpoint"
        ) from exc
    return merged


def _build_s2a_summarize_fn(agent_cfg):
    """Build a synchronous ``summarize_fn(prompt) -> str`` for S2a.

    Builds the BYO-LLM model via the SAME ``_build_chat_model`` path the agent
    uses, but with the model id resolved through ``resolve_compaction_model()``
    (the reserved ``agent.compaction.model`` slot → falls back to the agent's own
    model). The creds/base_url/proxy are reused from ``agent_cfg`` — so at the
    compaction seam the model + run creds are reachable (they ride ``agent_cfg``,
    which is in the ``_build_context_edits`` closure). Returns None when no usable
    cfg → S2a stays inert (fail-soft → deterministic abstract).

    Runs on the synchronous ``ContextEdit.apply`` seam, so it uses the model's
    sync ``.invoke`` (a one-shot, non-streaming summarizer call).
    """
    if agent_cfg is None:
        return None
    try:
        if isinstance(agent_cfg, AgentConfig):
            model_str = agent_cfg.resolve_compaction_model()
            base_cfg = agent_cfg.to_init_kwargs()
            if getattr(agent_cfg, "proxy", None):
                base_cfg["proxy"] = agent_cfg.proxy
        else:
            base = dict(agent_cfg)
            comp = (base.get("compaction") or {}).get("model")
            model_str = comp or base.get("model", "")
            base_cfg = {k: v for k, v in base.items()
                        if k in ("api_key", "base_url", "proxy", "temperature",
                                 "timeout", "max_retries", "extra_body",
                                 "use_responses_api", "reasoning", "output_version")}
        if not model_str:
            return None
        cfg = {"model": model_str, **base_cfg}
        model = _build_chat_model(cfg)
    except Exception as e:  # pragma: no cover - construction is config-dependent
        print(f"⚠️  [agent] S2a summarize_fn build failed: {e}")
        return None

    def summarize(prompt: str) -> str:
        # A bare model_str (no kwargs) → _build_chat_model returns the string id;
        # init it to a model so .invoke exists.
        m = model
        if isinstance(m, str):
            m = init_chat_model(m)
        resp = m.invoke([HumanMessage(content=prompt)])
        content = getattr(resp, "content", resp)
        if isinstance(content, list):  # multimodal blocks → join text parts
            content = "".join(
                p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
        return content if isinstance(content, str) else str(content)

    return summarize


def _build_s2a_compactor(agent_cfg, vfs_store, wf_id, vfs_reader=None):
    """Assemble the opt-in S2a context-aware per-output LLM compactor.

    OFF BY DEFAULT: returns None unless ``agent.compaction.s2a_enabled`` is true.
    Even when enabled, wired ONLY when BOTH (a) a summarize_fn can be built from
    ``agent_cfg`` (the BYO-LLM model + run creds ride ``agent_cfg``) and (b) a
    persistent VFS cache is available (``vfs_store`` + ``wf_id`` — the persistent
    AGENT tier). If any is missing → None → ``LifecyclePolicyEdit`` runs the
    deterministic head+tail/S1 path (fail-soft). Cap =
    ``agent.compaction.s2a_oversize_tokens`` (default 8k). ``vfs_reader`` lets the
    gist read the FULL body from VFS by path (§4.1a fix), not the omitted body.
    """
    if not vfs_store or not wf_id:
        return None
    if not _cfg_get(agent_cfg, "s2a_enabled", False):
        return None  # head+tail is the default; S2a is the opt-in upgrade
    summarize_fn = _build_s2a_summarize_fn(agent_cfg)
    if summarize_fn is None:
        return None
    cap = _cfg_get(agent_cfg, "s2a_oversize_tokens", S2A_OVERSIZE_TOKENS_DEFAULT)
    token_model = _cfg_get(agent_cfg, "model", "") or ""
    return S2aCompactor(
        summarize_fn=summarize_fn,
        cache=VfsS2aCache(vfs_store, wf_id),
        cap=cap,
        model=token_model,
        vfs_reader=vfs_reader or (VfsBodyReader(vfs_store, wf_id) if wf_id else None),
    )


def _cfg_get(agent_cfg, key: str, default=None):
    if isinstance(agent_cfg, dict):
        return agent_cfg.get(key, default)
    return getattr(agent_cfg, key, default) if agent_cfg else default


def _model_context_tokens(agent_cfg) -> int | None:
    value = _cfg_get(agent_cfg, "model_context_tokens", None)
    try:
        tokens = int(value)
    except (TypeError, ValueError):
        return None
    return tokens if tokens > 0 else None


def _build_s2b_compactor(agent_cfg, vfs_store, wf_id):
    """Assemble the default-on S2b whole-prefix long-session compactor.

    Wired ONLY when (a) ``agent.compaction.s2b_enabled`` (default True), (b) a
    summarize_fn can be built from ``agent_cfg`` (the BYO-LLM model + run creds ride
    ``agent_cfg`` — resolved through ``resolve_compaction_model`` exactly like S2a),
    and (c) a persistent VFS range cache is available (``vfs_store`` + ``wf_id``).
    Any missing → None → ``LifecyclePolicyEdit`` runs without S2b (pure S1 +
    head+tail). Trigger/target/head/tail come from ``agent.compaction.*``.
    ``wf_id`` is the conversation/thread id (first component of the range key)."""
    if not vfs_store or not wf_id:
        return None
    if not _cfg_get(agent_cfg, "s2b_enabled", True):
        return None
    summarize_fn = _build_s2a_summarize_fn(agent_cfg)  # shared BYO-LLM adapter
    if summarize_fn is None:
        return None
    token_model = _cfg_get(agent_cfg, "model", "") or ""
    context_tokens = _model_context_tokens(agent_cfg)
    trigger = _cfg_get(
        agent_cfg, "summary_trigger_tokens",
        int(context_tokens * 0.80) if context_tokens else 120_000,
    )
    target = _cfg_get(
        agent_cfg, "summary_target_tokens",
        int(context_tokens * 0.45) if context_tokens else 60_000,
    )
    live_tail = _cfg_get(agent_cfg, "summary_live_tail", 4)
    pinned_head = _cfg_get(agent_cfg, "summary_pinned_head", 2)
    return S2bCompactor(
        summarize_fn=summarize_fn,
        cache=VfsS2bCache(vfs_store, wf_id),
        thread_id=wf_id,
        model=token_model,
        trigger=trigger,
        target=target,
        pinned_head=pinned_head,
        live_tail=live_tail,
    )


def _build_context_edits(agent_cfg=None, vfs_store=None, wf_id="",
                         command_contexts=None,
                         activated_this_turn=None, hard_context=None,
                         form_projection_holder=None,
                         debug_context_holder=None):
    """Build the ContextEditingMiddleware edit chain for a turn.

    ``LifecyclePolicyEdit`` subsumes the former ClearToolUsesEdit +
    WorkflowProjectionStripEdit (token-gated compaction + superseded
    workflow-projection sweep). ``ContextPrefixStripEdit`` is retained
    (it edits the HumanMessage prefix, unrelated to tool outputs).

    ``vfs_store`` / ``wf_id`` (S2a): the persistent VFS + workflow id — used to
    build the S2a gist cache. Absent → S2a stays inert (pure S1).
    """
    context_tokens = _model_context_tokens(agent_cfg)
    trigger = _cfg_get(
        agent_cfg, "context_trigger",
        int(context_tokens * 0.50) if context_tokens else 80_000,
    )
    clear_at_least = _cfg_get(
        agent_cfg, "context_clear_at_least",
        int(context_tokens * 0.10) if context_tokens else 20_000,
    )
    # Tokenize ``meta.tokens`` with the active agent model because counts are
    # tokenizer-specific). The reserved `agent.compaction.model` slot is for the
    # LLM-compaction stages (S2a/S2b), NOT token counting — kept distinct.
    token_model = _cfg_get(agent_cfg, "model", "") or ""
    # The default form for fresh large outputs is a synchronous head/tail view.
    # VFS body reader so the middleware re-hydrates the full body by path. Absent
    # vfs_store/wf_id → None → tier-2 inert (the omitted output keeps abstract+path).
    vfs_reader = VfsBodyReader(vfs_store, wf_id) if (vfs_store and wf_id) else None
    s2a = _build_s2a_compactor(agent_cfg, vfs_store, wf_id, vfs_reader=vfs_reader)
    # S2b is the whole-prefix safety net and runs first inside the edit
    # (before S2a/head+tail/S1). Absent vfs_store/wf_id or disabled → None → pure S1.
    s2b = _build_s2b_compactor(agent_cfg, vfs_store, wf_id)
    threshold = _cfg_get(agent_cfg, "s2a_oversize_tokens", S2A_OVERSIZE_TOKENS_DEFAULT)
    head_tok = _cfg_get(agent_cfg, "headtail_head_tokens", 1500)
    tail_tok = _cfg_get(agent_cfg, "headtail_tail_tokens", 500)
    compaction_v2 = _cfg_get(agent_cfg, "compaction_v2", None)
    if isinstance(compaction_v2, dict):
        from vibecanvas_api.config import CompactionV2Config
        compaction_v2 = CompactionV2Config(compaction_v2)
    file_context_tiers = getattr(compaction_v2, "file_context_tiers", None)
    file_context_head_tokens = getattr(compaction_v2, "file_context_head_tokens", 2000)
    file_context_tail_tokens = getattr(compaction_v2, "file_context_tail_tokens", 2000)
    file_input_head_tokens = getattr(compaction_v2, "file_input_head_tokens", 512)
    file_input_tail_tokens = getattr(compaction_v2, "file_input_tail_tokens", 512)
    max_node_specs = getattr(agent_cfg, "context_max_node_specs", 5) if agent_cfg else 5
    edits = []
    # The system prompt is injected by create_agent(system_prompt=...) at model-call
    # time (never persisted into the message state), so there is no leading system
    # message in the history to keep current — no SystemPromptEdit needed.
    # Built-in commands are persistent capabilities, similar to platform-owned
    # skills. Their context is injected next to the latest activation message
    # before compaction so token estimation sees the same command payload the
    # model will receive. Repeating the same command moves the injection point to
    # the latest command message.
    if command_contexts:
        from vibecanvas_api.agents.middleware.command_context_edit import CommandContextEdit
        edits.append(
            CommandContextEdit(
                dict(command_contexts),
                set(activated_this_turn or set()),
            )
        )
    # Form-ladder compaction v2 (spec 2026-06-22) — gated. When
    # ``agent.compaction.v2.v2_enabled`` is set, the v2 ``FormLadderEdit`` REPLACES
    # ``LifecyclePolicyEdit`` as the leading compaction edit; otherwise the live
    # path is byte-for-byte unchanged.
    if compaction_v2 is not None and compaction_v2.v2_enabled:
        from vibecanvas_api.agents.middleware.form_ladder_edit import FormLadderEdit
        edits.append(FormLadderEdit(
            {"compaction_v2": compaction_v2.to_runtime_dict()},
            decision_holder=debug_context_holder,
        ))
    else:
        edits.append(
            LifecyclePolicyEdit(trigger=trigger, clear_at_least=clear_at_least,
                                model=token_model, s2a=s2a, s2b=s2b, vfs_reader=vfs_reader,
                                headtail_head_tokens=head_tok, headtail_tail_tokens=tail_tok,
                                headtail_threshold=threshold,
                                file_context_tiers=file_context_tiers,
                                file_context_head_tokens=file_context_head_tokens,
                                file_context_tail_tokens=file_context_tail_tokens,
                                file_input_head_tokens=file_input_head_tokens,
                                file_input_tail_tokens=file_input_tail_tokens,
                                max_node_specs=max_node_specs,
                                interactive_artifact_protect_recent_rounds=getattr(
                                    compaction_v2,
                                    "interactive_artifact_protect_recent_rounds",
                                    3,
                                ),
                                form_projection_holder=form_projection_holder))
    edits.append(ContextPrefixStripEdit(keep=1))
    # Cleanup for legacy dynamic hard-context reminders + active todo guidance.
    # Unfinished todos are inserted before the latest AIMessage in the
    # model-facing copy, so the next assistant step is guided without appending
    # a reminder after the model's own output or breaking tool adjacency.
    if hard_context is not None:
        from vibecanvas_api.agents.middleware.hard_context_edit import HardContextEdit
        edits.append(HardContextEdit(hard_context))
    if vfs_store and wf_id:
        # state.md is the agent-curated, always-pinned core memory.
        # Appended LAST (after compaction AND the recitation) so the resume
        # anchor lands at the absolute tail and is NEVER subject to S1/S2b. Inert
        # without a persistent VFS + wf_id (same gate as S2a/S2b/VfsBodyReader).
        from vibecanvas_api.agents.middleware.state_edit import StateEdit
        edits.append(StateEdit(vfs_store, wf_id))
    try:
        from vibecanvas_api.config import config as app_config
        if getattr(app_config, "agent_debug_view_enabled", False):
            from vibecanvas_api.agents.middleware.debug_snapshot_edit import DebugSnapshotEdit
            edits.append(DebugSnapshotEdit(
                context=debug_context_holder or hard_context,
                vfs=vfs_store,
                agent_cfg=agent_cfg,
            ))
    except Exception:
        pass
    return edits


async def _load_dynamic_tools(
    tenant_id: Any,
    runtime_mcp_tools: list | None = None,
    runtime_mcp_catalog: list[dict] | None = None,
    runtime_skill_catalog: list[dict] | None = None,
) -> dict:
    """Load the per-tenant DYNAMIC tool groups for the current turn.

    MCP descriptors are selected by the frontend/backend contract and loaded at
    the Runtime boundary before this function runs. Only Skill progressive
    disclosure remains Runtime-managed.

    Each group is fail-soft (a broken path must not abort the turn).
    """
    mcp_tools: list = list(runtime_mcp_tools or [])
    mcp_catalog: list[dict] = list(runtime_mcp_catalog or [])

    # The host has already filtered this immutable catalog with
    # ``skill_installation#can_use``. Never fall back to a tenant-wide SQL
    # read here: that would make the Runtime see shared resources that were
    # not included in the Turn capability.
    skill_catalog: list[dict] = list(runtime_skill_catalog or [])
    # Skill revisions are already mounted read-only below /skills. They use the
    # same filesystem discovery/read protocol as other Runtime files and do not
    # need a second model-facing loader tool.
    meta_tools: list = []

    return {
        "mcp_catalog": mcp_catalog,
        "skill_catalog": skill_catalog,
        "mcp_tools": mcp_tools,
        "meta_tools": meta_tools,
        "skill_tools": [],           # reserved (run_skill_script if re-introduced)
        # Knowledge Base access is a cross-Runtime Platform MCP capability.
        # Keep the composer slot empty until the legacy argument is removed.
        "kb_tools": [],
    }


async def _get_or_create_agent(
    agent_cfg, checkpointer: Any, tenant_id: Any,
    wf_id: str = "",
    username: str = "",
    current_workflow_id: str | None = None,
    vfs_store: Any = None,
    runtime_mcp_tools: list | None = None,
    runtime_mcp_catalog: list[dict] | None = None,
    runtime_skill_catalog: list[dict] | None = None,
    active_modes: set[str] | None = None,
    command_contexts: dict[str, str] | None = None,
    activated_this_turn: set[str] | None = None,
    surface: str = "chat",
    hard_context_holder: dict | None = None,
    form_projection_holder: dict | None = None,
    debug_context_holder: dict | None = None,
    conversation_clock: dict | None = None,
) -> Any:
    """Build and return a fresh agent for this turn.

    MCPs are already selected by the user and attached by the backend Runtime
    protocol. Skills retain progressive disclosure through their read-only
    ``/skills/<skill-id>/SKILL.md`` files.
    """
    dyn = await _load_dynamic_tools(
        tenant_id,
        runtime_mcp_tools=runtime_mcp_tools,
        runtime_mcp_catalog=runtime_mcp_catalog,
        runtime_skill_catalog=runtime_skill_catalog,
    )
    model = _build_chat_model(agent_cfg)
    token_model_for_recording = _cfg_get(agent_cfg, "model", "") or ""
    effective_modes = set(active_modes or set())
    agent_tools = build_tools(
        effective_modes,
        surface=surface,
        kb_tools=dyn["kb_tools"],
        meta_tools=dyn["meta_tools"],
        mcp_tools=dyn["mcp_tools"],
        skill_tools=dyn["skill_tools"],
        runtime_location=(
            "sandbox"
            if os.environ.get("VIBECANVAS_AGENT_RUNTIME_IN_SANDBOX") == "1"
            else "host"
        ),
    )
    # System prompt: includes the full MCP + skill catalog so agent can discover
    # and load integrations on demand (brief catalog, no per-agent selection needed).
    sys_prompt = build_system_prompt(
        effective_modes,
        surface=surface,
        mcp_catalog=dyn["mcp_catalog"],
        skill_catalog=dyn["skill_catalog"],
        conversation_clock=conversation_clock,
    )
    _all_tool_names = [getattr(t, "name", "?") for t in agent_tools]
    if debug_context_holder is not None:
        registry: list[dict[str, Any]] = []
        for tool in agent_tools:
            args_schema = getattr(tool, "args_schema", None)
            try:
                schema = args_schema.model_json_schema() if args_schema is not None else None
            except Exception:
                schema = None
            registry.append({
                "name": str(getattr(tool, "name", "") or ""),
                "description": str(getattr(tool, "description", "") or ""),
                "input_schema": schema,
            })
        debug_context_holder["tool_registry"] = registry
        debug_context_holder["mcp_catalog"] = list(dyn["mcp_catalog"])
    print(f"🔎 [agent] active_modes={sorted(active_modes) if active_modes else []} "
          f"sys_prompt_len={len(sys_prompt)} n_tools={len(_all_tool_names)}")
    agent = create_agent(
        model=model,
        tools=agent_tools,
        system_prompt=sys_prompt,
        state_schema=_AgentStateExt,
        context_schema=AgentContext,
        checkpointer=checkpointer,
        middleware=[
            # Explicit per-Turn budgets plus conservative retries. Only known
            # read-only tools retry; writes/external side effects remain once-only.
            RuntimeResilienceMiddleware(
                max_model_calls=_cfg_get(agent_cfg, "max_model_calls", 32),
                max_tool_calls=_cfg_get(agent_cfg, "max_tool_calls", 64),
                wall_clock_s=_cfg_get(agent_cfg, "turn_wall_clock_s", 900),
                model_retries=_cfg_get(agent_cfg, "model_retries", 1),
                read_tool_retries=_cfg_get(agent_cfg, "read_tool_retries", 1),
                trace_holder=debug_context_holder,
            ),
            # Record meta.tokens at message-creation so it PERSISTS in the
            # checkpointer before ContextEditingMiddleware
            # so the counts exist when compaction reads them.
            TokenRecordMiddleware(model=token_model_for_recording),
            # read_images: drain ctx.pending_images → one HumanMessage of image
            # blocks before the model call (the message is pre-stamped with the
            # pixel-based token cost, so TokenRecord above leaves it alone).
            ImageInjectionMiddleware(),
            # Reload ctx.workflow from the committed DB HEAD before each build
            # tool executes, picking up any user saves committed since turn start
            # (including the automatic save-before-send from run-agent-turn.ts).
            WorkflowRefreshMiddleware(),
            ContextEditingMiddleware(edits=_build_context_edits(
                agent_cfg, vfs_store=vfs_store, wf_id=wf_id,
                command_contexts=command_contexts,
                activated_this_turn=activated_this_turn,
                hard_context=hard_context_holder,
                form_projection_holder=form_projection_holder,
                debug_context_holder=debug_context_holder)),
            # FU-2: serialize a step's tool_calls (ToolNode runs them via
            # asyncio.gather) so tools sharing mutable state — run_workflow /
            # node_execute on the session run_dir/__exec__ — can't clobber each other.
            UserApprovalMiddleware(),
            SerialToolExecutionMiddleware(),
        ],
    )
    return agent


# ---------------------------------------------------------------------------
# Update normalization for the frontend
# ---------------------------------------------------------------------------

def _normalize_updates_for_frontend(updates: list, new_workflow: dict) -> list:
    """Pass-through. vibe-ops v2 ops (add/replace/remove/text_edit) are carried
    raw in the VIBE_ACTION payload. The frontend refetches the committed
    workflow on VIBE_ACTION (it does NOT apply these ops — it only reads
    `len(updates)` for a toast), so no lowering is needed. Kept as a named
    function so the VIBE_ACTION call site stays stable and a future client-side
    applier (T12) has one place to add lowering if it ever wants it.
    """
    return list(updates or [])


# ---------------------------------------------------------------------------
# Streaming bridge
# ---------------------------------------------------------------------------

def _build_flush_pending_vibe(context, build_signal):
    """Return a generator that yields VIBE_ACTION + META_SYNC for any pending
    canvas update. Generator yields zero signals if no update is pending.

    Extracted from _stream_and_yield so it can be unit-tested without
    spinning up the full agent."""
    def _flush():
        pending = context.pending_vibe if context else None
        if not pending:
            return
        context.pending_vibe = None
        new_wf = pending["new_workflow"]
        new_meta = (new_wf or {}).get("__meta__", {})
        yield build_signal("VIBE_ACTION", {
            "updates": _normalize_updates_for_frontend(
                pending["updates"], new_wf
            ),
            "apply_auto_layout": pending["apply_auto_layout"],
            "workflow_id": new_meta.get("workflow_id"),
            "workflow_version": new_meta.get("workflow_version"),
            "workflow_subversion": new_meta.get("workflow_subversion"),
        })
        yield build_signal("META_SYNC", {"meta": new_meta})
    return _flush


def _tool_name_from_message(msg: Any) -> str | None:
    name = getattr(msg, "name", None)
    if isinstance(name, str) and name:
        return name
    artifact = getattr(msg, "artifact", None)
    if isinstance(artifact, dict):
        meta = artifact.get("meta")
        if isinstance(meta, dict) and isinstance(meta.get("tool"), str):
            return meta["tool"]
    return None


def _normalized_tool_artifact(msg: Any) -> dict | None:
    """Unwrap the official LangChain MCP structured-content carrier.

    Native tools place the Flowork envelope directly in
    ``ToolMessage.artifact``. ``langchain-mcp-adapters`` correctly preserves an
    MCP server's ``structuredContent`` under ``structured_content``. Platform
    MCP returns the same envelope there, so normalize once at the streaming
    boundary instead of teaching every frontend renderer about SDK wrappers.
    """
    artifact = getattr(msg, "artifact", None)
    if not isinstance(artifact, dict):
        return None
    structured = artifact.get("structured_content")
    if (
        isinstance(structured, dict)
        and structured.get("schema_version") == 1
        and structured.get("status") in {"success", "error"}
    ):
        return structured
    return artifact


def _platform_projection_events(msg: Any) -> list[dict]:
    artifact = _normalized_tool_artifact(msg)
    meta = artifact.get("meta") if isinstance(artifact, dict) else None
    events = meta.get("platform_events") if isinstance(meta, dict) else None
    if not isinstance(events, list):
        return []
    return [
        item
        for item in events
        if isinstance(item, dict)
        and isinstance(item.get("type"), str)
        and isinstance(item.get("payload"), dict)
    ]


def _interactive_completion_mode(msg: Any) -> str | None:
    """Read render_interactive's completion contract from a ToolMessage.

    The full definition may be inline in the artifact or offloaded to VFS. The
    compact tool envelope always retains ``output.completion_mode``, so inspect
    both shapes without depending on frontend projection details.
    """
    if not isinstance(msg, ToolMessage) or _tool_name_from_message(msg) != "render_interactive":
        return None
    # Native LangChain tools place the envelope directly in ``artifact`` while
    # langchain-mcp-adapters nests MCP ``structuredContent`` one level deeper.
    # The stream projection already normalizes both forms; wait detection must
    # use the same envelope or an MCP render_interactive call will incorrectly
    # let the model continue and never create its durable new-Turn gate.
    artifact = _normalized_tool_artifact(msg)
    if isinstance(artifact, dict):
        payload = artifact.get("payload")
        if isinstance(payload, dict):
            definition = payload.get("artifact")
            if isinstance(definition, dict):
                mode = definition.get("completion_mode")
                if isinstance(mode, str):
                    return mode
    try:
        envelope = json.loads(_message_content_to_text(getattr(msg, "content", "")))
    except Exception:
        envelope = None
    if isinstance(envelope, dict):
        output = envelope.get("output")
        if isinstance(output, dict):
            mode = output.get("completion_mode")
            if isinstance(mode, str):
                return mode
    return None


def _tool_message_waits_for_user(msg: Any) -> bool:
    return _interactive_completion_mode(msg) == "wait_for_submit"


def _interactive_artifact_id_from_message(msg: Any) -> str:
    if not isinstance(msg, ToolMessage) or _tool_name_from_message(msg) != "render_interactive":
        return ""
    artifact = _normalized_tool_artifact(msg)
    if isinstance(artifact, dict):
        payload = artifact.get("payload")
        if isinstance(payload, dict):
            for candidate in (payload.get("artifact"), payload.get("artifact_preview")):
                if isinstance(candidate, dict) and candidate.get("artifact_id"):
                    return str(candidate["artifact_id"])
    try:
        envelope = json.loads(_message_content_to_text(getattr(msg, "content", "")))
    except Exception:
        envelope = None
    if isinstance(envelope, dict) and isinstance(envelope.get("output"), dict):
        return str(envelope["output"].get("artifact_id") or "")
    return ""


def _bind_hitl_request_to_tool_projection(
    msg: ToolMessage,
    hitl_request_id: str,
    *,
    status: str,
) -> None:
    """Enrich the outbound projection; PostgreSQL remains authoritative."""
    # Mutate the normalized object in place. For MCP ToolMessages this is the
    # ``structured_content`` child of the SDK wrapper, so the subsequently
    # emitted tool_end frame and the durable history projection see identical
    # HITL identifiers and state.
    artifact = _normalized_tool_artifact(msg)
    if isinstance(artifact, dict):
        payload = artifact.get("payload")
        if isinstance(payload, dict):
            payload["hitl_request_id"] = hitl_request_id
            definition = payload.get("artifact")
            if isinstance(definition, dict):
                definition["hitl_request_id"] = hitl_request_id
                interaction_state = definition.get("interaction_state")
                if isinstance(interaction_state, dict):
                    interaction_state["status"] = status
    try:
        envelope = json.loads(_message_content_to_text(getattr(msg, "content", "")))
    except Exception:
        envelope = None
    if isinstance(envelope, dict) and isinstance(envelope.get("output"), dict):
        envelope["output"]["hitl_request_id"] = hitl_request_id
        msg.content = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


async def _ensure_post_tool_interaction_request(
    *,
    agent: Any,
    config: dict,
    context: AgentContext,
    msg: ToolMessage,
    publish_interaction: Callable[[dict], Awaitable[None]] | None,
) -> str:
    """Publish a post-tool gate through the host Runtime control plane.

    The sandbox owns only the SDK projection and checkpoint reference.  The
    host validates the already-durable artifact and creates the encrypted HITL
    row before it exposes this Tool result to the frontend.
    """
    artifact_id = _interactive_artifact_id_from_message(msg)
    if not artifact_id:
        raise RuntimeError("wait_for_submit ToolMessage is missing artifact_id")
    if not context.tenant_id or not context.chat_id:
        raise RuntimeError("post-tool interaction requires tenant_id and chat_id")
    if publish_interaction is None:
        raise RuntimeError("post-tool interaction requires a host Runtime broker")

    stable_suffix = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"vibecanvas:interactive:{artifact_id}",
    ).hex[:16]
    hitl_request_id = f"hitl_{stable_suffix}"
    request_status = "pending"
    _bind_hitl_request_to_tool_projection(
        msg,
        hitl_request_id,
        status=request_status,
    )
    projected_artifact = deepcopy(_normalized_tool_artifact(msg) or {})
    payload = _json_dict(projected_artifact.get("payload"))
    metadata = _json_dict(projected_artifact.get("meta"))
    content_hash = payload.get("hash") or metadata.get("content_hash")
    tool_call_id = str(getattr(msg, "tool_call_id", None) or artifact_id)

    # The private Runtime bus is ordered. The orchestrator persists this event
    # before consuming the following Tool projection, so the browser can never
    # observe a non-recoverable Continue card.
    await publish_interaction({
        "hitl_request_id": hitl_request_id,
        "artifact_id": artifact_id,
        "tool_call_id": tool_call_id,
        "artifact": projected_artifact,
        "runtime_correlation": {
            "source": "langchain",
            "runtime_request_id": artifact_id,
            "runtime_method": "tool/postInteraction",
            "runtime_thread_id": context.thread_id or None,
            "runtime_turn_id": context.turn_id or None,
            "runtime_item_id": tool_call_id,
        },
    })

    ref = {
        "artifact_id": artifact_id,
        "hitl_request_id": hitl_request_id,
        "status": request_status,
        "content_hash": content_hash,
        "db_ref": f"interactive_artifact:{artifact_id}",
    }
    context.interactive_artifact_refs[artifact_id] = ref
    # Agent resume is durable before the card is published. Match the pre-tool
    # gate's real-time state update; do not wait for end-of-Turn finally.
    await _safe_update_state(
        agent,
        config,
        {"interactive_artifact_refs": dict(context.interactive_artifact_refs)},
    )
    _bind_hitl_request_to_tool_projection(
        msg,
        hitl_request_id,
        status=request_status,
    )
    return hitl_request_id


def _todo_items_from_artifact(artifact: Any) -> list[dict] | None:
    if not isinstance(artifact, dict):
        return None
    body = artifact.get("artifact")
    if not isinstance(body, dict):
        return None
    handles = body.get("handles")
    if not isinstance(handles, dict):
        return None
    items = handles.get("todo_items")
    if not isinstance(items, list):
        return None
    return [
        item for item in items
        if isinstance(item, dict)
        and item.get("status") in {"pending", "in_progress", "done"}
    ]


async def _stream_and_yield(
    agent,
    input_data: Any,
    config: dict,
    chat_id: str,
    build_signal: Callable,
    context: AgentContext | None = None,
    stop_event: Any | None = None,
    emit_noop: bool = True,
    turn_id: str = "",
    request_tool_approval: Callable[
        [str, str, dict], Awaitable[str]
    ] | None = None,
    publish_post_tool_interaction: Callable[[dict], Awaitable[None]] | None = None,
    publish_runtime_usage: Callable[[dict], Awaitable[None]] | None = None,
) -> AsyncIterator[dict]:
    """Stream agent output with token-level granularity."""
    print("🟡 [stream] starting agent.astream (v2, messages+updates) ...")
    stream_started = time.perf_counter()
    first_graph_event_logged = False
    first_model_chunk_logged = False
    first_visible_event_logged = False
    streaming_text = ""
    text_message_id: str | None = None
    text_message_seq = 0
    # Last-known full assistant text. Survives `streaming_text` resets so
    # the end-of-turn defensive flush has the complete content even when
    # the 'updates' branch already cleared streaming_text in the middle.
    last_assistant_text = ""
    last_tool_call_ai: AIMessage | None = None
    closed_tool_call_ids: set[str] = set()
    active_tool_call_ids: set[str] = set()
    suppress_streaming_text = False
    waiting_for_interactive_submit = False
    tool_invocations: dict[str, tuple[dict[str, Any], float]] = {}

    _flush_inner = _build_flush_pending_vibe(context, build_signal)

    def _next_text_message_id() -> str:
        nonlocal text_message_seq
        text_message_seq += 1
        turn_part = turn_id or "turn"
        return f"{chat_id}:{turn_part}:assistant:{text_message_seq}"

    def _tool_message_id(tool_calls: list[dict]) -> str:
        ids = [str(tc.get("id") or "") for tc in tool_calls]
        joined = ":".join([i for i in ids if i])
        turn_part = turn_id or "turn"
        return f"{chat_id}:{turn_part}:tools:{joined or len(ids)}"

    def _tool_args_to_string(args: Any) -> str:
        if isinstance(args, str):
            return args
        try:
            return json.dumps(args if args is not None else {}, ensure_ascii=False)
        except Exception:
            return str(args)

    def _start_invocation(tool_call_id: str, name: str, arguments: Any) -> dict[str, Any]:
        invocation, started = start_tool_invocation(
            invocation_id=tool_call_id,
            runtime_type="langchain",
            name=name,
            arguments=arguments,
            mcp_catalog=(context.runtime_mcp_catalog if context else None),
        )
        if tool_call_id:
            tool_invocations[tool_call_id] = (invocation, started)
        return invocation

    def _finish_invocation(
        tool_call_id: str,
        name: str,
        status: str,
        content: str,
        artifact: dict[str, Any] | None,
    ) -> dict[str, Any]:
        prior = tool_invocations.pop(tool_call_id, None)
        return finish_tool_invocation(
            prior[0] if prior else None,
            started_monotonic=prior[1] if prior else None,
            invocation_id=tool_call_id,
            runtime_type="langchain",
            name=name,
            status=status,
            content=content,
            artifact=artifact,
            mcp_catalog=(context.runtime_mcp_catalog if context else None),
        )

    def _chat_events_for_message(msg: Any) -> list[dict]:
        chatml = to_chatml_message(msg)
        role = chatml.get("role")
        content = _message_content_to_text(chatml.get("content", ""))
        internal_content = _is_internal_chat_content(content)
        if internal_content and not (
            isinstance(msg, AIMessage) and msg.tool_calls
        ):
            return []
        if internal_content:
            content = ""
        if isinstance(msg, AIMessage):
            if msg.tool_calls:
                message_id = _tool_message_id(msg.tool_calls)
                visible_content = _sanitize_visible_assistant_text(content)
                events = [{
                    "type": "message_start",
                    "message_id": message_id,
                    "role": "assistant",
                    # A tool-calling message is also allowed to contain ordinary
                    # assistant prose ("I'll inspect that first"). Preserve it:
                    # it is part of the model's reply and establishes the
                    # visible AI → tool ordering. Only known platform-internal
                    # context markers are filtered above.
                    "content": visible_content,
                }]
                for tc in msg.tool_calls:
                    tcid = tc.get("id") or ""
                    if tcid:
                        active_tool_call_ids.add(tcid)
                    print(
                        "🛠️  [tool_start] "
                        f"name={tc.get('name') or '(unknown tool)'} "
                        f"tool_call_id={tcid or '<missing>'}"
                    )
                    invocation = _start_invocation(
                        tcid,
                        str(tc.get("name") or "(unknown tool)"),
                        tc.get("args"),
                    )
                    events.append({
                        "type": "tool_start",
                        "message_id": message_id,
                        "tool_call_id": tcid,
                        "name": tc.get("name") or "(unknown tool)",
                        "arguments": _tool_args_to_string(invocation.get("input")),
                        "invocation": invocation,
                    })
                events.append({
                    "type": "message_end",
                    "message_id": message_id,
                })
                return events
            content = _sanitize_visible_assistant_text(content)
            message_id = _next_text_message_id()
            return [
                {"type": "message_start", "message_id": message_id, "role": "assistant"},
                {"type": "message_replace", "message_id": message_id, "content": content},
                {"type": "message_end", "message_id": message_id},
            ]
        if isinstance(msg, ToolMessage):
            artifact = _normalized_tool_artifact(msg)
            status = (
                "error"
                if (
                    getattr(msg, "status", None) == "error"
                    or (isinstance(artifact, dict) and artifact.get("status") == "error")
                )
                else "done"
            )
            tcid = getattr(msg, "tool_call_id", "") or ""
            tool_name = _tool_name_from_message(msg) or "(unknown tool)"
            if tcid in active_tool_call_ids:
                active_tool_call_ids.discard(tcid)
                print(f"🔧 [tool_end] name={tool_name} tool_call_id={tcid} status={status}")
            else:
                print(
                    "🔴 [tool_protocol_mismatch] "
                    f"name={tool_name} tool_call_id={tcid or '<missing>'} "
                    f"active={sorted(active_tool_call_ids)} status={status}"
                )
            events = [{
                "type": "tool_end",
                "tool_call_id": tcid,
                "content": content,
                "artifact": artifact if isinstance(artifact, dict) else None,
                "status": status,
                "invocation": _finish_invocation(
                    tcid,
                    tool_name,
                    status,
                    content,
                    artifact if isinstance(artifact, dict) else None,
                ),
            }]
            if tool_name == "todo" and status == "done":
                todo_items = _todo_items_from_artifact(artifact)
                if todo_items is not None:
                    events.append({
                        "type": "todo_update",
                        "items": todo_items,
                    })
            return events
        if role in {"assistant", "system"}:
            message_id = _next_text_message_id()
            return [
                {"type": "message_start", "message_id": message_id, "role": role},
                {"type": "message_replace", "message_id": message_id, "content": _sanitize_visible_assistant_text(content)},
                {"type": "message_end", "message_id": message_id},
            ]
        return []

    async def _gate_pre_tool_approvals(msg: AIMessage) -> None:
        if not context or not msg.tool_calls:
            return
        for tc in msg.tool_calls:
            tool_name = str(tc.get("name") or "")
            tool_call_id = str(tc.get("id") or "")
            args = _json_dict(tc.get("args") or {})
            if not tool_call_id:
                continue
            if not is_pre_tool_approval_candidate(tool_name, args):
                continue
            # The Runtime adapter owns the suspend/resume transport. Missing
            # host control is a denial, never permission to execute.
            if request_tool_approval is None:
                decision = "denied"
            else:
                decision = await request_tool_approval(
                    tool_name,
                    tool_call_id,
                    args,
                )
            context.tool_approval_decisions[tool_call_id] = (
                decision if decision in {"approved", "denied"} else "denied"
            )

    def _flush_pending_vibe():
        """Flush pending canvas sync signals after a canvas-mutating tool."""
        return _flush_inner()

    def _stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    async def _emit_cancel_closure():
        nonlocal streaming_text, text_message_id, last_assistant_text, suppress_streaming_text
        if streaming_text:
            visible_text = _sanitize_visible_assistant_text(streaming_text)
            partial = AIMessage(
                content=visible_text,
                response_metadata={"interrupted": True, "finish_reason": "cancelled"},
            )
            await _safe_update_state(agent, config, {"messages": [partial]}, as_node="model")
            if text_message_id is None:
                text_message_id = _next_text_message_id()
                yield build_signal("CHAT_EVENT", {
                    "type": "message_start",
                    "message_id": text_message_id,
                    "role": "assistant",
                })
            yield build_signal("CHAT_EVENT", {
                "type": "message_replace",
                "message_id": text_message_id,
                "content": visible_text,
            })
            yield build_signal("CHAT_EVENT", {
                "type": "message_end",
                "message_id": text_message_id,
            })
            text_message_id = None
            streaming_text = ""
            last_assistant_text = ""
            suppress_streaming_text = False
            return
        if last_tool_call_ai is not None:
            missing = [
                m for m in _cancelled_tool_messages(last_tool_call_ai)
                if m.tool_call_id not in closed_tool_call_ids
            ]
            if missing:
                await _safe_update_state(agent, config, {"messages": missing}, as_node="tools")
                for msg in missing:
                    for ev in _chat_events_for_message(msg):
                        yield build_signal("CHAT_EVENT", ev)

                    if isinstance(msg, ToolMessage):
                        for platform_event in _platform_projection_events(msg):
                            yield build_signal(
                                platform_event["type"], platform_event["payload"]
                            )

    astream = agent.astream(
        input_data,
        config=config,
        context=context,
        stream_mode=["messages", "updates"],
        version="v2",
    )
    while True:
        try:
            chunk = await astream.__anext__()
        except StopAsyncIteration:
            break
        except asyncio.CancelledError:
            if _stopped():
                print("🟡 [stream] task cancelled by stop request; emitting closure")
                async for ev in _emit_cancel_closure():
                    yield ev
                return
            raise
        if _stopped():
            print("🟡 [stream] stop_event set, breaking out of stream loop")
            break

        ctype = chunk.get("type")
        if not first_graph_event_logged:
            first_graph_event_logged = True
            print(
                "⏱️  [model_timing] "
                f"phase=first_graph_event elapsed_ms={int((time.perf_counter() - stream_started) * 1000)} "
                f"event_type={ctype or 'unknown'} chat_id={chat_id} turn_id={turn_id}"
            )

        if ctype == "messages":
            token, meta = chunk["data"]
            if not isinstance(token, AIMessageChunk):
                continue
            if not first_model_chunk_logged:
                first_model_chunk_logged = True
                print(
                    "⏱️  [model_timing] "
                    f"phase=first_model_chunk elapsed_ms={int((time.perf_counter() - stream_started) * 1000)} "
                    f"chat_id={chat_id} turn_id={turn_id}"
                )
            if token.tool_call_chunks:
                continue
            # `.text` flattens both flat-string (Chat Completions) and
            # typed-block (Responses API) chunk content to plain text.
            delta = token.text or ""
            if delta:
                if not first_visible_event_logged:
                    first_visible_event_logged = True
                    print(
                        "⏱️  [model_timing] "
                        f"phase=first_visible_text elapsed_ms={int((time.perf_counter() - stream_started) * 1000)} "
                        f"chat_id={chat_id} turn_id={turn_id}"
                    )
                if text_message_id is None:
                    text_message_id = _next_text_message_id()
                    suppress_streaming_text = False
                    yield build_signal("CHAT_EVENT", {
                        "type": "message_start",
                        "message_id": text_message_id,
                        "role": "assistant",
                    })
                streaming_text += delta
                visible_text = _sanitize_visible_assistant_text(streaming_text)
                if suppress_streaming_text or _is_internal_chat_content(visible_text):
                    suppress_streaming_text = True
                    yield build_signal("CHAT_EVENT", {
                        "type": "message_replace",
                        "message_id": text_message_id,
                        "content": "",
                    })
                    continue
                last_assistant_text = visible_text
                yield build_signal("CHAT_EVENT", {
                    "type": "message_replace",
                    "message_id": text_message_id,
                    "content": visible_text,
                })

        elif ctype == "updates":
            for node_name, output in chunk["data"].items():
                if node_name == "__interrupt__":
                    print("🟡 [stream] __interrupt__ (unexpected, ignoring)")
                    continue

                # A hook-only middleware node (e.g. TokenRecordMiddleware's
                # after_model/before_model/wrap_tool_call) can yield a None state
                # delta in the "updates" stream — guard so it doesn't crash the turn.
                for msg in (output or {}).get("messages", []):
                    chatml = to_chatml_message(msg)
                    if chatml.get('tool_calls'):
                        names = [tc.get('function', {}).get('name', '?') for tc in chatml.get('tool_calls', [])]
                        print(f"🛠️  [tool_call] {','.join(names)}")
                    elif chatml.get('role') == 'tool':
                        print(f"🔧 [tool_result] name={chatml.get('name','?')} content_len={len(chatml.get('content',''))}")
                    else:
                        print(f"🟡 [stream] {node_name} role={chatml.get('role')} content_len={len(chatml.get('content',''))}")

                    # A no-tool AIMessage is the terminal response of the ReAct
                    # loop.  Empty terminal responses are never a successful
                    # product result: treating one as normal previously made the
                    # UI show a completed turn with no answer.  Do not synthesize
                    # another model call here.  Fail visibly so callers preserve
                    # the exact provider outcome and the user can retry or select
                    # a different model deliberately.
                    if (
                        isinstance(msg, AIMessage)
                        and not msg.tool_calls
                        and not (msg.text or "").strip()
                    ):
                        raise EmptyModelResponseError(
                            "The model returned an empty final response; "
                            "the turn stopped without an automatic retry."
                        )

                    # The updates-branch AIMessage is the full
                    # object and carries usage_metadata (the streaming
                    # "messages" chunks do not). Record per-model token/call
                    # metrics + a per-tenant token log line here. Fail-safe.
                    if isinstance(msg, AIMessage) and getattr(msg, "usage_metadata", None):
                        record_llm_usage(
                            model=(msg.response_metadata or {}).get("model_name")
                                  or (msg.response_metadata or {}).get("model"),
                            usage_metadata=msg.usage_metadata,
                            tenant_id=current_sync_tenant_id.get(),
                        )
                        # Durable metering is host-owned. A sandbox receives no
                        # database credential and publishes only the compact
                        # usage fact over the private Runtime bus.
                        if publish_runtime_usage is not None:
                            await publish_runtime_usage({
                                "model": (
                                    (msg.response_metadata or {}).get("model_name")
                                    or (msg.response_metadata or {}).get("model")
                                    or "unknown"
                                ),
                                "prompt_tokens": int(
                                    msg.usage_metadata.get("input_tokens", 0) or 0
                                ),
                                "completion_tokens": int(
                                    msg.usage_metadata.get("output_tokens", 0) or 0
                                ),
                            })

                    if isinstance(msg, ToolMessage) and _tool_message_waits_for_user(msg):
                        if context is None:
                            raise RuntimeError(
                                "post-tool interaction requires an AgentContext"
                            )
                        await _ensure_post_tool_interaction_request(
                            agent=agent,
                            config=config,
                            context=context,
                            msg=msg,
                            publish_interaction=publish_post_tool_interaction,
                        )

                    if isinstance(msg, AIMessage) and msg.tool_calls:
                        last_tool_call_ai = msg
                        closed_tool_call_ids.clear()
                    elif isinstance(msg, ToolMessage):
                        tcid = getattr(msg, "tool_call_id", None)
                        if isinstance(tcid, str) and tcid:
                            closed_tool_call_ids.add(tcid)

                    # If this is a final-answer AIMessage (no tool_calls) that we
                    # already streamed via "messages" chunks, the streaming_text
                    # has already pushed the full visible content to the frontend.
                    # Re-yielding chatml here would overwrite that streamed content
                    # with `msg.content` — which can be an empty string or a
                    # structured-content list depending on the model, causing the
                    # message to visibly disappear. Skip the redundant yield.
                    if isinstance(msg, AIMessage) and not msg.tool_calls and streaming_text:
                        if text_message_id is not None:
                            content = chatml.get("content", "") or ""
                            if _is_internal_chat_content(content):
                                yield build_signal("CHAT_EVENT", {
                                    "type": "message_replace",
                                    "message_id": text_message_id,
                                    "content": "",
                                })
                            yield build_signal("CHAT_EVENT", {
                                "type": "message_end",
                                "message_id": text_message_id,
                            })
                            text_message_id = None
                        streaming_text = ""
                        suppress_streaming_text = False
                        continue

                    # Tool-calling AIMessage can arrive in `updates` after its
                    # visible prefix was already streamed token-by-token in the
                    # `messages` branch. Re-emitting it as a new tool-call
                    # message duplicates that prefix (`ai_msg1 -> tool`). Bind
                    # the tool calls to the existing streamed message instead.
                    if isinstance(msg, AIMessage) and msg.tool_calls and streaming_text:
                        if text_message_id is None:
                            text_message_id = _next_text_message_id()
                            yield build_signal("CHAT_EVENT", {
                                "type": "message_start",
                                "message_id": text_message_id,
                                "role": "assistant",
                            })
                        # Keep the already-streamed prose on the carrier. It is
                        # user-visible assistant output preceding the tool call,
                        # not disposable protocol data. Clearing it here made
                        # every intermediate AI segment flash and disappear.
                        visible_text = _sanitize_visible_assistant_text(streaming_text)
                        yield build_signal("CHAT_EVENT", {
                            "type": "message_replace",
                            "message_id": text_message_id,
                            "content": (
                                ""
                                if suppress_streaming_text
                                or _is_internal_chat_content(visible_text)
                                else visible_text
                            ),
                        })
                        for tc in msg.tool_calls:
                            tcid = tc.get("id") or ""
                            if tcid:
                                active_tool_call_ids.add(tcid)
                            print(
                                "🛠️  [tool_start] "
                                f"name={tc.get('name') or '(unknown tool)'} "
                                f"tool_call_id={tcid or '<missing>'}"
                            )
                            yield build_signal("CHAT_EVENT", {
                                "type": "tool_start",
                                "message_id": text_message_id,
                                "tool_call_id": tcid,
                                "name": tc.get("name") or "(unknown tool)",
                                "arguments": _tool_args_to_string(tc.get("args")),
                                "invocation": _start_invocation(
                                    tcid,
                                    str(tc.get("name") or "(unknown tool)"),
                                    tc.get("args"),
                                ),
                            })
                        yield build_signal("CHAT_EVENT", {
                            "type": "message_end",
                            "message_id": text_message_id,
                        })
                        await _gate_pre_tool_approvals(msg)
                        text_message_id = None
                        streaming_text = ""
                        last_assistant_text = ""
                        suppress_streaming_text = False
                        continue

                    if isinstance(msg, AIMessage) and streaming_text:
                        if text_message_id is not None:
                            yield build_signal("CHAT_EVENT", {
                                "type": "message_end",
                                "message_id": text_message_id,
                            })
                            text_message_id = None
                        streaming_text = ""
                        suppress_streaming_text = False

                    for ev in _chat_events_for_message(msg):
                        yield build_signal("CHAT_EVENT", ev)

                    if isinstance(msg, ToolMessage):
                        for platform_event in _platform_projection_events(msg):
                            yield build_signal(
                                platform_event["type"], platform_event["payload"]
                            )

                    if isinstance(msg, AIMessage) and msg.tool_calls:
                        await _gate_pre_tool_approvals(msg)

                    tool_name = _tool_name_from_message(msg) if isinstance(msg, ToolMessage) else None
                    if isinstance(msg, ToolMessage) and context and context.pending_vibe:
                        for vibe_sig in _flush_pending_vibe():
                            yield vibe_sig
                    elif (
                        isinstance(msg, ToolMessage)
                        and tool_name == "new_version"
                        and not _platform_projection_events(msg)
                    ):
                        # These persist a commit / major-version bump on the
                        # server but mutate nothing the frontend already holds —
                        # emit META_SYNC so the canvas re-fetches the new version
                        # pointer (otherwise the save/version stays invisible).
                        meta = (context.workflow or {}).get("__meta__", {}) if context else {}
                        yield build_signal("META_SYNC", {"meta": meta})
                    if _tool_message_waits_for_user(msg):
                        # Post-tool elicitation is independent from pre-tool
                        # approval_mode. Once the durable card ToolMessage has
                        # been checkpointed and projected, V1 closes this Turn
                        # before the model can take another step. The user's
                        # explicit submit/cancel starts a new Human Turn.
                        waiting_for_interactive_submit = True
                        print("🟡 [stream] interactive input required; closing current turn")
                if waiting_for_interactive_submit:
                    # A ToolNode may return sibling ToolMessages in the same
                    # update. Project all of them before closing so no already-
                    # executed call is left visually hanging.
                    break
            if waiting_for_interactive_submit:
                close_stream = getattr(astream, "aclose", None)
                if callable(close_stream):
                    try:
                        await close_stream()
                    except Exception as exc:
                        print(f"⚠️  [stream] failed to close interactive wait stream: {exc}")
                break
    if _stopped():
        async for ev in _emit_cancel_closure():
            yield ev

    if streaming_text:
        if text_message_id is not None:
            yield build_signal("CHAT_EVENT", {
                "type": "message_end",
                "message_id": text_message_id,
            })
            text_message_id = None
        streaming_text = ""

    # Defensive final flush — re-emit the full accumulated assistant text
    # so the rendered chat matches what the checkpointer persisted, even
    # if any intermediate token CHAT_UPDATE was lost in transit (throttle
    # drop, buffer eviction, SSE blip). Frontend's consumeChatMessage
    # merges by length, so this is a no-op when the latest token already
    # arrived; it heals truncation otherwise.
    #
    # MUST run BEFORE the synthetic vibe-snapshot injection. If it runs
    # after, the synthetic tool_message becomes lastMsg on the frontend;
    # the flush's role=assistant lands as a new entry (different role)
    # and visibly duplicates the final answer.
    if last_assistant_text:
        # Final flush is a replace on the same text message when possible. It is
        # not a new chat message; this keeps resume/replay idempotent.
        turn_part = turn_id or "turn"
        flush_id = text_message_id or f"{chat_id}:{turn_part}:assistant:{text_message_seq or 1}"
        yield build_signal("CHAT_EVENT", {
            "type": "message_replace",
            "message_id": flush_id,
            "content": last_assistant_text,
        })
        yield build_signal("CHAT_EVENT", {
            "type": "message_end",
            "message_id": flush_id,
        })

    if emit_noop:
        print("🟡 [stream] done, yielding NO_OP")
        yield build_signal("NO_OP", {})


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_agent_turn(
    *args: Any,
    **kwargs: Any,
) -> AsyncIterator[dict]:
    """Run one agent turn — thin observability wrapper around the inner
    generator.

    Wrap the whole turn in an ``agent.turn`` span and record the
    ``AGENT_TURNS_TOTAL`` / ``AGENT_TURN_DURATION`` metrics. The span is opened
    manually (rather than via ``with``) so it stays alive across the inner
    generator's yields and is reliably ended in ``finally`` on every exit path
    (normal end, early return, or exception). A turn is bounded, so holding the
    span open across yields is safe.

    Detached-task note: ``run_agent_turn`` is launched in a detached
    ``asyncio.Task`` (``streaming/turn_runtime.py``), so this span is a
    *separate trace root* — no cross-task context propagation is attempted here
    (deferred for this sub-project). Fail-safe: span/metric bookkeeping never
    breaks the turn or swallows its yields.
    """
    span = _agent_tracer.start_span("agent.turn")
    _t0 = time.time()
    status = "success"
    try:
        async for ev in _run_agent_turn_inner(*args, **kwargs):
            yield ev
    except Exception:
        status = "error"
        raise
    finally:
        try:
            AGENT_TURNS_TOTAL.labels(status=status).inc()
            AGENT_TURN_DURATION.observe(time.time() - _t0)
        except Exception:
            pass
        try:
            span.end()
        except Exception:
            pass


async def _run_agent_turn_inner(
    user_message: dict,
    thread_id: str,
    is_first: bool,
    workflow: dict,
    chat_context: str,
    agent_cfg: dict,
    checkpointer: Any,
    build_signal: Callable,
    chat_id: str,
    stop_event: Any | None = None,
    execution_context: str = "",
    attachments: list | None = None,
    repo: Any = None,
    vfs_store: Any = None,
    username: str = "",
    wf_id: str = "",
    current_workflow_id: str | None = None,
    tenant_id: str | None = None,
    surface: str = "chat",
    approval_mode: str = "agent",
    available_commands: set[str] | None = None,
    active_modes: set[str] | None = None,
    command_contexts: dict[str, str] | None = None,
    activated_this_turn: set[str] | None = None,
    turn_id: str = "",
    runtime_mcp_tools: list | None = None,
    runtime_mcp_catalog: list[dict] | None = None,
    runtime_skill_catalog: list[dict] | None = None,
    runtime_todo_items: list[dict] | None = None,
    runtime_interactive_artifact_refs: dict[str, dict] | None = None,
    runtime_context_manifest: dict[str, Any] | None = None,
    conversation_clock: dict[str, Any] | None = None,
    request_tool_approval: Callable[
        [str, str, dict], Awaitable[str]
    ] | None = None,
    publish_post_tool_interaction: Callable[[dict], Awaitable[None]] | None = None,
    publish_runtime_usage: Callable[[dict], Awaitable[None]] | None = None,
    request_background_job: Callable[
        [str, dict], Awaitable[dict]
    ] | None = None,
) -> AsyncIterator[dict]:
    """Run one agent turn for a user message and yield signals."""
    # The agent is driven entirely by the additive ``active_modes`` (build /
    # browser); there is no exclusive ``mode``. The routes layer folds the legacy
    # API ``mode`` (e.g. body.mode="browser") into ``active_modes`` before calling
    # us, so prompt + tools + browser binding all key off the same set. Browser
    # mode is driven purely by ``/browser`` (active_modes) — no feature flag.
    # Set the RLS tenant ContextVar inside the async entry point.
    # Setting it here — BEFORE any ``asyncio.to_thread`` / sync-repo call
    # the agent will make so every short repository session
    # every ``run_in_short_session`` inside the agent / its tools reads
    # this CV in its calling task's context (``asyncio.to_thread`` copies
    # the calling task's contextvars into the worker thread) and emits
    # ``SET LOCAL app.tenant_id`` so Postgres RLS isolates the write.
    current_sync_tenant_id.set(tenant_id)

    print(f"🟢 [agent] run_agent_turn start: thread={thread_id} is_first={is_first}")

    # Todo is a platform Chat concern, not LangGraph state. The backend sends a
    # complete snapshot on every Turn and commits each emitted update before it
    # becomes visible to the frontend.
    todo_items_prev: List[dict] = list(runtime_todo_items or [])
    current_workflow_id_prev = await _read_checkpoint_channel(
        checkpointer, thread_id, "current_workflow_id", None
    )
    message_form_overrides_prev = dict(
        await _read_checkpoint_channel(checkpointer, thread_id, "message_form_overrides", {})
        or {}
    )
    interactive_artifact_refs_prev = _json_dict(
        await _read_checkpoint_channel(
            checkpointer, thread_id, "interactive_artifact_refs", {}
        )
    )
    interactive_artifact_refs_durable = _json_dict(
        runtime_interactive_artifact_refs or {}
    )
    interactive_artifact_refs_current = {
        **interactive_artifact_refs_prev,
        **interactive_artifact_refs_durable,
    }
    # The backend Chat binding is the product source of truth. A checkpoint may
    # retain the prior binding after a Platform MCP set/create call, but it must
    # never override a newer database value (including an explicit unbind).
    effective_current_workflow_id = current_workflow_id
    hard_context_holder: dict[str, Any] = {}
    debug_context_holder: dict[str, Any] = {}
    form_projection_holder: dict[str, dict] = {}

    try:
        agent = await _get_or_create_agent(
            agent_cfg, checkpointer, tenant_id,
            wf_id=wf_id,
            username=username,
            current_workflow_id=effective_current_workflow_id,
            vfs_store=vfs_store,
            runtime_mcp_tools=runtime_mcp_tools,
            runtime_mcp_catalog=runtime_mcp_catalog,
            runtime_skill_catalog=runtime_skill_catalog,
            active_modes=active_modes,
            command_contexts=command_contexts,
            activated_this_turn=activated_this_turn,
            surface=surface,
            hard_context_holder=hard_context_holder,
            form_projection_holder=form_projection_holder,
            debug_context_holder=debug_context_holder,
            conversation_clock=conversation_clock,
        )
        config = {"configurable": {"thread_id": thread_id}}
        interactive_artifact_refs_current = (
            await _reconcile_interactive_artifact_refs_before_model(
                agent,
                config,
                checkpoint_refs=interactive_artifact_refs_prev,
                durable_refs=interactive_artifact_refs_durable,
            )
        )
        # This is now the checkpoint baseline for turn-end dirty detection.
        interactive_artifact_refs_prev = dict(interactive_artifact_refs_current)
        print(f"🟢 [agent] agent created: {type(agent).__name__}")
    except Exception as e:
        print(f"🔴 [agent] _get_or_create_agent FAILED: {e}")
        traceback.print_exc()
        message_id = f"{chat_id}:{turn_id or 'turn'}:assistant:init_error"
        yield build_signal("CHAT_EVENT", {
            "type": "message_start",
            "message_id": message_id,
            "role": "assistant",
        })
        yield build_signal("CHAT_EVENT", {
            "type": "message_replace",
            "message_id": message_id,
            "content": _format_agent_init_error(e),
        })
        yield build_signal("CHAT_EVENT", {
            "type": "message_end",
            "message_id": message_id,
        })
        yield build_signal("NO_OP", {})
        return

    context = AgentContext(
        workflow=workflow,
        repo=repo,
        vfs=vfs_store,
        username=username,
        wf_id=wf_id,
        tenant_id=tenant_id,
        chat_id=chat_id,
        thread_id=thread_id,
        turn_id=turn_id,
        surface=surface,
        approval_mode=approval_mode,
        active_commands=sorted((command_contexts or {}).keys()),
        available_commands=sorted(available_commands or set()),
        current_workflow_id=effective_current_workflow_id,
        run_id="",
        agent_cfg=agent_cfg,
        stop_event=stop_event,
        runtime_skill_catalog=list(runtime_skill_catalog or []),
        todo_items=list(todo_items_prev),
        interactive_artifact_refs=dict(interactive_artifact_refs_current),
        tool_approval_decisions={},
        background_job_submitter=request_background_job,
        context_manifest=dict(runtime_context_manifest or {}),
        runtime_mcp_catalog=list(runtime_mcp_catalog or []),
    )
    hard_context_holder["context"] = context
    debug_context_holder["context"] = context

    # `additional_kwargs` carries the context-attachment list (set by
    # handlers.agent.handle_agent_chat right after build_context_prefix).
    # It MUST flow through to the LangChain HumanMessage so the
    # checkpointer persists it — otherwise re-entry loses the chips even
    # though the prefix text in `content` survives.
    user_kwargs = (user_message or {}).get("additional_kwargs") or {}
    file_prefix = build_file_attachment_prefix(list(attachments or []))
    user_lc_msg = {
        "role": "user",
        "content": file_prefix + user_message.get("content", ""),
        **({"additional_kwargs": user_kwargs} if user_kwargs else {}),
    }

    # The input is ALWAYS just the new user message. The system prompt is injected
    # by create_agent(system_prompt=...) each model call; prior conversation is
    # resumed automatically by the checkpointer via thread_id (a cold start simply
    # has no prior state). No is_first / system-seeding branch is needed.
    lc_messages: List[dict] = [user_lc_msg]

    print(f"🟢 [agent] lc_messages count={len(lc_messages)} roles={[m['role'] for m in lc_messages]}")

    try:
        # One user Turn maps to exactly one agent invocation. Todo is durable
        # progress state and model-facing guidance, never an outer-loop control
        # signal: unfinished items must not synthesize a new HumanMessage or
        # compete with approval / post-tool interaction pause points.
        async for ev in _stream_and_yield(
            agent,
            {"messages": lc_messages},
            config, chat_id, build_signal,
            context=context,
            stop_event=stop_event,
            emit_noop=False,
            turn_id=turn_id,
            request_tool_approval=request_tool_approval,
            publish_post_tool_interaction=publish_post_tool_interaction,
            publish_runtime_usage=publish_runtime_usage,
        ):
            yield ev

        yield build_signal("NO_OP", {})
    except Exception as e:
        print(f"🔴 [agent] _stream_and_yield EXCEPTION: {e}")
        traceback.print_exc()
        message_id = f"{chat_id}:{turn_id or 'turn'}:assistant:runtime_error"
        yield build_signal("CHAT_EVENT", {
            "type": "message_start",
            "message_id": message_id,
            "role": "assistant",
        })
        yield build_signal("CHAT_EVENT", {
            "type": "message_replace",
            "message_id": message_id,
            "content": f"Agent error: {e}",
        })
        yield build_signal("CHAT_EVENT", {
            "type": "message_end",
            "message_id": message_id,
        })
        yield build_signal("NO_OP", {})
    finally:
        # Persist cross-turn state channels to the checkpointer if changed this turn.
        state_updates: dict = {}
        if context.current_workflow_id != current_workflow_id_prev:
            state_updates["current_workflow_id"] = context.current_workflow_id
        if form_projection_holder != message_form_overrides_prev:
            state_updates["message_form_overrides"] = dict(form_projection_holder)
        if context.interactive_artifact_refs != interactive_artifact_refs_prev:
            state_updates["interactive_artifact_refs"] = _json_dict(
                context.interactive_artifact_refs
            )
        if state_updates:
            try:
                await asyncio.to_thread(
                    agent.update_state,
                    {"configurable": {"thread_id": thread_id}},
                    state_updates,
                )
            except Exception as exc:
                print(f"⚠️  [agent] state channel sync failed: {exc}")

        # G3: end-of-turn ASYNC writeback. Fire-and-forget (NOT awaited) so the
        # conversation isn't blocked on the durable-VFS diff-sync; the session's
        # own coalesce/drain machinery + the close()/sweep drain handle ordering
        # and eviction safety. Fires ONLY if a sandbox session was actually
        # attached THIS turn (``_attached_session`` memoized by sandbox_session)
        # — a pure chat turn that booted no sandbox does not boot one to write
        # back.
        attached = getattr(context, "_attached_session", None)
        if attached is not None:
            try:
                attached.schedule_writeback()
            except Exception:  # pragma: no cover - fail-soft
                print("⚠️  [agent] turn-end writeback schedule failed")

        # Auto-commit any unpersisted vibe edits so they survive a process
        # restart (vibe_workflow commits per-call, this is a safety net).
        # The sync Repo facades use ``run_in_short_session`` which itself
        # calls ``asyncio.run`` — that would crash inside a running event
        # loop, so push each call off the loop via ``asyncio.to_thread``.
        if context.workflow_dirty and context.repo and context.workflow:
            try:
                wf_id_meta = (context.workflow.get("__meta__") or {}).get("workflow_id")
                if wf_id_meta:
                    await asyncio.to_thread(
                        context.repo.commit, wf_id_meta, context.workflow,
                        note="Agent Auto-Save",
                    )
                    await asyncio.to_thread(context.repo.mark_saved, wf_id_meta)
                    fresh = await asyncio.to_thread(
                        context.repo.get_current_workflow, wf_id_meta
                    ) or {}
                    new_meta = fresh.get("__meta__", {})
                    print(f"💾 [agent] auto-saved at turn end: v{new_meta.get('workflow_version', 0)}.sv{new_meta.get('workflow_subversion', 0)}")
                    yield build_signal("META_SYNC", {"meta": new_meta})
            except Exception as e:
                print(f"⚠️  [agent] auto-save failed: {e}")
        print(f"🟢 [agent] run_agent_turn end: thread={thread_id}")
