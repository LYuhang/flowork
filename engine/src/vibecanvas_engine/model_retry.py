"""Bounded model-request retries; never retry a workflow or a tool invocation."""
from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from contextlib import contextmanager

observations: ContextVar[list | None] = ContextVar('model_retry_observations', default=None)

_active_attempt: ContextVar[tuple | None] = ContextVar('model_active_attempt', default=None)


@contextmanager
def _attempt(record, number):
    token = _active_attempt.set((record, number))
    try:
        yield
    finally:
        _active_attempt.reset(token)


def record_openai_response(response):
    """Persist only protocol enums and numeric usage, never response text."""
    def field(obj, name):
        return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
    choices = field(response, 'choices')
    choice = choices[0] if isinstance(choices, list) and choices else None
    reason = field(choice, 'finish_reason')
    value = {'finish_reason': reason if reason in {
        'stop', 'length', 'content_filter', 'tool_calls', 'function_call'
    } else 'unknown'}
    usage = field(response, 'usage')
    tokens = {}
    for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
        count = field(usage, key)
        if type(count) is int and count >= 0:
            tokens[key] = count
    details = field(usage, 'completion_tokens_details')
    count = field(details, 'reasoning_tokens')
    if type(count) is int and count >= 0:
        tokens['reasoning_tokens'] = count
    if tokens:
        value['usage'] = tokens
    active = _active_attempt.get()
    if active is not None:
        record, number = active
        record.setdefault('responses', []).append({'attempt': number, **value})
    return value


class EmptyModelResponse(RuntimeError):
    pass


def retryable(exc: Exception) -> bool:
    if isinstance(exc, EmptyModelResponse):
        return True
    status = getattr(exc, 'status_code', None)
    if status is None:
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
    if isinstance(status, int):
        return status in {408, 429, 500, 502, 503, 504}
    # SDKs expose different network exception classes; do not import all SDKs.
    return type(exc).__name__ in {
        'APIConnectionError', 'APITimeoutError', 'ConnectError', 'ConnectTimeout',
        'ReadTimeout', 'WriteTimeout', 'PoolTimeout', 'RemoteProtocolError',
    }


def _record(retry):
    if type(retry) is not int or not 0 <= retry <= 10:
        raise ValueError('retry must be an integer between 0 and 10')
    record = {'attempts': 0, 'failures': [], 'succeeded': False}
    target = observations.get()
    if target is not None:
        target.append(record)
    return record


def _check(stop):
    if stop is not None and stop.is_set():
        raise asyncio.CancelledError('Model request cancelled')


def _failed(record, exc, attempt, retry):
    record['failures'].append({'attempt': attempt + 1, 'error_type': type(exc).__name__})
    return attempt < retry and retryable(exc)


def require_text(value):
    if not isinstance(value, str) or not value.strip():
        raise EmptyModelResponse("The model returned no text")
    return value


def call_with_retry(call, retry=0, stop=None, validate=None):
    record = _record(retry)
    for attempt in range(retry + 1):
        _check(stop)
        record['attempts'] += 1
        try:
            with _attempt(record, attempt + 1):
                result = call()
            if validate is not None:
                result = validate(result)
            _check(stop)
            record['succeeded'] = True
            return result
        except Exception as exc:
            if not _failed(record, exc, attempt, retry):
                raise
        deadline = time.monotonic() + min(2 ** attempt, 30)
        while time.monotonic() < deadline:
            _check(stop)
            time.sleep(min(.1, max(0, deadline - time.monotonic())))


async def acall_with_retry(call, retry=0, stop=None, validate=None):
    record = _record(retry)
    for attempt in range(retry + 1):
        _check(stop)
        record['attempts'] += 1
        try:
            with _attempt(record, attempt + 1):
                result = await call()
            if validate is not None:
                result = validate(result)
            _check(stop)
            record['succeeded'] = True
            return result
        except Exception as exc:
            if not _failed(record, exc, attempt, retry):
                raise
        deadline = time.monotonic() + min(2 ** attempt, 30)
        while time.monotonic() < deadline:
            _check(stop)
            await asyncio.sleep(min(.1, max(0, deadline - time.monotonic())))
