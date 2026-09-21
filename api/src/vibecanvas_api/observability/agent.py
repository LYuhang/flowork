"""Agent-turn, model-call, and tool-call instrumentation helpers.

Per-tenant token usage is logged but never used as a Prometheus label.
Fail-safe: nothing here raises into the agent loop."""
from __future__ import annotations

import logging

import structlog

from vibecanvas_api.observability.metrics import (
    AGENT_LLM_CALLS_TOTAL,
    AGENT_LLM_TOKENS_TOTAL,
    AGENT_TOOL_CALLS_TOTAL,
)

_log = logging.getLogger(__name__)
_slog = structlog.get_logger("vibecanvas.agent.usage")


def record_llm_usage(*, model, usage_metadata, tenant_id) -> None:
    """Read usage off an updates-branch AIMessage. usage_metadata is the
    Runtime dict {input_tokens, output_tokens, total_tokens}. Per-model
    Prometheus counters + a per-tenant token log line."""
    try:
        model_label = str(model or "unknown")
        AGENT_LLM_CALLS_TOTAL.labels(model=model_label).inc()
        if usage_metadata:
            prompt = int(usage_metadata.get("input_tokens", 0) or 0)
            completion = int(usage_metadata.get("output_tokens", 0) or 0)
            if prompt:
                AGENT_LLM_TOKENS_TOTAL.labels(model=model_label, type="prompt").inc(prompt)
            if completion:
                AGENT_LLM_TOKENS_TOTAL.labels(model=model_label, type="completion").inc(completion)
            # per-tenant usage → LOG (queryable later / metering ledger input)
            _slog.info(
                "llm_usage",
                tenant_id=str(tenant_id) if tenant_id is not None else None,
                model=model_label,
                prompt_tokens=prompt,
                completion_tokens=completion,
            )
    except Exception:
        _log.exception("record_llm_usage failed")  # never break the turn


def record_tool_call(*, tool_name: str, status: str) -> None:
    try:
        AGENT_TOOL_CALLS_TOTAL.labels(tool_name=str(tool_name), status=status).inc()
    except Exception:
        _log.exception("record_tool_call failed")
