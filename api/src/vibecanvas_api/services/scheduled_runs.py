"""Scheduled-run helpers shared by routes and background workers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from croniter import croniter


DEFAULT_NOTIFICATION_POLICY = {
    "enabled": True,
    "on": ["failed"],
    "channels": ["in_app"],
    "include_detail_link": True,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def compute_next_run_at(
    *,
    schedule_type: str,
    timezone_name: str,
    interval_seconds: int | None = None,
    cron_expr: str | None = None,
    start_at: datetime | None = None,
    base: datetime | None = None,
) -> datetime | None:
    """Return the next future fire time in UTC.

    V1 uses catchup=false. Callers should pass ``base=now`` after a dispatch so
    missed windows are not replayed.
    """
    now = ensure_utc(base) or utc_now()
    start = ensure_utc(start_at)
    if start and start > now:
        return start
    if schedule_type == "interval":
        seconds = int(interval_seconds or 0)
        if seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        return now + timedelta(seconds=seconds)
    if schedule_type == "cron":
        if not cron_expr:
            raise ValueError("cron_expr is required for cron schedules")
        tz = ZoneInfo(timezone_name or "UTC")
        local_base = now.astimezone(tz)
        next_local = croniter(cron_expr, start_time=local_base).get_next(ret_type=datetime)
        if next_local.tzinfo is None:
            next_local = next_local.replace(tzinfo=tz)
        return next_local.astimezone(timezone.utc)
    raise ValueError(f"unsupported schedule_type: {schedule_type}")


def merge_notification_policy(value: dict | None) -> dict:
    policy = dict(DEFAULT_NOTIFICATION_POLICY)
    if isinstance(value, dict):
        policy.update(value)
    on = policy.get("on")
    policy["on"] = [x for x in on if x in {"succeeded", "failed"}] if isinstance(on, list) else ["failed"]
    channels = policy.get("channels")
    policy["channels"] = [x for x in channels if isinstance(x, str)] if isinstance(channels, list) else ["in_app"]
    policy["enabled"] = bool(policy.get("enabled", True))
    policy["include_detail_link"] = bool(policy.get("include_detail_link", True))
    return policy


def schedule_to_out(schedule) -> dict:
    return {
        "id": str(schedule.id),
        "task_id": str(schedule.task_id),
        "workflow_id": schedule.workflow_id,
        "name": schedule.name,
        "enabled": schedule.enabled,
        "schedule_type": schedule.schedule_type,
        "cron_expr": schedule.cron_expr,
        "interval_seconds": schedule.interval_seconds,
        "timezone": schedule.timezone,
        "input_preset": schedule.input_preset,
        "mount_enabled": schedule.mount_enabled,
        "notification_policy": schedule.notification_policy,
        "concurrency_policy": schedule.concurrency_policy,
        "failure_policy": schedule.failure_policy,
        "catchup_policy": schedule.catchup_policy,
        "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
        "end_at": schedule.end_at.isoformat() if schedule.end_at else None,
        "last_run_at": schedule.last_run_at.isoformat() if schedule.last_run_at else None,
        "last_status": schedule.last_status,
        "created_at": schedule.created_at.isoformat() if schedule.created_at else None,
        "updated_at": schedule.updated_at.isoformat() if schedule.updated_at else None,
    }


def execution_to_out(execution) -> dict:
    return {
        "id": str(execution.id),
        "schedule_id": str(execution.schedule_id),
        "workflow_id": execution.workflow_id,
        "run_key": execution.run_key,
        "status": execution.status,
        "trigger_type": execution.trigger_type,
        "triggered_at": execution.triggered_at.isoformat() if execution.triggered_at else None,
        "started_at": execution.started_at.isoformat() if execution.started_at else None,
        "finished_at": execution.finished_at.isoformat() if execution.finished_at else None,
        "input_snapshot": execution.input_snapshot,
        "result": execution.result,
        "results_uri": execution.results_uri,
        "error": execution.error,
        "run_state": execution.run_state,
        "notification_state": execution.notification_state,
    }
