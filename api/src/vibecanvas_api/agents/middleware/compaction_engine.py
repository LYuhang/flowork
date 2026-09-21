"""Compaction engine — pure form-selection over the ladder (spec 2026-06-22 §4).

Operates on lightweight ``item`` dicts so it is independent of Runtime SDK
message classes. Each item:

    {"meta": <_meta dict>, "is_error": bool, "tool": str|None,
     "path": str|None, "stale": bool}

``meta["tokens"]["content"]`` is the raw token count (the size-tier input);
``meta["round_index"]`` the round. The engine MUTATES ``meta`` 's ``current_form``
(monotonic — only ever moves DOWN the ladder). The middleware adapts real messages
to/from these items and triggers the LLM steps (content_compress / B2) that the
engine only marks.

This module: C1 = tiers + A age decay. (C2 batched re-segment + B1, C3 supersession,
C4 B2, C5 content_compress land incrementally in the same file.)
"""
from __future__ import annotations

from vibecanvas_api.agents.middleware.meta_tokens import (
    current_form, set_current_form, context_size,
)

# Degradation rank among the deterministic text rungs (lower = fuller). Used to
# enforce the monotonic ratchet (a form only ever moves to a higher rank).
LADDER_RANK = {
    "content": 0,
    "content_abbreviation": 1,
    "content_abstract": 2,
    "ref": 3,
}


def _rank(form: str) -> int:
    return LADDER_RANK.get(form, 0)


def _age(meta: dict, current_round: int) -> int:
    return current_round - meta.get("round_index", 0)


def _degrade_to(meta: dict, target: str) -> None:
    """Move ``current_form`` to ``target`` ONLY if that is strictly more degraded
    (monotonic ratchet)."""
    if _rank(target) > _rank(current_form(meta)):
        set_current_form(meta, target)


def _is_protected(item: dict, current_round: int, cfg) -> bool:
    """Round 0 (pinned first exchange) and the most-recent ``protect_recent_rounds``
    are never degraded by the lossy A/B1 steps."""
    meta = item["meta"]
    if cfg.pin_first_exchange and meta.get("round_index", 0) == 0:
        return True
    protect_recent_rounds = meta.get(
        "protect_recent_rounds", cfg.protect_recent_rounds
    )
    if _age(meta, current_round) <= max(0, int(protect_recent_rounds)):
        return True
    return False


def _error_resists(item: dict, current_round: int, cfg) -> bool:
    """Error outputs resist A/B1. ``error_protect_rounds`` is
    None → never degrade; an int → degrade only once older than it."""
    if not item.get("is_error"):
        return False
    epr = cfg.error_protect_rounds
    if epr is None:
        return True
    return _age(item["meta"], current_round) <= int(epr)


def age_decay_target(item: dict, current_round: int, cfg) -> str:
    """The A-decay target form for one item (pure). ``content`` if still within its
    size-tier full-rounds budget / protected / error-resisting; else
    ``content_abbreviation``."""
    if _is_protected(item, current_round, cfg) or _error_resists(item, current_round, cfg):
        return "content"
    meta = item["meta"]
    raw = (meta.get("tokens", {}) or {}).get("content") or 0
    tier = cfg.tier_of(raw)
    full_rounds = tier.get("full_rounds")
    if full_rounds is None:               # `none` band (and any never-decay tier)
        return "content"
    if _age(meta, current_round) > full_rounds:
        return "content_abbreviation"
    return "content"


def apply_age_decay(items: list, *, current_round: int, cfg) -> None:
    """Layer A degrades ``content`` to ``content_abbreviation`` for items
    past their size-tier budget. Monotonic; respects protect-window / pin-first /
    error-resist."""
    for item in items:
        target = age_decay_target(item, current_round, cfg)
        _degrade_to(item["meta"], target)


# ─────────────────────── C2: batched re-segment + B1 ────────────────────────
def pressure(items: list, window: int) -> float:
    """Context pressure = running size / model window."""
    if not window:
        return 0.0
    return context_size([it["meta"] for it in items]) / window


def should_resegment(*, current_round: int, prev_pressure: float,
                     cur_pressure: float, cfg) -> bool:
    """A re-segment event fires on a pressure-threshold crossing (B1/B2) OR on the
    fixed cadence ``resegment_every_rounds``. Between events the old
    prefix is byte-stable → cache hits."""
    every = cfg.resegment_every_rounds
    if every and current_round > 0 and current_round % every == 0:
        return True
    for thr in (cfg.pressure_abstract, cfg.pressure_summary):
        if prev_pressure < thr <= cur_pressure:
            return True
    return False


def apply_b1_abstract(items: list, *, current_round: int, window: int, cfg) -> None:
    """B1: under pressure of at least 50%, squeeze oldest to newest toward
    ``content_abstract`` until the running size falls under ``hysteresis_target``.
    Protects the recent window / pinned first / errors."""
    target_size = cfg.hysteresis_target * window
    metas = [it["meta"] for it in items]
    for item in sorted(items, key=lambda it: it["meta"].get("round_index", 0)):
        if context_size(metas) <= target_size:
            break
        if _is_protected(item, current_round, cfg) or _error_resists(item, current_round, cfg):
            continue
        _degrade_to(item["meta"], "content_abstract")


# ─────────────────────── C3: stale-read supersession ───────────────────────
def apply_supersession(items: list, *, cfg) -> None:
    """§4.5: for ``stale_on_reread`` tools, keep only the LATEST output per input
    ``path`` at full form; earlier reads of the same path → ``ref``. Deterministic,
    unconditional, lossless → **overrides the protect window**. Monotonic."""
    stale_tools = set(cfg.stale_on_reread_tools)
    groups: dict = {}
    for idx, item in enumerate(items):
        if item.get("tool") in stale_tools:
            key = item.get("path") or item.get("key")  # fall back to (tool,args) key
            if key is not None:
                groups.setdefault(key, []).append((idx, item))
    for _key, group in groups.items():
        latest_idx = max(group, key=lambda p: (p[1]["meta"].get("round_index", 0), p[0]))[0]
        for idx, item in group:
            if idx != latest_idx:
                _degrade_to(item["meta"], "ref")


def resegment(items: list, *, current_round: int, window: int, cfg,
              force: bool = False) -> dict:
    """Apply one batched A+B1 re-segment, gated by the ``clear_at_least`` floor
    If the batch would reclaim fewer than ``clear_at_least`` tokens it is rolled
    back (never pay a cache rewrite for a trivial gain) — unless ``force`` (B2 at
    ≥80% always fires, §4.0/§4.2). Returns ``{applied, reclaimed}``."""
    metas = [it["meta"] for it in items]
    before = context_size(metas)
    snapshot = [(it["meta"], current_form(it["meta"])) for it in items]

    apply_age_decay(items, current_round=current_round, cfg=cfg)
    if window and (before / window) >= cfg.pressure_abstract:
        apply_b1_abstract(items, current_round=current_round, window=window, cfg=cfg)

    reclaimed = before - context_size(metas)
    if not force and reclaimed < cfg.clear_at_least:
        for meta, form in snapshot:               # roll back — not worth the cache rewrite
            set_current_form(meta, form)
        return {"applied": False, "reclaimed": 0}
    return {"applied": True, "reclaimed": reclaimed}


# ─────────────────── C5: content_compress candidates (selective) ────────────
def compress_candidates(items: list, *, pressure: float, cfg) -> list:
    """§4.1: which outputs are eligible for the per-output LLM gist. Fires only for
    a HUGE single output (raw > ``compress_single_tokens``) OR under real pressure
    (≥ ``compress_pressure``), on items still carrying substance, never on errors.
    The engine only IDENTIFIES; the middleware does the LLM call + persists the
    gist into ``content_compress`` (frozen-once)."""
    out = []
    for item in items:
        if item.get("is_error"):
            continue
        if current_form(item["meta"]) not in ("content", "content_abbreviation"):
            continue
        raw = (item["meta"].get("tokens", {}) or {}).get("content") or 0
        if raw > cfg.compress_single_tokens or pressure >= cfg.compress_pressure:
            item["needs_compress"] = True
            out.append(item)
    return out


# ─────────────────── C4: B2 whole-prefix block summary plan ─────────────────
def b2_plan(items: list, *, current_round: int, window: int, cfg) -> dict | None:
    """§4.2: identify the oldest contiguous prefix to LLM-summarize. Returns None
    below ``pressure_summary``. The summarized span excludes the pinned first
    exchange (round 0) and the protected recent window; the boundary `k` = the last
    summarized round; the ``survivor`` (first message after the span, snapped past
    any tool message so a tool pair isn't split) is where the ``summary_ref`` gets
    stamped. The middleware does the LLM call (on the CURRENT degraded forms, not
    raw — §4.2), writes the summary to VFS keyed by ``boundary_uid``, and stamps the
    survivor."""
    if pressure(items, window) < cfg.pressure_summary:
        return None
    summarize = []
    for item in items:
        ri = item["meta"].get("round_index", 0)
        if cfg.pin_first_exchange and ri == 0:
            continue                                   # pinned first exchange survives
        if (current_round - ri) > cfg.protect_recent_rounds:
            summarize.append(item)
    if not summarize:
        return None
    boundary = max(summarize, key=lambda it: it["meta"].get("round_index", 0))
    bidx = items.index(boundary)
    sidx = bidx + 1
    while sidx < len(items) and items[sidx].get("role") == "tool":
        summarize.append(items[sidx])                  # snap: don't split a tool pair
        sidx += 1
    survivor = items[sidx] if sidx < len(items) else None
    return {
        "summarize": summarize,
        "boundary_uid": boundary["meta"]["unique_id"],
        "survivor": survivor,
    }


# ─────────────────────── D1: tool-INPUT compaction ─────────────────────────
def input_target_form(item: dict, current_round: int, cfg) -> str:
    """§4.3 deterministic input ladder: ``content`` (full args) → ``content_abbreviation``
    → ``ref``. write-content args degrade STRAIGHT to ``ref`` (the VFS file the write
    created is a lossless copy). No LLM. Protect window / pin-first honored."""
    if _is_protected(item, current_round, cfg):
        return "content"
    meta = item["meta"]
    raw = (meta.get("tokens", {}) or {}).get("content") or 0
    tier = cfg.tier_of(raw)
    if tier.get("full_rounds") is None:
        return "content"
    if _age(meta, current_round) <= tier["full_rounds"]:
        return "content"
    if item.get("is_write_content") and item.get("path"):
        return "ref"
    return "content_abbreviation"


def apply_input_decay(input_items: list, *, current_round: int, cfg) -> None:
    """Apply §4.3 input compaction (deterministic, monotonic). ``input_items`` are
    keyed by ``tool_call_id`` upstream; here each carries its own ``_meta``."""
    for item in input_items:
        _degrade_to(item["meta"], input_target_form(item, current_round, cfg))


# ─────────────────────── F1: auxiliary multimodal lane ─────────────────────
def apply_aux_decay(aux_items: list, *, current_round: int, cfg, multimodal: bool = True) -> None:
    """§6.1: a media item stays live (``auxiliary``) for ``aux_full_rounds`` then
    degrades to a caption stub (``ref``). A text-only (non-multimodal) model gets
    the caption immediately. Orthogonal to the text ladder; monotonic."""
    for item in aux_items:
        meta = item["meta"]
        if not multimodal or _age(meta, current_round) > cfg.aux_full_rounds:
            _degrade_to(meta, "ref")


# ─────────────────────── project — the §4.0 ordered pipeline ────────────────
def project(items: list, *, current_round: int, window: int, cfg,
            force: bool = False) -> dict:
    """Run the deterministic §4.0 pipeline in ORDER and return the LLM-step plans
    the middleware must execute:
        1. §4.5 supersession → 2. A age decay → 3. B1 abstract (gated by
        clear_at_least) → 4. content_compress candidates → 5. B2 plan (if ≥80%).
    Steps 1–3 mutate ``current_form`` in place; 4–5 only return plans."""
    apply_supersession(items, cfg=cfg)
    res = resegment(items, current_round=current_round, window=window, cfg=cfg, force=force)
    p = pressure(items, window)
    compress = compress_candidates(items, pressure=p, cfg=cfg)
    b2 = b2_plan(items, current_round=current_round, window=window, cfg=cfg)
    return {"resegment": res, "compress": compress, "b2": b2}
