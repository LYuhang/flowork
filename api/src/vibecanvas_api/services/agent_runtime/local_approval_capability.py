"""Short-lived authorization for a local command's approval records only."""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import re
import time
from uuid import UUID, uuid5

_DOMAIN = b'flowork:local-workflow-approval:v1\0'
_AUDIENCE = 'local-workflow-approval'


def workflow_digest(workflow: dict) -> str:
    return hashlib.sha256(json.dumps(workflow, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class LocalApprovalCapability:
    organization_id: str
    user_id: str
    workflow_id: str
    run_id: str
    sandbox_id: str
    runtime_process: dict
    workflow_digest: str
    workflow_version: str
    authorization_generation: str
    issued_at: int
    expires_at: int
    audience: str = _AUDIENCE

    # The common live authorization gate checks the user and Workflow. A
    # background local command is deliberately not an Agent-turn lease.
    @property
    def execution_resource_type(self): return 'workflow'
    @property
    def execution_id(self): return self.workflow_id
    @property
    def principal_type(self): return 'user'
    @property
    def principal_id(self): return self.user_id
    @property
    def principal_generation(self): return 0

    def sample_id(self, index: int) -> str:
        if type(index) is not int or index < 0:
            raise ValueError('invalid sample index')
        return str(uuid5(UUID(self.run_id), str(index)))


def _valid(value, now):
    UUID(value.organization_id)
    UUID(value.user_id)
    UUID(value.run_id)
    process = value.runtime_process
    if not isinstance(process, dict) or set(process) != {'host_id', 'boot_id', 'pid', 'group', 'start'}:
        return False
    if (not isinstance(process['host_id'], str) or not re.fullmatch(r'[a-f0-9]{64}', process['host_id'])
            or type(process['pid']) is not int or process['pid'] <= 1
            or type(process['group']) is not int or process['group'] != process['pid']
            or type(process['start']) is not int or process['start'] < 0):
        return False
    UUID(process['boot_id'])
    return bool(value.audience == _AUDIENCE
        and isinstance(value.sandbox_id, str) and value.sandbox_id
        and isinstance(value.workflow_id, str) and value.workflow_id
        and isinstance(value.authorization_generation, str) and value.authorization_generation
        and re.fullmatch(r'[a-f0-9]{64}', value.workflow_digest)
        and re.fullmatch(r'v[1-9][0-9]*\.sv[0-9]+', value.workflow_version)
        and type(value.issued_at) is int and type(value.expires_at) is int
        and value.issued_at <= now + 30
        and value.expires_at > max(now, value.issued_at))


def _encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def _signature(body, secret):
    return _encode(hmac.new(secret.encode(), _DOMAIN + body.encode(), hashlib.sha256).digest())


def mint_local_approval_capability(*, secret, ttl_s, now=None, **claims):
    issued = int(time.time()) if now is None else now
    capability = LocalApprovalCapability(**claims, issued_at=issued, expires_at=issued + max(1, ttl_s))
    if not _valid(capability, issued):
        raise ValueError('invalid local approval capability')
    body = _encode(json.dumps(asdict(capability), sort_keys=True, separators=(',', ':')).encode())
    token = body + '.' + _signature(body, secret)
    if len(token) > 16384:
        raise ValueError('local approval capability too large')
    return token


def verify_local_approval_capability(token, *, secret, now=None):
    if not isinstance(token, str) or not token or len(token) > 16384:
        return None
    try:
        body, signature = token.rsplit('.', 1)
        if not hmac.compare_digest(signature, _signature(body, secret)):
            return None
        value = LocalApprovalCapability(**json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4))))
        return value if _valid(value, int(time.time()) if now is None else now) else None
    except (ValueError, TypeError, KeyError, UnicodeError, AttributeError):
        return None
