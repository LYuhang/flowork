"""Logical Workflow storage binding shared by host runtime protocols."""
from pydantic import BaseModel, ConfigDict, Field


class WorkflowRunSource(BaseModel):
    """Authorized logical source; sandboxd alone resolves its filesystem path."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    workflow_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
