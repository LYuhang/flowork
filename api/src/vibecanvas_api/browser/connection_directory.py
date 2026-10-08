"""Redis directory for live browser sockets; socket objects stay in their owner.

This directory conveys routing, never authorization or command completion.
Connections expire when their owner stops renewing. Every replacement gets a
new connection ID so a delayed renewal/cleanup cannot modify its successor.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import uuid


@dataclass(frozen=True)
class ConnectionOwner:
    instance_id: str
    connection_id: str
    transport_id: str
    session_id: str
    channel: str = ""  # Empty for extension transport, Chat channel for CDP.

    def encode(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


_REGISTER = """
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
if #KEYS == 2 then
  redis.call('SADD', KEYS[2], KEYS[1])
  redis.call('EXPIRE', KEYS[2], ARGV[2])
end
return 1
"""
_RENEW = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('EXPIRE', KEYS[1], ARGV[2])
if #KEYS == 2 then redis.call('EXPIRE', KEYS[2], ARGV[2]) end
return 1
"""
_REMOVE = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('DEL', KEYS[1])
if #KEYS == 2 then redis.call('SREM', KEYS[2], KEYS[1]) end
return 1
"""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class ConnectionDirectory:
    def __init__(self, redis, *, prefix: str = "flowork:browser", ttl: int = 60):
        self.redis = redis
        self.prefix = prefix
        self.ttl = ttl

    def _scope(self, tenant_id: str, user_id: str) -> str:
        # Multi-key scripts for one browser user share a Redis Cluster slot.
        return f"{self.prefix}:{{{_digest(json.dumps([tenant_id, user_id]))}}}"

    def route_key(self, transport_id: str, channel: str = "") -> str:
        tenant, user, _browser = transport_id.split(":", 2)
        return f"{self._scope(tenant, user)}:route:{_digest(json.dumps([transport_id, channel]))}"

    def session_key(self, tenant_id: str, user_id: str, session_id: str) -> str:
        return f"{self._scope(tenant_id, user_id)}:session:{_digest(session_id)}"

    def _keys(self, owner: ConnectionOwner) -> list[str]:
        keys = [self.route_key(owner.transport_id, owner.channel)]
        if not owner.channel:
            tenant, user, _browser = owner.transport_id.split(":", 2)
            keys.append(self.session_key(tenant, user, owner.session_id))
        return keys

    async def register(self, *, instance_id: str, transport_id: str,
                       session_id: str, channel: str = "") -> ConnectionOwner:
        owner = ConnectionOwner(instance_id, uuid.uuid4().hex, transport_id, session_id, channel)
        await self.put(owner)
        return owner

    async def put(self, owner: ConnectionOwner) -> None:
        keys = self._keys(owner)
        await self.redis.eval(_REGISTER, len(keys), *keys, owner.encode(), self.ttl)

    async def renew(self, owner: ConnectionOwner) -> bool:
        keys = self._keys(owner)
        return bool(await self.redis.eval(_RENEW, len(keys), *keys, owner.encode(), self.ttl))

    async def remove(self, owner: ConnectionOwner) -> bool:
        keys = self._keys(owner)
        return bool(await self.redis.eval(_REMOVE, len(keys), *keys, owner.encode()))

    async def get(self, transport_id: str, channel: str = "") -> ConnectionOwner | None:
        raw = await self.redis.get(self.route_key(transport_id, channel))
        return ConnectionOwner(**json.loads(raw)) if raw else None

    async def find_for_session(self, tenant_id: str, user_id: str,
                               session_id: str) -> ConnectionOwner | None:
        keys = await self.redis.smembers(self.session_key(tenant_id, user_id, session_id))
        if not keys:
            return None
        values = await self.redis.mget(list(keys))
        matches = [ConnectionOwner(**json.loads(value)) for value in values if value]
        # An index may outlive a replaced route. Never infer identity from the
        # index alone or choose arbitrarily when two browser profiles match.
        matches = [owner for owner in matches if owner.session_id == session_id
                   and owner.transport_id.startswith(f"{tenant_id}:{user_id}:")
                   and not owner.channel]
        return matches[0] if len(matches) == 1 else None
