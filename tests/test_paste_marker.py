"""Coverage for the widget-agnostic paste decision logic shared by
PasteInput (Input-based) and ChatInput (TextArea-based)."""

from pcli.tui.widgets.paste_marker import PendingPaste, decide_paste


def test_decide_paste_empty_text_returns_none():
    assert decide_paste("", expand_full_paste=True) is None
    assert decide_paste("", expand_full_paste=False) is None


def test_decide_paste_single_line_inserts_directly_regardless_of_expand_flag():
    for expand in (True, False):
        decision = decide_paste("hello", expand_full_paste=expand)
        assert decision is not None
        assert decision.text_to_insert == "hello"
        assert decision.pending_marker is None
        assert decision.pending_full_text is None


def test_decide_paste_multiline_not_expanding_keeps_first_line_only():
    decision = decide_paste("line one\nline two\nline three", expand_full_paste=False)
    assert decision is not None
    assert decision.text_to_insert == "line one"
    assert decision.pending_marker is None


def test_decide_paste_multiline_expanding_produces_marker():
    clipboard = "line one\nline two\nline three"
    decision = decide_paste(clipboard, expand_full_paste=True)
    assert decision is not None
    assert decision.text_to_insert == "[Pasted 3 lines]"
    assert decision.pending_marker == "[Pasted 3 lines]"
    assert decision.pending_full_text == clipboard


def test_pending_paste_consume_expands_marker():
    pending = PendingPaste()
    pending.set("[Pasted 2 lines]", "a\nb")
    expanded = pending.consume("explain this: [Pasted 2 lines] please")
    assert expanded == "explain this: a\nb please"


def test_pending_paste_consume_is_noop_when_marker_was_edited_away():
    pending = PendingPaste()
    pending.set("[Pasted 2 lines]", "a\nb")
    assert pending.consume("never mind") == "never mind"


def test_pending_paste_consume_clears_state_so_it_cannot_be_reused():
    pending = PendingPaste()
    pending.set("[Pasted 2 lines]", "a\nb")
    marker = "[Pasted 2 lines]"
    assert pending.consume(marker) == "a\nb"
    assert pending.consume(marker) == marker  # already consumed once


def test_pending_paste_consume_with_nothing_pending_is_noop():
    pending = PendingPaste()
    assert pending.consume("plain typed text") == "plain typed text"
