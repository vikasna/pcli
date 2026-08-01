"""Name -> ToolSpec lookup, and the OpenAI `tools=[...]` payload builder."""

from __future__ import annotations

from pcli.llm.models import ToolDefinition
from pcli.tools.base import ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, tool: ToolSpec) -> None:
        self._tools[tool.name] = tool

    def merge(self, other: ToolRegistry) -> None:
        for tool in other:
            self.register(tool)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def to_openai_tools(self) -> list[ToolDefinition]:
        return [tool.to_openai_tool() for tool in self._tools.values()]

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __iter__(self):
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)


def build_default_registry() -> ToolRegistry:
    from pcli.tools.builtin.artifact_tool import FETCH_ARTIFACT
    from pcli.tools.builtin.fs_tools import GLOB_SEARCH, LIST_DIR, READ_FILE, WRITE_FILE
    from pcli.tools.builtin.grep_tool import GREP
    from pcli.tools.builtin.shell_tool import RUN_SHELL
    from pcli.tools.builtin.subagent_tool import SPAWN_SUBAGENT
    from pcli.tools.builtin.todo_tool import WRITE_TODOS
    from pcli.tools.pydiscovery.invoke import CALL_PYTHON
    from pcli.tools.pydiscovery.search import INSPECT_PYTHON_MODULE, SEARCH_PYTHON

    registry = ToolRegistry()
    for tool in (
        READ_FILE,
        WRITE_FILE,
        LIST_DIR,
        GLOB_SEARCH,
        GREP,
        RUN_SHELL,
        SEARCH_PYTHON,
        INSPECT_PYTHON_MODULE,
        CALL_PYTHON,
        SPAWN_SUBAGENT,
        WRITE_TODOS,
        FETCH_ARTIFACT,
    ):
        registry.register(tool)
    return registry
