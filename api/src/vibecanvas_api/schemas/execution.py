"""Execution-related schemas."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID
from pydantic import BaseModel, Field


class ExecutionRequest(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)
    resume_from: UUID | None = None


class ResumeCheckRequest(BaseModel):
    workflow: dict[str, Any]
    input: dict[str, Any] = Field(default_factory=dict)


class NodeExecutionRequest(BaseModel):
    """Body for single-node debug execution.

    The node comes from the request body — the UNSAVED *draft* node the
    user is editing in the inspector, NOT ``get_current_workflow`` (the
    committed snapshot). Node-debug is on the in-flight node, so the
    frontend ships the full ``node_dict`` it has locally. ``input`` is the
    node's inputs supplied directly (``run_node`` does no reference
    resolution — this is the isolated node-debug surface).
    """
    node: dict[str, Any]
    input: dict[str, Any] = Field(default_factory=dict)


class ExecutionStatusOut(BaseModel):
    exec_id: str
    history_id: str | None = None
    wf_id: str
    status: Literal["running", "completed", "stopped", "error"]
    started_at: float
    finished_at: float | None = None
    result: dict | None = None
    error: str | None = None


class ExecutionListItem(BaseModel):
    exec_id: str
    wf_id: str
    status: str
    started_at: float
    finished_at: float | None = None
