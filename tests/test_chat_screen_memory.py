"""Integration coverage for global user memory wired into ChatScreen: the
system-prompt injection for a brand-new session, the /memory command, and
the end-to-end extraction trigger piggybacked on _run_compaction (see
memory/extraction.py and test_memory_extraction.py for extraction's own
unit coverage)."""

import json
from pathlib import Path

import httpx
import pytest
import respx
from textual.app import App

from pcli.config.settings import Settings
from pcli.memory.store import add_entry, read_memory
from pcli.session.models import Message, Session
from pcli.session.store import SessionStore
from pcli.tui.screens.chat import ChatScreen
from pcli.tui.widgets.message_view import MessageView
from pcli.tui.widgets.status_bar import StatusBar


class _HostApp(App):
    def __init__(self, screen: ChatScreen) -> None:
        super().__init__()
        self._initial_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._initial_screen)


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


def _text_response(text: str, *, usage: dict | None = None) -> httpx.Response:
    chunks = [{"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}]
    if usage is not None:
        chunks.append({"choices": [], "usage": usage})
    return httpx.Response(200, content=_sse(*chunks))


def _remember_call_response(call_id: str, content: str, category: str) -> httpx.Response:
    arguments = json.dumps({"content": content, "category": category})
    return httpx.Response(
        200,
        content=_sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": call_id,
                                    "function": {"name": "remember", "arguments": arguments},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ),
    )


def _make_screen(tmp_path: Path, *, session: Session | None = None, **settings_overrides) -> tuple[ChatScreen, SessionStore]:
    store = SessionStore(base_dir=tmp_path / "sessions")
    settings = Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
        sandbox_backend="subprocess",  # skip the real docker probe in on_mount
        **settings_overrides,
    )
    screen = ChatScreen(settings, session=session, store=store)
    return screen, store


# --- system-prompt injection for a brand-new session ---


def test_new_session_system_prompt_includes_memory_when_present(tmp_path: Path):
    add_entry("Works as a backend Python developer", category="profile", source="derived", max_entries=40)

    screen, _store = _make_screen(tmp_path)

    system_message = screen._session.messages[0]
    assert system_message.role == "system"
    assert "Works as a backend Python developer" in system_message.content
    assert "# What you know about this user" in system_message.content


def test_new_session_system_prompt_has_no_memory_section_when_store_is_empty(tmp_path: Path):
    screen, _store = _make_screen(tmp_path)

    assert "# What you know about this user" not in screen._session.messages[0].content


def test_new_session_system_prompt_omits_memory_when_disabled(tmp_path: Path):
    add_entry("Works as a backend Python developer", category="profile", source="derived", max_entries=40)

    screen, _store = _make_screen(tmp_path, memory_enabled=False)

    assert "Works as a backend Python developer" not in screen._session.messages[0].content


def test_resuming_an_existing_session_does_not_re_inject_memory(tmp_path: Path):
    """Only a brand-new session's system prompt gets built (build_system_prompt) -
    an existing session passed in already has its own, whatever it was when
    created; resuming it must not silently rewrite it."""
    add_entry("some fact", category="profile", source="derived", max_entries=40)
    store = SessionStore(base_dir=tmp_path / "sessions")
    existing = store.new_session(model="fake-model")
    existing.messages.append(Message(role="system", content="original prompt, no memory"))

    screen, _store2 = _make_screen(tmp_path, session=existing)

    assert screen._session.messages[0].content == "original prompt, no memory"


# --- /memory command ---


@pytest.mark.asyncio
async def test_memory_command_with_nothing_stored(tmp_path: Path):
    screen, _store = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen._handle_command("/memory")
        await pilot.pause()

        assert "No memory entries yet." in screen.query_one(MessageView)._current_text


@pytest.mark.asyncio
async def test_memory_command_lists_entries_grouped_by_category(tmp_path: Path):
    add_entry("Backend Python dev", category="profile", source="derived", max_entries=40)
    add_entry("Prefers tabs", category="preference", source="explicit", max_entries=40)

    screen, _store = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen._handle_command("/memory")
        await pilot.pause()

        text = screen.query_one(MessageView)._current_text
        assert "Backend Python dev" in text
        assert "Prefers tabs" in text
        assert "(explicit)" in text


@pytest.mark.asyncio
async def test_memory_forget_removes_by_short_id(tmp_path: Path):
    entry = add_entry("Backend Python dev", category="profile", source="derived", max_entries=40)

    screen, _store = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen._handle_command(f"/memory forget {entry.id[-4:]}")
        await pilot.pause()

        assert read_memory().entries == []
        assert "Forgot:" in screen.query_one(MessageView)._current_text


@pytest.mark.asyncio
async def test_memory_forget_with_unknown_id_reports_no_match(tmp_path: Path):
    screen, _store = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen._handle_command("/memory forget zzzz")
        await pilot.pause()

        assert "No memory entry found" in screen.query_one(MessageView)._current_text


@pytest.mark.asyncio
async def test_memory_clear_wipes_the_store(tmp_path: Path):
    add_entry("fact one", category="profile", source="derived", max_entries=40)
    add_entry("fact two", category="preference", source="derived", max_entries=40)

    screen, _store = _make_screen(tmp_path)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen._handle_command("/memory clear")
        await pilot.pause()

        assert read_memory().entries == []
        assert "Cleared all memory entries." in screen.query_one(MessageView)._current_text


# --- end-to-end: compaction triggers extraction ---


@pytest.mark.asyncio
@respx.mock
async def test_compaction_triggers_memory_extraction_and_records_its_cost(tmp_path: Path):
    route = respx.post("http://fake-gateway.test/v1/chat/completions")
    route.side_effect = [
        _text_response(
            "Summary of turn 1.", usage={"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70}
        ),
        _remember_call_response("call_1", "Works as a backend Python developer", "profile"),
        _text_response(
            "Done reviewing.", usage={"prompt_tokens": 30, "completion_tokens": 5, "total_tokens": 35}
        ),
    ]

    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model", gateway_base_url="http://fake-gateway.test/v1")
    session.messages = [
        Message(role="system", content="system prompt"),
        Message(role="user", content="turn 1 user"),
        Message(role="assistant", content="turn 1 assistant"),
        Message(role="user", content="turn 2 user"),
        Message(role="assistant", content="turn 2 assistant"),
    ]
    settings = Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
        sandbox_backend="subprocess",
        auto_compact_keep_recent_turns=1,
    )
    screen = ChatScreen(settings, session=session, store=store)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None

        await screen._run_compaction("auto")
        await pilot.pause()

        entries = read_memory().entries
        assert len(entries) == 1
        assert entries[0].content == "Works as a backend Python developer"

        memory_turns = [t for t in session.cost.turns if t.source == "memory"]
        assert len(memory_turns) == 1
        assert memory_turns[0].usage.total_tokens == 35


@pytest.mark.asyncio
@respx.mock
async def test_busy_indicator_stays_lit_through_memory_extraction(tmp_path: Path):
    """Regression coverage for a real reported bug: status_bar.busy used to
    flip back to False the instant maybe_compact's own summarization call
    finished, before the (also real, gateway-calling) memory-extraction
    pass that follows a successful compaction - "Working..." would
    disappear from the status bar, then reappear once extraction quietly
    finished, looking exactly like pcli had stalled in between."""
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model", gateway_base_url="http://fake-gateway.test/v1")
    session.messages = [
        Message(role="system", content="system prompt"),
        Message(role="user", content="turn 1 user"),
        Message(role="assistant", content="turn 1 assistant"),
        Message(role="user", content="turn 2 user"),
        Message(role="assistant", content="turn 2 assistant"),
    ]
    settings = Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
        sandbox_backend="subprocess",
        auto_compact_keep_recent_turns=1,
    )
    screen = ChatScreen(settings, session=session, store=store)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        status_bar = screen.query_one(StatusBar)
        observed_busy_states: list[bool] = []
        responses = iter(
            [
                _text_response(
                    "Summary of turn 1.",
                    usage={"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
                ),
                _remember_call_response("call_1", "Works as a backend Python developer", "profile"),
                _text_response(
                    "Done reviewing.",
                    usage={"prompt_tokens": 30, "completion_tokens": 5, "total_tokens": 35},
                ),
            ]
        )

        def _record_busy_and_respond(request):
            # Captured *before* each gateway call resolves - the compaction
            # summarization call is #1, the other two are memory
            # extraction's own nested AgentLoop turn - all three must see
            # status_bar.busy already True.
            observed_busy_states.append(status_bar.busy)
            return next(responses)

        respx.post("http://fake-gateway.test/v1/chat/completions").mock(
            side_effect=_record_busy_and_respond
        )

        assert status_bar.busy is False
        await screen._run_compaction("auto")
        await pilot.pause()

        assert observed_busy_states == [True, True, True]
        assert status_bar.busy is False  # back off once everything, including extraction, is done


@pytest.mark.asyncio
@respx.mock
async def test_compaction_does_not_trigger_extraction_when_memory_disabled(tmp_path: Path):
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_text_response(
            "Summary of turn 1.", usage={"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70}
        )
    )

    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model", gateway_base_url="http://fake-gateway.test/v1")
    session.messages = [
        Message(role="system", content="system prompt"),
        Message(role="user", content="turn 1 user"),
        Message(role="assistant", content="turn 1 assistant"),
        Message(role="user", content="turn 2 user"),
        Message(role="assistant", content="turn 2 assistant"),
    ]
    settings = Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
        sandbox_backend="subprocess",
        auto_compact_keep_recent_turns=1,
        memory_enabled=False,
    )
    screen = ChatScreen(settings, session=session, store=store)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()

        await screen._run_compaction("auto")
        await pilot.pause()

        assert read_memory().entries == []
        assert not any(t.source == "memory" for t in session.cost.turns)
