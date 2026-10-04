"""Lazy model construction for the workflow ``SubAgentNode``.

This is intentionally separate from the selectable Agent Runtime subsystem.
The workflow node is executed inside the workflow sandbox and imports its
provider SDK only when the node actually runs, keeping the API host's normal
startup path Runtime-SDK neutral.
"""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager

import httpx

from vibecanvas_api.config import AgentConfig


@asynccontextmanager
async def workflow_chat_model(agent_cfg):
    """Own node-local transports until execution ends, including cancellation.

    Close clients in the same event loop that used them. Do not cache models
    across executions: broker capabilities belong to an individual invocation.
    """
    from langchain.chat_models import init_chat_model

    if isinstance(agent_cfg, AgentConfig):
        kwargs = agent_cfg.to_init_kwargs()
        model = agent_cfg.model
    else:
        model = str(agent_cfg.get("model") or "")
        kwargs = {
            key: agent_cfg[key]
            for key in (
                "base_url",
                "api_key",
                "temperature",
                "max_tokens",
                "timeout",
                "max_retries",
                "extra_body",
                "use_responses_api",
                "reasoning",
                "output_version",
            )
            if agent_cfg.get(key) is not None
        }
    if not model:
        raise ValueError("workflow SubAgentNode requires a model")

    async with AsyncExitStack() as resources:
        provider = model.partition(":")[0].lower()
        if kwargs and provider in {"openai", "azure_openai"}:
            timeout_value = kwargs.get("timeout")
            timeout = httpx.Timeout(float(timeout_value)) if timeout_value else httpx.Timeout(60.0)
            client_options: dict = {"timeout": timeout}
            proxy = (
                agent_cfg.get("proxy")
                if isinstance(agent_cfg, dict)
                else getattr(agent_cfg, "proxy", None)
            )
            if proxy:
                client_options["proxy"] = proxy
            kwargs["http_client"] = resources.enter_context(httpx.Client(**client_options))
            kwargs["http_async_client"] = await resources.enter_async_context(
                httpx.AsyncClient(**client_options)
            )
            model_kwargs = dict(kwargs.get("model_kwargs") or {})
            model_kwargs.setdefault("parallel_tool_calls", False)
            kwargs["model_kwargs"] = model_kwargs

        yield init_chat_model(model, **kwargs) if kwargs else init_chat_model(model)


__all__ = ["workflow_chat_model"]
