"""Projection and control boundary for durable Runtime background jobs."""

from __future__ import annotations

import base64
import json

from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.storage.background_jobs_repo import (
    BackgroundJobsRepo,
    project_background_job,
)
from vibecanvas_api.storage.models_background_jobs import (
    ACTIVE_BACKGROUND_JOB_STATUSES,
)


async def list_background_jobs(
    session: AsyncSession,
    *,
    chat_id: str,
    creator_user_id: str,
    include_finished: bool,
    limit: int,
) -> list[dict]:
    page = await list_background_jobs_page(
        session,
        chat_id=chat_id,
        creator_user_id=creator_user_id,
        include_finished=include_finished,
        limit=limit,
        cursor=None,
    )
    return page["jobs"]


def _cursor_encode(item: dict, active_statuses: set[str]) -> str:
    raw = json.dumps(
        {
            "rank": 0 if item.get("status") in active_statuses else 1,
            "updated_at": str(item.get("updated_at") or ""),
            "job_id": str(item.get("job_id") or ""),
        },
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor_decode(cursor: str | None) -> tuple[int, str, str] | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode())
        return int(value["rank"]), str(value["updated_at"]), str(value["job_id"])
    except Exception as exc:
        raise ValueError("invalid background job cursor") from exc


async def list_background_jobs_page(
    session: AsyncSession,
    *,
    chat_id: str,
    creator_user_id: str,
    include_finished: bool,
    limit: int,
    cursor: str | None,
) -> dict:
    bounded = max(1, min(int(limit), 100))
    fetch_limit = min(200, bounded * 2 + 1)
    repo = BackgroundJobsRepo(session)
    await repo.reconcile_stale_for_chat(chat_id=chat_id)
    rows = await repo.list_for_user(
        chat_id=chat_id,
        creator_user_id=creator_user_id,
        statuses=None if include_finished else ACTIVE_BACKGROUND_JOB_STATUSES,
        limit=fetch_limit,
    )
    jobs = [project_background_job(row) for row in rows]
    active_statuses = set(ACTIVE_BACKGROUND_JOB_STATUSES)
    jobs.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    jobs.sort(key=lambda item: 0 if item.get("status") in active_statuses else 1)
    decoded = _cursor_decode(cursor)
    if decoded is not None:
        cursor_rank, cursor_updated, cursor_id = decoded
        jobs = [
            item
            for item in jobs
            if (
                (0 if item.get("status") in active_statuses else 1) > cursor_rank
                or (
                    (0 if item.get("status") in active_statuses else 1)
                    == cursor_rank
                    and (
                        str(item.get("updated_at") or "") < cursor_updated
                        or (
                            str(item.get("updated_at") or "") == cursor_updated
                            and str(item.get("job_id") or "") < cursor_id
                        )
                    )
                )
            )
        ]
    page = jobs[:bounded]
    return {
        "jobs": page,
        "next_cursor": (
            _cursor_encode(page[-1], active_statuses)
            if len(jobs) > bounded and page
            else None
        ),
    }


async def get_background_job(
    session: AsyncSession,
    *,
    chat_id: str,
    job_id: str,
    creator_user_id: str,
) -> dict | None:
    repo = BackgroundJobsRepo(session)
    await repo.reconcile_stale_for_chat(chat_id=chat_id)
    row = await repo.get_for_user(
        chat_id=chat_id,
        job_id=job_id,
        creator_user_id=creator_user_id,
    )
    return project_background_job(row) if row is not None else None


async def cancel_background_job(
    session: AsyncSession,
    *,
    chat_id: str,
    job_id: str,
    creator_user_id: str,
) -> dict | None:
    row = await BackgroundJobsRepo(session).request_cancel(
        chat_id=chat_id,
        job_id=job_id,
        creator_user_id=creator_user_id,
        reason="agent_requested",
    )
    return project_background_job(row) if row is not None else None
