"""Per-message ``_meta`` token bookkeeping (spec 2026-06-22 §3, §7).

Every message carries a ``_meta`` annotation with an immutable ``unique_id``, a
``round_index`` (the user-turn round), and a per-rung ``tokens`` map. The running
context size is a cheap SUM of each message's ``tokens[current_form]`` — O(new
turns), never a re-tokenize of history.

Forms are FROZEN-ONCE: the first time a form's token count is stamped it sticks
(write-once), so the export stays complete and the fed view re-derives consistently
These helpers operate on the ``_meta`` dict itself; a Runtime adapter may attach
or extract it from its native message representation.
"""
from __future__ import annotations

# Form-ladder rungs that may be stored as ``current_form``.
FORMS = (
    "content",
    "content_abbreviation",
    "content_abstract",
    "ref",
    "content_compress",
    "auxiliary",
)


def new_meta(unique_id: str, round_index: int) -> dict:
    """A fresh ``_meta`` for a newly-created message. ``current_form`` starts at
    the fullest rung (``content``)."""
    tokens = {f: None for f in FORMS}
    tokens["current_form"] = "content"
    return {"unique_id": unique_id, "round_index": round_index, "tokens": tokens}


def stamp_tokens(meta: dict, form: str, n: int) -> None:
    """Write-once record of a form's token count (frozen-once). A second stamp for
    an already-set form is a deliberate no-op."""
    if form not in FORMS:
        raise ValueError(f"unknown form {form!r}")
    tokens = meta.setdefault("tokens", {})
    if tokens.get(form) is None:
        tokens[form] = n


def set_current_form(meta: dict, form: str) -> None:
    """Select the effective rung for this message (monotonic down the ladder is
    enforced by the compaction engine, not here)."""
    if form not in FORMS:
        raise ValueError(f"unknown form {form!r}")
    meta.setdefault("tokens", {})["current_form"] = form


def current_form(meta: dict) -> str:
    return meta.get("tokens", {}).get("current_form", "content")


def current_tokens(meta: dict) -> int:
    """Tokens of the effective rung; 0 if not yet counted."""
    tokens = meta.get("tokens", {})
    return tokens.get(current_form(meta)) or 0


def context_size(metas) -> int:
    """Running context size = Σ over messages of ``tokens[current_form]``."""
    return sum(current_tokens(m) for m in metas)
