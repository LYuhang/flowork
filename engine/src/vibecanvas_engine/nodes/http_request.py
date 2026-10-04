# -*- coding: utf-8 -*-
"""HTTPRequestNode — send HTTP requests to external REST APIs."""

import re
import asyncio
import base64
import jsonschema
from copy import deepcopy

from ..utils import safe_call_with_args
from ..register import node_registry
from .base import BaseNode


# Engine hard-coded default request timeout (seconds). Per-workflow
# ``__meta__.settings.timeouts.http`` overrides the per-instance default;
# a per-node ``node_config["timeout"]`` still wins over that.
DEFAULT_HTTP_TIMEOUT: float = 30.0
_HTTP_TIMEOUT = DEFAULT_HTTP_TIMEOUT


@node_registry.register()
class HTTPRequestNode(BaseNode):
    """Send HTTP requests to external REST APIs with configurable method, headers, body, and auth."""

    # Engine dispatch uses call_async; the sync entry is for direct callers.


    # Class-level fallback so ``__new__``-constructed instances still resolve a
    # timeout; ``__init__`` shadows it per-instance, ``Workflow.__init__`` may
    # override that from ``__meta__.settings.timeouts.http``.
    _default_timeout: float = _HTTP_TIMEOUT

    CONFIG_SCHEMA = {
        "type": "object",
        "required": ["method", "url"],
        "properties": {
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "DELETE"],
                "description": "HTTP method."
            },
            "url": {
                "type": "string",
                "description": "Request URL. Supports {{field_name}} interpolation."
            },
            "headers": {
                "type": "object",
                "description": "(Optional) Custom request headers.",
                "additionalProperties": {"type": "string"}
            },
            "body": {
                "type": "object",
                "description": "(Optional) Request body configuration.",
                "required": ["content"],
                "properties": {
                    "format": {
                        "type": "string",
                        "enum": ["json", "form"]
                    },
                    "content": {
                        "description": "Body content. Dict/string for json, key-value for form."
                    }
                },
                "additionalProperties": False
            },
            "auth": {
                "type": "object",
                "description": "(Optional) Quick auth configuration.",
                "required": ["type"],
                "properties": {
                    "type": {"type": "string", "enum": ["bearer", "api_key", "basic"]},
                    "token": {"type": "string"},
                    "key": {"type": "string"},
                    "header_name": {"type": "string"},
                    "username": {"type": "string"},
                    "password": {"type": "string"}
                },
                "additionalProperties": False
            },
            "timeout": {
                "type": "number",
                "minimum": 1,
                "description": "(Optional) Per-request timeout in seconds; omit to use the default."
            }
        },
        "additionalProperties": False
    }

    AGENT_SPEC = {
        "summary": "Call an external HTTP API and expose the response body, status code, and response headers to later nodes.",
        "when_to_use": "Use for REST APIs, webhooks, third-party services, or fetching remote data when the workflow needs a real HTTP call.",
        "when_not_to_use": "For LLM calls use PromptNode. For local data processing use CodeNode or TransformNode.",
        "constraints": [
            "Use node_type='HTTPRequestNode' and at most one child.",
            "method must be GET, POST, PUT, or DELETE; body is allowed only for POST/PUT.",
            "url, headers, auth fields, and string body leaves support {{field_name}} interpolation from input_fields.",
            "For JSON/Form request bodies, put structured content in body.content and interpolate only the string leaves that come from inputs.",
            "auth is optional: bearer requires token, api_key requires key, and basic requires username plus password. Explicit headers override auth-generated headers.",
            "HTTP 4xx/5xx responses do not fail the node. Inspect status_code downstream, often with ConditionNode, before trusting response_body.",
            "output_fields must be exactly response_body, status_code, and response_headers."
        ],
        "config_guide": {
            "method": "HTTP method: GET, POST, PUT, or DELETE.",
            "url": "Full URL. Use {{field_name}} to insert input values, e.g. https://api.example.com/users/{{user_id}}.",
            "headers": "(Optional) Custom headers as key-value pairs. Values support {{field_name}} interpolation.",
            "body": "(Optional, POST/PUT only) Object {format: 'json'|'form', content}. format defaults to 'json'.",
            "auth": "(Optional) Quick auth. type='bearer': token. type='api_key': key and optional header_name (default X-API-Key). type='basic': username and password.",
            "timeout": "(Optional) Per-request timeout in seconds; omit to use the default."
        },
        "examples": [
            {
                "scenario": "POST JSON body to an authenticated API",
                "node_dict": {
                    "node_id": "node_3",
                    "node_name": "create_record",
                    "node_type": "HTTPRequestNode",
                    "node_description": "Create a new record via API",
                    "input_fields": {
                        "name": {"type": "string", "value": "", "reference": "__start__.name"},
                        "email": {"type": "string", "value": "", "reference": "__start__.email"},
                        "api_key": {"type": "string", "value": "", "reference": "__start__.api_key"}
                    },
                    "output_fields": {
                        "response_body": {"type": "object", "description": "API response body"},
                        "status_code": {"type": "integer", "description": "HTTP status code"},
                        "response_headers": {"type": "object", "description": "Response headers"}
                    },
                    "node_config": {
                        "method": "POST",
                        "url": "https://api.example.com/records",
                        "headers": {"Content-Type": "application/json"},
                        "body": {"format": "json", "content": {"name": "{{name}}", "email": "{{email}}"}},
                        "auth": {"type": "api_key", "key": "{{api_key}}", "header_name": "X-API-Key"}
                    },
                    "children": ["node_4"],
                    "__attributes__": {"x": 200, "y": 0}
                }
            }
        ],
        "display": {
            "name": {"en": "HTTPRequestNode", "zh": "HTTP请求节点"},
            "description": {"en": "Send HTTP requests to external APIs", "zh": "向外部 API 发送 HTTP 请求"},
            "icon": "http",
            "category": {"en": "External Services", "zh": "外部服务"},
        }
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Per-workflow default request timeout; overridden by
        # ``Workflow.__init__`` from ``__meta__.settings.timeouts.http``.
        # A per-node ``node_config["timeout"]`` still wins (see ``__call__``).
        self._default_timeout: float = _HTTP_TIMEOUT

    @staticmethod
    @safe_call_with_args(prefix="[HTTPRequestNode Check]: ")
    def check(node_dict: dict) -> bool:

        jsonschema.validate(instance=node_dict, schema=BaseNode.GENERAL_NODE_SCHEMA)

        specific_schema = deepcopy(BaseNode.GENERAL_NODE_SCHEMA)
        specific_schema["properties"]["node_config"] = HTTPRequestNode.CONFIG_SCHEMA
        jsonschema.validate(instance=node_dict, schema=specific_schema)

        jsonschema.validate(instance=node_dict, schema={
            "type": "object",
            "properties": {
                "node_type": {"const": "HTTPRequestNode"},
                "children": {"type": "array", "maxItems": 1}
            }
        })

        output_fields = node_dict.get("output_fields", {})
        expected_outputs = {"response_body", "status_code", "response_headers"}
        assert set(output_fields.keys()) == expected_outputs, (
            "For HTTPRequestNode, output_fields must be exactly "
            "'response_body', 'status_code', and 'response_headers'."
        )
        assert output_fields["response_body"].get("type") in {"object", "string"}, (
            "For HTTPRequestNode, output_fields.response_body type must be "
            "'object' or 'string'."
        )
        assert output_fields["status_code"].get("type") == "integer", (
            "For HTTPRequestNode, output_fields.status_code type must be 'integer'."
        )
        assert output_fields["response_headers"].get("type") == "object", (
            "For HTTPRequestNode, output_fields.response_headers type must be 'object'."
        )

        config = node_dict["node_config"]
        method = config["method"]
        assert not (method in {"GET", "DELETE"} and config.get("body") is not None), (
            "For HTTPRequestNode, body is only allowed for POST/PUT. "
            f"Remove body or change method; current method is {method}."
        )

        auth = config.get("auth") or {}
        auth_type = auth.get("type")
        if auth_type == "bearer":
            assert auth.get("token"), "For HTTPRequestNode auth.type='bearer', auth.token is required."
        elif auth_type == "api_key":
            assert auth.get("key"), "For HTTPRequestNode auth.type='api_key', auth.key is required."
        elif auth_type == "basic":
            assert auth.get("username") and auth.get("password"), (
                "For HTTPRequestNode auth.type='basic', auth.username and "
                "auth.password are required."
            )

    @staticmethod
    def _interpolate(template_str: str, inputs: dict) -> str:
        """Replace {{field_name}} placeholders with input values."""
        def _replace(match):
            key = match.group(1).strip()
            if key in inputs:
                val = inputs[key]
                return str(val) if not isinstance(val, (dict, list)) else str(val)
            return match.group(0)
        return re.sub(r"\{\{(.*?)\}\}", _replace, str(template_str))

    @staticmethod
    def _build_auth_headers(auth: dict, inputs: dict) -> dict:
        """Convert auth config into HTTP headers."""
        if not auth or not auth.get("type"):
            return {}
        auth_type = auth["type"]
        if auth_type == "bearer":
            token = HTTPRequestNode._interpolate(auth.get("token", ""), inputs)
            return {"Authorization": f"Bearer {token}"}
        elif auth_type == "api_key":
            key = HTTPRequestNode._interpolate(auth.get("key", ""), inputs)
            header_name = auth.get("header_name", "X-API-Key")
            return {header_name: key}
        elif auth_type == "basic":
            username = HTTPRequestNode._interpolate(auth.get("username", ""), inputs)
            password = HTTPRequestNode._interpolate(auth.get("password", ""), inputs)
            encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
            return {"Authorization": f"Basic {encoded}"}
        return {}

    @safe_call_with_args(prefix="[HTTPRequestNode Call]: ")
    async def call_async(self, inputs: dict, previous_outputs: dict, extra: dict = None) -> dict:
        stop_event = (extra or {}).get("stop_event")
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError("HTTPRequestNode cancelled.")

        config = self.node_config
        method = config["method"].upper()
        url = self._interpolate(config["url"], inputs)
        timeout = float(config.get("timeout", self._default_timeout))

        # Build headers: auth first, then explicit headers override
        headers = self._build_auth_headers(config.get("auth", {}), inputs)
        for k, v in (config.get("headers") or {}).items():
            headers[k] = self._interpolate(v, inputs)

        # Build body
        kwargs = {"headers": headers, "timeout": timeout}
        body_config = config.get("body")
        if body_config and method in ("POST", "PUT"):
            fmt = body_config.get("format", "json")
            content = body_config.get("content", {})
            if isinstance(content, str):
                content = self._interpolate(content, inputs)
            elif isinstance(content, dict):
                content = {k: self._interpolate(str(v), inputs) if isinstance(v, str) else v
                           for k, v in content.items()}
            if fmt == "json":
                kwargs["json"] = content
            elif fmt == "form":
                kwargs["data"] = content

        if stop_event is not None and stop_event.is_set():
            raise RuntimeError("HTTPRequestNode cancelled before sending.")

        import httpx

        # A scoped client closes its transport on success, failure and cancel.
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.request(method, url, **kwargs)

        # Parse response
        try:
            response_body = resp.json()
        except Exception:
            response_body = resp.text

        return {
            "response_body": response_body,
            "status_code": resp.status_code,
            "response_headers": dict(resp.headers),
        }

    def __call__(self, inputs: dict, previous_outputs: dict, extra: dict = None) -> dict:
        return asyncio.run(self.call_async(inputs, previous_outputs, extra))
