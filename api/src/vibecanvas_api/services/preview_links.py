"""Shared, expiring short links. Values are encrypted forwarding capabilities."""

import secrets

from fastapi import HTTPException
from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

from vibecanvas_api.config import config


def client():
    return Redis.from_url(
        config.redis.url, decode_responses=True,
        socket_connect_timeout=2, socket_timeout=2,
        retry=Retry(NoBackoff(), 0),
    )


async def put(token: str, ttl: int) -> str:
    identifier = secrets.token_urlsafe(18)
    try:
        async with client() as redis:
            stored = await redis.set("preview:link:" + identifier, token, ex=ttl, nx=True)
    except (RedisError, OSError) as exc:
        raise HTTPException(503, "service_preview_link_store_unavailable") from exc
    if not stored:
        raise HTTPException(503, "service_preview_link_not_created")
    return identifier


async def get(identifier: str) -> str:
    try:
        async with client() as redis:
            token = await redis.get("preview:link:" + identifier)
    except (RedisError, OSError) as exc:
        raise HTTPException(503, "service_preview_link_store_unavailable") from exc
    if token is None:
        raise HTTPException(410, "service_preview_link_expired_reopen_from_chat")
    return token
