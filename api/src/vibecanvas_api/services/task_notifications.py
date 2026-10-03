"""Task notification configuration. Delivery is intentionally a no-op."""
from typing import Annotated
import re
from pydantic import BeforeValidator

DEFAULT_NOTIFICATION_POLICY = {"enabled": False, "on": [], "email": ""}


def normalize_notification_policy(value: dict | None) -> dict:
    value = value or {}
    if not isinstance(value, dict):
        raise ValueError("notification_policy must be an object")
    events = value.get("on", [])
    if not isinstance(events, list) or any(e not in ("succeeded", "failed") for e in events):
        raise ValueError("notification events must be succeeded or failed")
    enabled = bool(value.get("enabled", bool(events))) and bool(events)
    email = str(value.get("email") or "").strip()
    if enabled and (len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)):
        raise ValueError("A valid notification recipient email is required")
    return {"enabled": enabled, "on": list(dict.fromkeys(events)) if enabled else [], "email": email if enabled else ""}


NotificationPolicy = Annotated[dict, BeforeValidator(normalize_notification_policy)]


def notify_task_execution(*, policy: dict, status: str) -> None:
    pass


def notification_state(policy: dict, status: str) -> dict:
    if policy.get("enabled") and status in (policy.get("on") or []):
        notify_task_execution(policy=policy, status=status)
        return {"status": "skipped", "reason": "delivery_not_implemented"}
    return {"status": "skipped", "reason": "policy_not_matched"}
