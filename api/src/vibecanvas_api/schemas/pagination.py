"""Generic limit/offset paginated response wrapper.

Spec §3.6 + P2.2.8 — every list endpoint paginates from day 1 to
match common GitHub / Dify / LangFlow conventions.

Usage in route handler:

    @router.get("/things", response_model=Page[ThingOut])
    async def list_things(page: PageRequest = Depends()):
        items, total = repo.list_things(limit=page.limit, offset=page.offset)
        return Page(items=items, total=total, limit=page.limit, offset=page.offset)
"""

from __future__ import annotations

from typing import Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel, Field, ConfigDict

T = TypeVar("T")


class PageRequest(BaseModel):
    """Query-param dependency for paginated list endpoints."""
    limit: int = Field(50, ge=1, le=500)
    offset: int = Field(0, ge=0)

    @classmethod
    def as_query(
        cls,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> "PageRequest":
        return cls(limit=limit, offset=offset)


class Page(BaseModel, Generic[T]):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    items: list[T]
    total: int
    limit: int
    offset: int
