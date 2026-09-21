"""Narrow, fail-closed HTTP client for the pinned OpenFGA control plane."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from .types import ConsistencyPreference


class OpenFgaUnavailableError(RuntimeError):
    """OpenFGA could not produce an authoritative decision."""

    def __init__(self, reason_code: str = "authorization_unavailable") -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class OpenFgaTuple:
    user: str
    relation: str
    object: str

    def as_json(self) -> dict[str, str]:
        return {
            "user": self.user,
            "relation": self.relation,
            "object": self.object,
        }


@dataclass(frozen=True, slots=True)
class OpenFgaReadPage:
    tuples: tuple[OpenFgaTuple, ...]
    continuation_token: str = ""


class OpenFgaHttpClient:
    """Own OpenFGA's wire contract without exposing it to product code."""

    MAX_BATCH_CHECKS = 50

    def __init__(
        self,
        *,
        api_url: str,
        store_id: str,
        authorization_model_id: str,
        api_token: str = "",
        timeout_seconds: float = 2.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_url or not store_id or not authorization_model_id:
            raise ValueError(
                "OpenFGA api_url, store_id, and authorization_model_id "
                "must be pinned"
            )
        self.api_url = api_url.rstrip("/")
        self.store_id = _safe_path_segment(store_id)
        self.authorization_model_id = _safe_path_segment(
            authorization_model_id
        )
        headers = {"accept": "application/json"}
        if api_token:
            headers["authorization"] = f"Bearer {api_token}"
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.api_url,
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def probe(self) -> None:
        """Verify both process health and the immutable pinned model."""
        await self._request("GET", "/healthz")
        await self._request(
            "GET",
            f"/stores/{self.store_id}/authorization-models/"
            f"{self.authorization_model_id}",
        )

    async def check(
        self,
        *,
        user: str,
        relation: str,
        object_: str,
        consistency: ConsistencyPreference,
    ) -> bool:
        payload = await self._request(
            "POST",
            f"/stores/{self.store_id}/check",
            json={
                "authorization_model_id": self.authorization_model_id,
                "tuple_key": {
                    "user": user,
                    "relation": relation,
                    "object": object_,
                },
                "consistency": consistency.value,
            },
        )
        allowed = payload.get("allowed")
        if not isinstance(allowed, bool):
            raise OpenFgaUnavailableError("authorization_invalid_response")
        return allowed

    async def batch_check(
        self,
        checks: Sequence[tuple[str, str, str]],
        *,
        consistency: ConsistencyPreference,
    ) -> tuple[bool, ...]:
        if not checks:
            return ()
        result: list[bool] = []
        for offset in range(0, len(checks), self.MAX_BATCH_CHECKS):
            chunk = checks[offset:offset + self.MAX_BATCH_CHECKS]
            request_checks = [
                {
                    "tuple_key": {
                        "user": user,
                        "relation": relation,
                        "object": object_,
                    },
                    "correlation_id": str(index + offset),
                }
                for index, (user, relation, object_) in enumerate(chunk)
            ]
            payload = await self._request(
                "POST",
                f"/stores/{self.store_id}/batch-check",
                json={
                    "authorization_model_id": self.authorization_model_id,
                    "checks": request_checks,
                    "consistency": consistency.value,
                },
            )
            # The server API names this map ``result`` (singular).  Client SDK
            # examples often expose a higher-level ``results`` collection, so
            # keep this parser pinned to the wire contract rather than an SDK
            # projection.
            raw_results = payload.get("result")
            if not isinstance(raw_results, dict):
                raise OpenFgaUnavailableError(
                    "authorization_invalid_response"
                )
            for index in range(offset, offset + len(chunk)):
                item = raw_results.get(str(index))
                if not isinstance(item, dict) or "error" in item:
                    raise OpenFgaUnavailableError(
                        "authorization_check_failed"
                    )
                allowed = item.get("allowed")
                if not isinstance(allowed, bool):
                    raise OpenFgaUnavailableError(
                        "authorization_invalid_response"
                    )
                result.append(allowed)
        return tuple(result)

    async def list_objects(
        self,
        *,
        user: str,
        relation: str,
        object_type: str,
        consistency: ConsistencyPreference,
    ) -> tuple[str, ...]:
        payload = await self._request(
            "POST",
            f"/stores/{self.store_id}/list-objects",
            json={
                "authorization_model_id": self.authorization_model_id,
                "type": object_type,
                "relation": relation,
                "user": user,
                "consistency": consistency.value,
            },
        )
        objects = payload.get("objects")
        if not isinstance(objects, list) or any(
            not isinstance(value, str) for value in objects
        ):
            raise OpenFgaUnavailableError("authorization_invalid_response")
        prefix = f"{object_type}:"
        if any(not value.startswith(prefix) for value in objects):
            raise OpenFgaUnavailableError("authorization_invalid_response")
        return tuple(value[len(prefix):] for value in objects)

    async def read(
        self,
        *,
        tuple_key: OpenFgaTuple,
        continuation_token: str = "",
        page_size: int = 100,
        consistency: ConsistencyPreference = (
            ConsistencyPreference.HIGHER_CONSISTENCY
        ),
    ) -> OpenFgaReadPage:
        body: dict[str, Any] = {
            "tuple_key": {
                key: value
                for key, value in tuple_key.as_json().items()
                if value
            },
            "page_size": max(1, min(page_size, 100)),
            "consistency": consistency.value,
        }
        if continuation_token:
            body["continuation_token"] = continuation_token
        payload = await self._request(
            "POST",
            f"/stores/{self.store_id}/read",
            json=body,
        )
        raw_tuples = payload.get("tuples")
        if not isinstance(raw_tuples, list):
            raise OpenFgaUnavailableError("authorization_invalid_response")
        tuples: list[OpenFgaTuple] = []
        for raw in raw_tuples:
            key = raw.get("key") if isinstance(raw, dict) else None
            if not isinstance(key, dict):
                raise OpenFgaUnavailableError(
                    "authorization_invalid_response"
                )
            values = (key.get("user"), key.get("relation"), key.get("object"))
            if any(not isinstance(value, str) for value in values):
                raise OpenFgaUnavailableError(
                    "authorization_invalid_response"
                )
            tuples.append(OpenFgaTuple(*values))
        token = payload.get("continuation_token") or ""
        if not isinstance(token, str):
            raise OpenFgaUnavailableError("authorization_invalid_response")
        return OpenFgaReadPage(tuple(tuples), token)

    async def write(
        self,
        *,
        writes: Iterable[OpenFgaTuple] = (),
        deletes: Iterable[OpenFgaTuple] = (),
    ) -> None:
        write_items = tuple(writes)
        delete_items = tuple(deletes)
        if not write_items and not delete_items:
            return
        if len(write_items) + len(delete_items) > 100:
            raise ValueError("OpenFGA write supports at most 100 tuples")
        body: dict[str, Any] = {
            "authorization_model_id": self.authorization_model_id,
        }
        if write_items:
            body["writes"] = {
                "tuple_keys": [item.as_json() for item in write_items]
            }
        if delete_items:
            body["deletes"] = {
                "tuple_keys": [item.as_json() for item in delete_items]
            }
        await self._request(
            "POST",
            f"/stores/{self.store_id}/write",
            json=body,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, json=json)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise OpenFgaUnavailableError() from exc
        if response.status_code >= 400:
            # The response may contain model/tuple details. Preserve only a
            # stable availability category and never propagate the body.
            reason = (
                "authorization_unavailable"
                if response.status_code >= 500
                else "authorization_configuration_error"
            )
            raise OpenFgaUnavailableError(reason)
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise OpenFgaUnavailableError(
                "authorization_invalid_response"
            ) from exc
        if not isinstance(payload, dict):
            raise OpenFgaUnavailableError("authorization_invalid_response")
        return payload


def openfga_client_from_config() -> OpenFgaHttpClient:
    """Build the pinned client used outside the FastAPI lifespan.

    Background workers and administrative commands do not have ``app.state``.
    Keeping their construction here prevents those processes from silently
    selecting a different store/model or interpreting bootstrap files.
    """
    from vibecanvas_api.config import config

    return OpenFgaHttpClient(
        api_url=config.openfga_api_url,
        store_id=config.openfga_store_id,
        authorization_model_id=config.openfga_authorization_model_id,
        api_token=config.openfga_api_token,
        timeout_seconds=config.openfga_timeout_seconds,
    )


def _safe_path_segment(value: str) -> str:
    if (
        not value
        or "/" in value
        or "\\" in value
        or "?" in value
        or "#" in value
        or len(value) > 128
    ):
        raise ValueError("OpenFGA identifier is invalid")
    return value
