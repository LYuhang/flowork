"""todo tool — Runtime-neutral adapter for the backend-owned Chat task list.

The current backend snapshot is copied into AgentContext at the Turn boundary.
Every mutation emits a complete ``todo_update`` projection which is committed
to the Chat database before the frontend observes it. Runtime-native state
does not own or duplicate the list.

Three statuses form the stable cross-Runtime task contract:
    pending → in_progress → done
"""
from __future__ import annotations

import asyncio

from vibecanvas_api.services.platform_mcp.tool_runtime import ToolRuntime, tool

from vibecanvas_api.agents.tools.decorator import ToolError, tool_output
from vibecanvas_api.agents.tools.render import Rendered, register_render

_STATUS_ICON = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
_VALID_OPS = ("list", "add", "start", "done", "remove", "clear_done")


def _format(items: list[dict]) -> str:
    if not items:
        return "(empty)"
    return "\n".join(
        f"{_STATUS_ICON.get(i['status'], '[ ]')} {i['id']}. {i['text']}"
        for i in items
    )


def _clear_if_all_done(items: list[dict]) -> list[dict]:
    if items and all(i.get("status") == "done" for i in items):
        return []
    return items


# ---------------------------------------------------------------------------
# Three-layer pattern
# ---------------------------------------------------------------------------

@register_render("todo")
def _render_todo(raw: dict, ctx) -> Rendered:
    items = raw.get("items", [])
    pending = sum(1 for i in items if i["status"] == "pending")
    in_prog = sum(1 for i in items if i["status"] == "in_progress")
    done_n = sum(1 for i in items if i["status"] == "done")
    abstract = f"todo → {pending} pending, {in_prog} in progress, {done_n} done"
    return Rendered(
        content=_format(items),
        content_type="text/plain",
        abstract=abstract,
        extras={"todo_items": items},
    )


@tool_output(content_type="text/plain", tool="todo")
async def _do_todo(op: str, text: str, id: int, runtime: ToolRuntime) -> dict:
    def _run():
        ctx = runtime.context
        items: list[dict] = list(ctx.todo_items)  # work on a copy

        if op == "list":
            pass

        elif op == "add":
            if not text.strip():
                raise ToolError("bad_input", "text is required for 'add'")
            next_id = max((i["id"] for i in items), default=0) + 1
            items.append({"id": next_id, "text": text.strip(), "status": "pending"})
            ctx.todo_items = items

        elif op == "start":
            item = next((i for i in items if i["id"] == id), None)
            if item is None:
                raise ToolError("not_found", f"no task with id={id}")
            item["status"] = "in_progress"
            ctx.todo_items = items

        elif op == "done":
            item = next((i for i in items if i["id"] == id), None)
            if item is None:
                raise ToolError("not_found", f"no task with id={id}")
            item["status"] = "done"
            items = _clear_if_all_done(items)
            ctx.todo_items = items

        elif op == "remove":
            new_items = [i for i in items if i["id"] != id]
            if len(new_items) == len(items):
                raise ToolError("not_found", f"no task with id={id}")
            ctx.todo_items = new_items
            items = new_items

        elif op == "clear_done":
            items = [i for i in items if i["status"] != "done"]
            ctx.todo_items = items

        else:
            raise ToolError("bad_op", f"unknown op '{op}'; valid: {', '.join(_VALID_OPS)}")

        return {"items": list(ctx.todo_items)}

    try:
        return await asyncio.to_thread(_run)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError("todo_failed", str(exc))


@tool(response_format="content_and_artifact")
async def todo(
    op: str,
    text: str = "",
    id: int = 0,
    *,
    runtime: ToolRuntime,
) -> str:
    """Manage your task list for the current job.

    The list persists across dialogue turns in the backend Chat state — tasks
    survive Runtime restarts, context compaction, and frontend reconnects. Use this
    alongside `/memory/state.md`: that file holds the overall goal and next
    step; the todo list holds the discrete subtask breakdown.

    Use todos for multi-step work:
        1. Add tasks when you identify a non-trivial plan.
        2. Mark one task as "in_progress" before working on it.
        3. As soon as you complete that task, immediately call
           todo(op="done", id=...) before starting or continuing another task.
           Do not batch completed-item updates until the end; each completion
           must be reflected right away so the user sees live progress.
        4. Update the tool state; do not only describe progress in text.
        5. When every item is done, the current todo state is cleared and the
           frontend progress dock disappears.

    Args:
        op: operation to perform. One of:
            "list"       — return the current task list (no other args needed).
            "add"        — append a new task in "pending" status.
                           Requires: text (the task description).
            "start"      — mark a task as "in_progress".
                           Requires: id.
            "done"       — mark a task as "done".
                           Requires: id.
            "remove"     — delete a task by id.
                           Requires: id.
            "clear_done" — remove all "done" tasks (compacts the list).
        text: task description, for "add" only.
        id:   task id, for "start" / "done" / "remove".

    Status lifecycle:
        pending  →  start  →  in_progress  →  done  →  (clear_done removes)
        [ ]                   [~]              [x]

    Returns:
        content = the full task list rendered as checkbox lines.
        abstract = "todo → N pending, M in progress, K done".

    Examples:
        todo(op="add", text="Add StartNode to canvas")
        todo(op="add", text="Write CSV parser logic")
        todo(op="start", id=1)
        todo(op="done", id=1)
        todo(op="clear_done")
    """
    return await _do_todo(op, text, id, runtime)
