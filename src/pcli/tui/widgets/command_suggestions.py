"""CommandSuggestions: a live-updating list of slash-command matches, shown
above ChatInput while the user is typing a command name. Purely a rendering
target - it never receives keyboard focus itself (ChatInput does, since
that's where the user is actually typing) and holds no matching/navigation
state of its own; ChatInput computes all of that from its own text and drives
this widget entirely through update_suggestions(), reached via the
ChatInput.SuggestionsChanged message it posts on every relevant change."""

from __future__ import annotations

from textual.widgets import ListItem, ListView, Static

# Wide enough for the longest current command name (max-tool-calls-per-minute,
# 25 chars) with a couple of spaces before the description starts.
_NAME_COLUMN_WIDTH = 28


class CommandSuggestions(ListView):
    def on_mount(self) -> None:
        # Set explicitly (not left to pcli.tcss alone, mirroring StatusPane's
        # identical on_mount pattern) so this widget starts correctly hidden
        # even if embedded somewhere its usual stylesheet isn't loaded.
        self.styles.display = "none"

    def update_suggestions(self, matches: list[tuple[str, str]], highlighted_index: int) -> None:
        self.clear()
        self.styles.display = "block" if matches else "none"
        if not matches:
            return
        for name, description in matches:
            row = f"/{name:<{_NAME_COLUMN_WIDTH}}{description}"
            self.append(ListItem(Static(row)))
        self.index = max(0, min(highlighted_index, len(matches) - 1))
