"""Name -> ToolSpec lookup, and the OpenAI `tools=[...]` payload builder."""

from __future__ import annotations

from collections.abc import Callable

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

    def filtered(self, predicate: Callable[[ToolSpec], bool]) -> ToolRegistry:
        """A new registry containing only the tools predicate(tool) accepts —
        the shared subsetting primitive behind spawn_subagent's allowed_tools,
        plan mode's tool filtering, and agent tools' fixed allowed-tool sets."""
        subset = ToolRegistry()
        for tool in self:
            if predicate(tool):
                subset.register(tool)
        return subset

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
    from pcli.tools.agent_tools import (
        DATA_ANALYSIS,
        DEEP_RESEARCH,
        EXPLORE_CODEBASE,
        EXPLORE_FILES,
        EXPLORE_LOGS,
        VERIFY_COMPUTATION,
        WRITE_DOCUMENTATION,
    )
    from pcli.tools.builtin.agent_tool_register_tool import REGISTER_AGENT_TOOL
    from pcli.tools.builtin.artifact_tool import FETCH_ARTIFACT
    from pcli.tools.builtin.ask_tool import ASK_USER_QUESTION
    from pcli.tools.builtin.decision_tool import RECORD_DECISION
    from pcli.tools.builtin.diff_tools import APPLY_PATCH, DIFF_FILES
    from pcli.tools.builtin.fs_tools import EDIT_FILE, GLOB_SEARCH, LIST_DIR, READ_FILE, WRITE_FILE
    from pcli.tools.builtin.grep_tool import GREP
    from pcli.tools.builtin.memory_tool import REMEMBER
    from pcli.tools.builtin.network_tools import DOWNLOAD_FILE
    from pcli.tools.builtin.pip_tool import PIP_INSTALL
    from pcli.tools.builtin.shell_tool import (
        READ_BACKGROUND_OUTPUT,
        RUN_SHELL,
        RUN_SHELL_BACKGROUND,
        STOP_BACKGROUND_PROCESS,
    )
    from pcli.tools.builtin.subagent_tool import SPAWN_SUBAGENT
    from pcli.tools.builtin.todo_tool import WRITE_TODOS
    from pcli.tools.builtin.toolbox_register_tool import REGISTER_TOOLBOX_TOOL
    from pcli.tools.builtin.web_tools import WEB_FETCH, WEB_SEARCH
    from pcli.tools.pydiscovery.invoke import CALL_PYTHON
    from pcli.tools.pydiscovery.search import INSPECT_PYTHON_MODULE, SEARCH_PYTHON

    registry = ToolRegistry()
    for tool in (
        READ_FILE,
        WRITE_FILE,
        EDIT_FILE,
        LIST_DIR,
        GLOB_SEARCH,
        GREP,
        DOWNLOAD_FILE,
        WEB_FETCH,
        WEB_SEARCH,
        DIFF_FILES,
        APPLY_PATCH,
        RUN_SHELL,
        RUN_SHELL_BACKGROUND,
        READ_BACKGROUND_OUTPUT,
        STOP_BACKGROUND_PROCESS,
        PIP_INSTALL,
        SEARCH_PYTHON,
        INSPECT_PYTHON_MODULE,
        CALL_PYTHON,
        SPAWN_SUBAGENT,
        ASK_USER_QUESTION,
        WRITE_TODOS,
        RECORD_DECISION,
        REMEMBER,
        FETCH_ARTIFACT,
        REGISTER_TOOLBOX_TOOL,
        EXPLORE_CODEBASE,
        EXPLORE_FILES,
        EXPLORE_LOGS,
        WRITE_DOCUMENTATION,
        VERIFY_COMPUTATION,
        DEEP_RESEARCH,
        DATA_ANALYSIS,
        REGISTER_AGENT_TOOL,
    ):
        registry.register(tool)
    return registry
