"""Small, Runtime-neutral token-counting helpers.

The active Runtime owns conversation history and compaction. Flowork only
needs an approximate counter for host-side artifact previews, so this module
must not depend on a particular Agent SDK's message classes.
"""

from __future__ import annotations

from typing import Any


_TIKTOKEN_CACHE: dict[str, Any] = {}


def count_tokens(text: str, model: str) -> int:
    """Count with tiktoken when applicable, otherwise use chars divided by 4."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)

    encoding = _tiktoken_for(model)
    if encoding is not None:
        try:
            return len(encoding.encode(text))
        except Exception:
            pass
    return max(0, len(text) // 4)


def _tiktoken_for(model: str):
    if not model:
        return None
    if model in _TIKTOKEN_CACHE:
        return _TIKTOKEN_CACHE[model]

    encoding = None
    try:
        provider, separator, name = model.partition(":")
        bare_model = name if separator else provider
        normalized_provider = provider.lower()
        if (
            "openai" in normalized_provider
            or normalized_provider in {"", "gpt"}
            or bare_model.lower().startswith(("gpt", "o1", "o3", "o4"))
        ):
            import tiktoken

            try:
                encoding = tiktoken.encoding_for_model(bare_model)
            except Exception:
                encoding = tiktoken.get_encoding("cl100k_base")
    except Exception:
        encoding = None

    _TIKTOKEN_CACHE[model] = encoding
    return encoding
