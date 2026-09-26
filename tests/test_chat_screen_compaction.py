"""Integration coverage for compaction wired into ChatScreen.

_run_compaction is invoked directly on a real, fully-mounted ChatScreen
(rather than driving the full @work-wrapped _stream_response streaming
pipeline) — it's the same method _stream_response's auto-trigger calls, and
this covers everything that actually matters here: ToolInvocation
bookkeeping (so export/import carries the artifact along), cost tracking,
context-display isolation, and the UI notice — without the fragility of
scripting a full turn's SSE stream just to reach a 3-line conditional.
"""

import json
import logging
from pathlib import Path

import httpx
import pytest
import respx
from textual.app import App

from pcli.config.settings import Settings
from pcli.cost.context import ContextLimitTable
from pcli.llm.models import Usage
from pcli.session.models import Message, Session, TurnCost
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


def _summary_response(text: str) -> httpx.Response:
    chunk = {
        "choices": [{"delta": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
    }
    return httpx.Response(200, content=_sse(chunk))


def _build_session_with_two_turns(store: SessionStore) -> Session:
    session = store.new_session(model="fake-model", gateway_base_url="http://fake-gateway.test/v1")
    session.messages = [
        Message(role="system", content="system prompt"),
        Message(role="user", content="turn 1 user"),
        Message(role="assistant", content="turn 1 assistant"),
        Message(role="user", content="turn 2 user"),
        Message(role="assistant", content="turn 2 assistant"),
    ]
    return session


def _make_screen(
    tmp_path: Path, *, keep_recent_turns: int = 1, **settings_overrides
) -> tuple[ChatScreen, SessionStore, Session]:
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = _build_session_with_two_turns(store)
    settings = Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
        sandbox_backend="subprocess",  # skip the real docker probe in on_mount
        auto_compact_keep_recent_turns=keep_recent_turns,
        # This file is about compaction's own bookkeeping specifically - the
        # memory-extraction pass _run_compaction also triggers on success
        # (memory/extraction.py) is covered separately in
        # test_chat_screen_memory.py, with its own precise cost/call
        # assertions; on here it'd just be an extra, untested gateway call
        # muddying this file's own token/call-count assertions.
        memory_enabled=False,
        **settings_overrides,
    )
    screen = ChatScreen(settings, session=session, store=store)
    return screen, store, session


@pytest.mark.asyncio
@respx.mock
async def test_run_compaction_auto_records_bookkeeping_and_isolates_context_display(
    tmp_path: Path,
):
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_summary_response("Summary of turn 1.")
    )

    screen, store, session = _make_screen(tmp_path, keep_recent_turns=1)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None  # on_mount succeeded

        # Force context usage to read as "over the limit" regardless of the
        # threshold, without needing to script a real turn's token counts.
        screen._context_limit_table = ContextLimitTable(entries={}, default=1)
        status_bar = screen.query_one(StatusBar)
        status_bar.context_used_tokens = 999
        status_bar.context_limit_tokens = 1

        tokens_before = session.cost.total_tokens

        await screen._run_compaction("auto")
        await pilot.pause()

        # Exactly turn 1 (2 messages) compacted; turn 2 (keep_recent_turns=1) untouched.
        assert len(session.messages) == 4  # system, summary, turn2_user, turn2_assistant
        assert session.messages[1].role == "system"
        assert "Summary of turn 1." in session.messages[1].content
        assert session.messages[2].content == "turn 2 user"

        # Synthetic ToolInvocation recorded so export bundles the artifact.
        compaction_invocations = [
            inv for inv in session.tool_invocations if inv.tool_name == "_compaction"
        ]
        assert len(compaction_invocations) == 1
        assert compaction_invocations[0].full_result_ref is not None
        archived = store.read_blob(session.id, compaction_invocations[0].full_result_ref)
        assert "turn 1 user" in archived

        # Real spend recorded (token accounting doesn't depend on this
        # machine's pricing.toml matching "fake-model")...
        assert session.cost.total_tokens == tokens_before + 70
        # ...but the context-usage display is untouched by the compaction
        # call's own (small, unrelated) usage — same isolation as subagents.
        assert status_bar.context_used_tokens == 999
        assert status_bar.context_limit_tokens == 1

        message_view = screen.query_one(MessageView)
        assert message_view._current_role == "system"
        assert "Compacted 2 earlier message(s)" in message_view._current_text


@pytest.mark.asyncio
@respx.mock
async def test_run_compaction_retries_with_a_smaller_window_when_still_full(tmp_path: Path):
    """Regression coverage for a real reported bug: after an earlier
    compaction pass, the configured keep_recent_turns window itself can be
    what's filling context (e.g. several truncation/auto-continue retries,
    each its own turn boundary, concentrated in the protected recent
    window) — compaction_cutoff then finds nothing eligible at the
    configured setting even though the caller is still critically full, and
    /compact previously just gave up and reported "Nothing to compact yet."
    _run_compaction must retry with a smaller window rather than stop
    there, since there's genuine pressure to free room."""
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_summary_response("Summary of turn 1.")
    )

    # Exactly 2 real turns with keep_recent_turns=2: compaction_cutoff finds
    # nothing at the configured setting (len(boundaries) == keep_recent_turns,
    # and the round-based fallback needs far more messages than this to
    # engage) — the scenario this fix targets.
    screen, _store, session = _make_screen(tmp_path, keep_recent_turns=2)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None

        # current_context_usage (what the retry gate actually checks) reads
        # from session.cost.turns, not the status bar's own display fields
        # (those are cosmetic and unrelated) — force it critically full by
        # recording a main-conversation usage entry directly.
        screen._context_limit_table = ContextLimitTable(entries={}, default=1)
        session.cost.turns.append(
            TurnCost(
                turn_index=0,
                model="fake-model",
                usage=Usage(total_tokens=999),
                cost_usd=0.0,
                source="main",
            )
        )

        await screen._run_compaction("auto")
        await pilot.pause()

        # Retried down to keep_recent_turns=1: turn 1 compacted, turn 2 kept.
        assert len(session.messages) == 4  # system, summary, turn2_user, turn2_assistant
        assert session.messages[2].content == "turn 2 user"

        message_view = screen.query_one(MessageView)
        assert "Compacted 2 earlier message(s)" in message_view._current_text
        assert "kept only the last 1 recent turn(s)" in message_view._current_text


@pytest.mark.asyncio
async def test_run_compaction_manual_reports_nothing_to_compact(tmp_path: Path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(model="fake-model", gateway_base_url="http://fake-gateway.test/v1")
    session.messages = [
        Message(role="system", content="system prompt"),
        Message(role="user", content="only turn"),
        Message(role="assistant", content="only reply"),
    ]
    settings = Settings(
        gateway_base_url="http://fake-gateway.test/v1",
        gateway_api_key="test-key",
        default_model="fake-model",
        sandbox_backend="subprocess",
        auto_compact_keep_recent_turns=2,
    )
    screen = ChatScreen(settings, session=session, store=store)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None

        await screen._run_compaction("manual")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        # The "nothing to compact" notice was the last thing added.
        assert message_view._current_role == "system"
        assert "Nothing to compact yet." in message_view._current_text


@pytest.mark.asyncio
@respx.mock
async def test_compact_slash_command_triggers_manual_compaction(tmp_path: Path):
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_summary_response("Summary via slash command.")
    )

    screen, _store, session = _make_screen(tmp_path, keep_recent_turns=1)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None

        screen._handle_command("/compact")
        await pilot.pause()
        await pilot.pause()  # let the @work-scheduled _manual_compact finish

        assert any(
            inv.tool_name == "_compaction" for inv in session.tool_invocations
        ), "manual /compact should have run the same compaction path"


@pytest.mark.asyncio
@respx.mock
async def test_run_compaction_gateway_error_is_shown_not_crashed(tmp_path: Path):
    """maybe_compact's summarization call failing (e.g. a timeout) was
    previously uncaught here entirely - it would crash straight out of
    _run_compaction with nothing shown to the user. The turn that triggered
    auto-compaction has already completed and saved successfully by this
    point, so this must be a visible notice, not a silent worker crash."""
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        side_effect=httpx.ReadTimeout("the read operation timed out")
    )

    screen, _store, session = _make_screen(tmp_path, keep_recent_turns=1)
    screen._settings.max_retries = 1  # fail fast, no retry backoff in the test

    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None

        messages_before = list(session.messages)

        await screen._run_compaction("manual")
        await pilot.pause()

        message_view = screen.query_one(MessageView)
        assert message_view._current_role == "system"
        assert "Compaction failed" in message_view._current_text
        assert "request_timeout_s" in message_view._current_text

        # Nothing was mutated - the session's messages are untouched.
        assert session.messages == messages_before


@pytest.mark.asyncio
@respx.mock
async def test_gateway_error_mid_turn_still_attempts_auto_compaction_recovery(tmp_path: Path, caplog):
    """Regression test for a real reported bug: context usage was reported
    over 100% once (the status bar's live per-round-trip update — see
    ChatScreen._refresh_context_display) but auto-compaction never actually
    ran. Root cause: a turn that fails outright with a GatewayError (e.g.
    because the next internal round-trip got rejected for being too large)
    previously hit an early `return` inside _run_one_turn's `except
    GatewayError` block, before the normal end-of-turn auto-compact check
    was ever reached — so a session already over auto_compact_threshold
    stayed over threshold, and every subsequent turn failed the exact same
    way. The fix threads the same prune-then-maybe-compact recovery into
    that exception handler too."""

    def _route(request: httpx.Request) -> httpx.Response:
        if b"You are summarizing an in-progress coding-agent conversation" in request.content:
            return _summary_response("Summary of turn 1.")
        return httpx.Response(400, content=b'{"error": {"message": "context_length_exceeded"}}')

    respx.post("http://fake-gateway.test/v1/chat/completions").mock(side_effect=_route)

    screen, _store, session = _make_screen(tmp_path, keep_recent_turns=1)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None

        # Force context usage to already read as over auto_compact_threshold
        # (default 0.8) before the failing turn even starts — simulating an
        # earlier round-trip (this turn's or a prior one's) having already
        # pushed usage past the model's real window, exactly what the status
        # bar would have shown crossing 100% right before the next call got
        # rejected.
        screen._context_limit_table = ContextLimitTable(entries={}, default=1)
        session.cost.turns.append(
            TurnCost(
                turn_index=0,
                model="fake-model",
                usage=Usage(total_tokens=999),
                cost_usd=0.0,
                source="main",
            )
        )

        session.messages.append(Message(role="user", content="one more message"))
        with caplog.at_level(logging.ERROR, logger="pcli.tui.screens.chat"):
            screen._stream_response()
            for _ in range(10):
                await pilot.pause()

        # The turn's own GatewayError still happened and was logged (this
        # isn't hidden or swallowed by the recovery)...
        assert any("Gateway error during turn" in r.message for r in caplog.records)

        # ...but unlike before the fix, compaction was still attempted
        # afterward rather than leaving the session stuck over threshold.
        compaction_invocations = [
            inv for inv in session.tool_invocations if inv.tool_name == "_compaction"
        ]
        assert compaction_invocations, (
            "a GatewayError mid-turn while already over auto_compact_threshold should "
            "still trigger an auto-compaction recovery pass, not silently leave the "
            "session stuck over threshold for every subsequent turn"
        )


@pytest.mark.asyncio
@respx.mock
async def test_run_compaction_uses_compaction_model_when_configured(tmp_path: Path):
    """settings.compaction_model, when set, is a deliberately cheaper model
    for this mechanical background call - it must actually be the model
    named in the request, not just default_model as before this setting
    existed."""
    requested_models: list[str | None] = []

    def _route(request: httpx.Request) -> httpx.Response:
        requested_models.append(json.loads(request.content)["model"])
        return _summary_response("Summary of turn 1.")

    respx.post("http://fake-gateway.test/v1/chat/completions").mock(side_effect=_route)

    screen, _store, _session = _make_screen(
        tmp_path, keep_recent_turns=1, compaction_model="cheap-model"
    )
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None

        await screen._run_compaction("manual")
        await pilot.pause()

        assert requested_models == ["cheap-model"]


@pytest.mark.asyncio
@respx.mock
async def test_run_compaction_falls_back_to_default_model_when_compaction_model_is_unset(
    tmp_path: Path,
):
    requested_models: list[str | None] = []

    def _route(request: httpx.Request) -> httpx.Response:
        requested_models.append(json.loads(request.content)["model"])
        return _summary_response("Summary of turn 1.")

    respx.post("http://fake-gateway.test/v1/chat/completions").mock(side_effect=_route)

    screen, _store, _session = _make_screen(tmp_path, keep_recent_turns=1)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None

        await screen._run_compaction("manual")
        await pilot.pause()

        assert requested_models == ["fake-model"]  # default_model, unchanged from before


@pytest.mark.asyncio
@respx.mock
async def test_run_compaction_tags_cost_tracking_with_the_actual_compaction_model(
    tmp_path: Path,
):
    """Regression guard: the cost-tracking call must be tagged with the
    SAME model the request was actually sent to, not default_model - a
    mismatch would price the compaction call using the wrong model's rate
    (or a rate for a model that was never even called)."""
    respx.post("http://fake-gateway.test/v1/chat/completions").mock(
        return_value=_summary_response("Summary of turn 1.")
    )

    screen, _store, session = _make_screen(
        tmp_path, keep_recent_turns=1, compaction_model="cheap-model"
    )
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen._client is not None

        await screen._run_compaction("manual")
        await pilot.pause()

        compaction_turn = next(t for t in session.cost.turns if t.source == "compaction")
        assert compaction_turn.model == "cheap-model"
