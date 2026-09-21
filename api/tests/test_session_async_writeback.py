# -*- coding: utf-8 -*-
"""Task 8 — turn-end async writeback: coalesce + drain + close safety.

The methods under test live on :class:`SandboxSession`, whose real ``__init__``
needs many materialized-mount args. We construct a BARE instance (bypassing
``__init__``) and wire only the fields the writeback machinery touches, plus a
counting stand-in for the real diff-sync ``writeback_vfs`` so we can assert how
many times it ran.
"""
import asyncio

import pytest

from vibecanvas_api.services.sandbox.manager import SandboxSession


def _bare_session(delay: float = 0.0) -> SandboxSession:
    s = SandboxSession.__new__(SandboxSession)  # bypass __init__
    s._lock = asyncio.Lock()
    s._transition_lock = asyncio.Lock()
    s._lifecycle_state = "warm"
    s._lifecycle_generation = 0
    s._wb_task = None
    s._wb_pending = False
    s._inflight_operations = 0
    s.last_used = 0.0
    s.closed = False
    s.wf_id = "writeback-test"
    s.run_dir = None
    s.mount_dir = None
    s.n = 0

    async def _wb() -> None:
        if delay:
            await asyncio.sleep(delay)
        s.n += 1

    s.writeback_vfs = _wb  # override the real diff-sync with a counter
    return s


@pytest.mark.asyncio
async def test_schedule_non_blocking_then_drains():
    s = _bare_session()
    s.schedule_writeback()       # returns immediately (fire-and-forget)
    await s.drain_writeback()
    assert s.n == 1


@pytest.mark.asyncio
async def test_coalesce_single_pending():
    s = _bare_session(delay=0.05)
    s.schedule_writeback()       # starts the in-flight run
    s.schedule_writeback()       # coalesced → one pending re-run
    s.schedule_writeback()       # coalesced into the SAME single pending
    await s.drain_writeback()
    assert s.n == 2              # one in-flight + exactly one coalesced re-run


@pytest.mark.asyncio
async def test_close_awaits_inflight():
    s = _bare_session(delay=0.05)
    s.schedule_writeback()
    await s.close()              # must drain the in-flight run, not tear down mid-write
    # close() drains the scheduled run (n=1) AND does its own final writeback_vfs
    # (n=2) — see task-8-report.md "close()-count" note.
    assert s.n == 2
    assert s.closed


@pytest.mark.asyncio
async def test_schedule_after_close_is_noop():
    s = _bare_session()
    await s.close()              # final writeback → n=1, closed=True
    s.schedule_writeback()       # closed → must not start a task
    await s.drain_writeback()
    assert s.n == 1
    assert s._wb_task is None
