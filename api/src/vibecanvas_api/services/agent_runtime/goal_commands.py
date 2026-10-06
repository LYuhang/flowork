"""Explicit user goal controls, separate from sticky domain commands."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GoalCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["new", "edit", "resume", "status", "clear"]
    objective: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def validate_objective(self) -> "GoalCommand":
        if self.action in {"new", "edit"}:
            if not self.objective or not self.objective.strip():
                raise ValueError("goal_objective_required")
            self.objective = self.objective.strip()
        elif self.objective is not None:
            raise ValueError("goal_control_does_not_accept_objective")
        return self


def parse_goal_command(content: str) -> GoalCommand | None:
    parts = content.lstrip().split(None, 1)
    if not parts:
        return None
    token = parts[0]
    if token != "/goal" and not token.startswith("/goal:"):
        return None
    objective = parts[1].strip() if len(parts) == 2 else None
    if token == "/goal":
        return GoalCommand(action="new" if objective else "status", objective=objective)
    action = token.partition(":")[2]
    if action == "new":
        raise ValueError("Use /goal followed by the objective")
    return GoalCommand(action=action, objective=objective)
