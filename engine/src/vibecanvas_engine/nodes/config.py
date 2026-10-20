# -*- coding: utf-8 -*-
"""
Module-level configuration for node CONFIG_SCHEMA enums.

These mutable lists are referenced *by identity* inside the CONFIG_SCHEMA dicts
of CodeNode and PromptNode. A jsonschema ``enum`` is just a Python list, so
mutating the list in place makes the schema see the new values immediately.

CodeNode libraries are NO LONGER configured here. The platform provides a base
package environment, and Workflow-specific additions come from the content-
addressed dependency overlay built from
``__meta__.settings.code_requirements``. The CodeNode worker appends the custom
overlay first and the base third-party paths second, both after stdlib
bootstrap. Only the CodeNode *timeout* default still lives here.

Code timeout comes from Workflow Settings (`__meta__.settings.timeouts.code`),
falling back to the app-wide default configured through `set_code_timeout`.
Legacy per-node timeout values are ignored.
"""

_CODE_LANGUAGES: list[str] = ["python"]
_PROMPT_MODELS: list[str] = ["OpenAI", "Gemini"]
_CODE_TIMEOUT: float = 60.0


def set_code_timeout(t: float) -> None:
    global _CODE_TIMEOUT
    if t and t > 0:
        _CODE_TIMEOUT = float(t)


def get_code_timeout() -> float:
    return _CODE_TIMEOUT


def set_programming_languages(langs: list[str]) -> None:
    """Replace the CodeNode ``programming_language`` enum in place."""
    if not langs:
        return
    _CODE_LANGUAGES.clear()
    _CODE_LANGUAGES.extend(langs)


_CUSTOM_PROVIDERS = ["OpenAI", "Gemini"]


def set_prompt_models(models: list[str]) -> None:
    """Replace the PromptNode ``model_name`` enum in place.

    Always appends custom provider names (OpenAI, Gemini) so they pass
    CONFIG_SCHEMA validation even though they're not in llm_registry.
    """
    if not models:
        return
    _PROMPT_MODELS.clear()
    _PROMPT_MODELS.extend(models)
    for p in _CUSTOM_PROVIDERS:
        if p not in _PROMPT_MODELS:
            _PROMPT_MODELS.append(p)


def get_programming_languages() -> list[str]:
    return list(_CODE_LANGUAGES)


def get_prompt_models() -> list[str]:
    return list(_PROMPT_MODELS)
