"""Coverage for ChatScreen._replay_message_history - reconstructing the
visible transcript from session.messages on mount/resume using the same
rendering helpers a live turn uses, rather than the previous flat, lossy
dump. Regression coverage for a real reported gap: a tool-call-only
assistant turn (content=None) used to vanish entirely, tool results were
hard-truncated to 500 chars with no collapsible/syntax rendering, and a
mid-conversation system message (notably a compaction summary) wasn't
shown at all."""

import json
from pathlib import Path

import pytest
from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text
from textual.app import App
from textual.widgets import Collapsible, Static

from pcli.config.settings import Settings
from pcli.llm.models import ToolCall, ToolCallFunction
from pcli.session.models import Message
from pcli.session.store import SessionStore
from pcli.tui.screens.chat import ChatScreen
from pcli.tui.widgets.message_view import MessageView


def _texts_in(widget: Static) -> list[str]:
    """Flattens a Static's rendered Group into plain strings - add_message
    renders role="user"/"system"/"decision" bodies as a Markdown object
    (not a plain Text), so a check must look inside both to actually prove
    something is (or isn't) present, rather than only matching the bold
    role-label Text and silently never finding real content."""
    content = widget.content
    if not isinstance(content, Group):
        return []
    texts = []
    for part in content.renderables:
        if isinstance(part, Text):
            texts.append(part.plain)
        elif isinstance(part, Markdown):
            texts.append(part.markup)
    return texts


class _HostApp(App):
    def __init__(self, screen: ChatScreen) -> None:
        super().__init__()
        self._initial_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._initial_screen)


def _make_screen(tmp_path: Path, messages: list[Message]) -> ChatScreen:
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model", gateway_base_url="http://fake-gateway.test/v1")
    session.messages = messages
    settings = Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
        sandbox_backend="subprocess",  # skip the real docker probe in on_mount
    )
    return ChatScreen(settings, session=session, store=store)


def _tool_call(call_id: str, name: str, arguments: dict) -> ToolCall:
    return ToolCall(id=call_id, function=ToolCallFunction(name=name, arguments=json.dumps(arguments)))


@pytest.mark.asyncio
async def test_reload_does_not_show_the_leading_system_prompt_as_a_message(tmp_path: Path):
    messages = [
        Message(role="system", content="you are pcli, a coding agent..."),
        Message(role="user", content="hello"),
    ]
    screen = _make_screen(tmp_path, messages)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        user_widgets = message_view.query(".message-user")
        assert len(user_widgets) == 1

        # The leading system prompt itself was never mounted as a message.
        for widget in message_view.query(Static):
            for text in _texts_in(widget):
                assert "you are pcli" not in text


@pytest.mark.asyncio
async def test_reload_shows_a_tool_call_only_assistant_turn(tmp_path: Path):
    """Regression: an assistant message with tool_calls but no content
    (the model only called a tool, said nothing) used to vanish entirely on
    reload - the old loop only handled role in (user, assistant) AND
    message.content."""
    messages = [
        Message(role="system", content="system prompt"),
        Message(role="user", content="read the config"),
        Message(
            role="assistant",
            content=None,
            tool_calls=[_tool_call("call_1", "read_file", {"path": "config.toml"})],
        ),
        Message(role="tool", tool_call_id="call_1", name="read_file", content="key = 1"),
    ]
    screen = _make_screen(tmp_path, messages)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        previews = [
            text for widget in message_view.query(".message-tool") for text in _texts_in(widget)
        ]
        assert "→ read_file" in previews


@pytest.mark.asyncio
async def test_reload_renders_tool_results_via_the_real_collapsible_not_truncated(tmp_path: Path):
    """Regression: the old reload path sliced tool content to 500 chars in
    a flat line. The real add_tool_result rendering keeps the full content
    (collapsed by default, expandable) and shows the true char count."""
    long_output = "x" * 2000
    messages = [
        Message(role="system", content="system prompt"),
        Message(role="user", content="grep for x"),
        Message(
            role="assistant", content=None, tool_calls=[_tool_call("call_1", "grep", {"pattern": "x"})]
        ),
        Message(role="tool", tool_call_id="call_1", name="grep", content=long_output),
    ]
    screen = _make_screen(tmp_path, messages)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        collapsibles = message_view.query(Collapsible)
        result_collapsible = next(c for c in collapsibles if "grep" in c.title)
        assert f"{len(long_output):,} char(s)" in result_collapsible.title
        assert "error" not in result_collapsible.classes


@pytest.mark.asyncio
async def test_reload_shows_a_mid_conversation_system_message(tmp_path: Path):
    """Regression: a compaction summary (role='system', not at index 0 -
    it replaces old turns in place, see agent/compaction.py's maybe_compact)
    was previously invisible on reload - the old loop never handled
    role == 'system' at all."""
    messages = [
        Message(role="system", content="leading system prompt"),
        Message(role="system", content="Summary of turn 1.\n\n[Compacted 2 earlier message(s)...]"),
        Message(role="user", content="turn 2 user"),
        Message(role="assistant", content="turn 2 assistant"),
    ]
    screen = _make_screen(tmp_path, messages)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        system_texts = [
            text for widget in message_view.query(".message-system") for text in _texts_in(widget)
        ]

        assert any("Summary of turn 1." in text for text in system_texts)
        assert not any("leading system prompt" in text for text in system_texts)


@pytest.mark.asyncio
async def test_reload_shows_record_decision_as_a_decision_notice_not_a_raw_tool_result(
    tmp_path: Path,
):
    messages = [
        Message(role="system", content="system prompt"),
        Message(role="user", content="pick an http client"),
        Message(
            role="assistant",
            content=None,
            tool_calls=[
                _tool_call(
                    "call_1",
                    "record_decision",
                    {"decision": "Use httpx", "rationale": "already async elsewhere"},
                )
            ],
        ),
        Message(
            role="tool",
            tool_call_id="call_1",
            name="record_decision",
            content="Decision recorded: Use httpx",
        ),
    ]
    screen = _make_screen(tmp_path, messages)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        decision_widgets = message_view.query(".message-decision")
        assert len(decision_widgets) == 1
        # Not routed through the generic tool-result Collapsible.
        assert not any("record_decision" in c.title for c in message_view.query(Collapsible))
