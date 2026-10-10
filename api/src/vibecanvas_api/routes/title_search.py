"""Bounded, permission-scoped search over encrypted Project and Chat titles."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, field_validator

from ..auth.deps import AuthContext, current_user
from ..authorization.dependencies import context_for_auth, get_authz_service, principal_for_auth
from ..authorization.service import AuthzService
from ..authorization.types import Action, ResourceType
from ..storage.chat_repo import ChatRepo
from ..storage.chat_project_repo import ChatProjectRepo
from .deps import get_chat_project_repo, get_chat_repo

router = APIRouter(prefix="/api/v1/search", tags=["title-search"])


class TitleSearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    after_id: str = Field(default="", max_length=256)

    @field_validator("query")
    @classmethod
    def nonblank_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must contain non-whitespace characters")
        return value


class TitleSearchItem(BaseModel):
    id: str
    title: str
    project_id: str | None = None
    scope_id: str | None = None
    surface: str | None = None
    workflow_id: str | None = None


class TitleSearchPage(BaseModel):
    items: list[TitleSearchItem]
    scanned: int
    next_cursor: str | None


@router.post("/projects", response_model=TitleSearchPage)
async def search_project_titles(
    body: TitleSearchBody, response: Response,
    repo: ChatProjectRepo = Depends(get_chat_project_repo),
    auth: AuthContext = Depends(current_user),
):
    response.headers["Cache-Control"] = "no-store"
    return await repo.search_titles(body.query, body.after_id)


@router.post("/chats", response_model=TitleSearchPage)
async def search_chat_titles(
    body: TitleSearchBody, request: Request, response: Response,
    repo: ChatRepo = Depends(get_chat_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    response.headers["Cache-Control"] = "no-store"
    # VIEW_METADATA may allow inventory visibility without private title access.
    ids = await service.list_authorized_ids(
        principal_for_auth(auth), Action.VIEW, ResourceType.CHAT,
        context_for_auth(auth, request),
    )
    return await repo.search_titles(body.query, body.after_id, ids)
