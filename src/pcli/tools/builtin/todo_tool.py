"""write_todos: lets the LLM maintain a structured task list for the current
session. Each call replaces the whole list (like Claude Code's own TodoWrite)
rather than applying incremental deltas — simpler for the model to get right
and impossible to desync. The list lives on Session.todos, so it persists,
exports/imports, and survives resuming a session, same as everything else."""

from __future__ import annotations

import difflib

from pcli.session.models import TodoItem
from pcli.tools.base import ToolContext, ToolResult, ToolSpec

_STATUS_ICONS = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}

# Below this similarity ratio, a new todo's content is treated as a
# different task from an old one, not a reworded version of the same task —
# see _dropped_completed_items. Threshold picked to tolerate ordinary
# rewording (e.g. tightening a task's phrasing as it's understood better)
# while still catching wholesale replacement with an unrelated task.
_SIMILARITY_THRESHOLD = 0.45


def render_todos(todos: list[TodoItem]) -> str:
    if not todos:
        return "Todo list is empty."
    lines = [f"{_STATUS_ICONS.get(t.status, '[ ]')} {t.content}" for t in todos]
    return "\n".join(lines)


def _still_represented(old_content: str, new_contents: list[str]) -> bool:
    return any(
        difflib.SequenceMatcher(None, old_content.lower(), nc.lower()).ratio() >= _SIMILARITY_THRESHOLD
        for nc in new_contents
    )


def _dropped_completed_items(old: list[TodoItem], new: list[TodoItem]) -> list[TodoItem]:
    """Old 'completed' items with no reasonably-similar counterpart in the
    new list — a real debugged case: a model silently replaced a todo list
    that had 4 completed items (real, already-verified work) with 3 brand
    new pending ones for a superficially similar but different task,
    without any explanation, and proceeded to redo the finished work. This
    doesn't block the update (a genuine restart is sometimes correct) — it
    just surfaces the loss so the model can catch its own mistake."""
    new_contents = [t.content for t in new]
    return [t for t in old if t.status == "completed" and not _still_represented(t.content, new_contents)]


async def _write_todos(arguments: dict, ctx: ToolContext) -> ToolResult:
    if ctx.session is None:
        return ToolResult(output="No session available to store todos in.", is_error=True)

    raw_todos = arguments.get("todos")
    if not isinstance(raw_todos, list):
        return ToolResult(output="'todos' must be a list.", is_error=True)

    try:
        todos = [TodoItem(content=item["content"], status=item.get("status", "pending")) for item in raw_todos]
    except (KeyError, TypeError) as exc:
        return ToolResult(output=f"Invalid todo entry: {exc}", is_error=True)

    in_progress_count = sum(1 for t in todos if t.status == "in_progress")
    if in_progress_count > 1:
        return ToolResult(
            output="Only one todo can be 'in_progress' at a time — mark the others "
            "'pending' or 'completed'.",
            is_error=True,
        )

    dropped = _dropped_completed_items(ctx.session.todos, todos)
    ctx.session.todos = todos
    output = f"Todo list updated:\n{render_todos(todos)}"
    if dropped:
        lost = "; ".join(f'"{t.content}"' for t in dropped[:5])
        output += (
            f"\n\n[pcli] Note: {len(dropped)} previously completed item(s) no longer appear "
            f"in this list ({lost}). If you're deliberately restarting or changing approach, "
            "that's fine — record_decision helps track why. If not, the underlying work may "
            "already be done and doesn't need redoing."
        )
    return ToolResult(output=output)


WRITE_TODOS = ToolSpec(
    name="write_todos",
    description="Create or update your task list for this session. Submit the FULL current "
    "list every time (this replaces it, it doesn't append). Use this for any multi-step "
    "task: write the plan as pending todos before starting, mark exactly one 'in_progress' "
    "while working on it, mark it 'completed' immediately when done, then move to the next. "
    "This keeps the user able to see your progress and keeps you from losing track of steps.",
    parameters={
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "The task, in imperative form."},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                    },
                    "required": ["content", "status"],
                },
            }
        },
        "required": ["todos"],
    },
    handler=_write_todos,
    needs_permission=False,
)
