"""Keep accepted synchronous invocation completion attached to its HTTP task.

Cancelling asyncio.to_thread does not stop the underlying synchronous sandbox
RPC. Finish the handler (including its durable terminal record) before allowing
request cancellation to unwind dependencies and release the invocation lease.
This covers cooperative cancellation, not process termination or power loss.
"""
from __future__ import annotations

import asyncio
from functools import wraps
from inspect import signature


def complete_before_cancelling(function):
    @wraps(function)
    async def wrapper(*args, **kwargs):
        owner = asyncio.create_task(function(*args, **kwargs))
        try:
            return await asyncio.shield(owner)
        except asyncio.CancelledError:
            # Retain a strong owner reference and tolerate repeated shutdown /
            # disconnect cancellations until the worker and DB finalizer settle.
            while not owner.done():
                try:
                    await asyncio.shield(owner)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not owner.cancelled():
                owner.exception()  # consume errors when no HTTP response remains
            raise

    # FastAPI must resolve forward annotations in the original module, not this
    # decorator's globals, and retain the original dependency declarations.
    wrapper.__signature__ = signature(function, eval_str=True)
    return wrapper
