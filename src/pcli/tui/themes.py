"""A curated set of custom Textual Themes approximating classic bundled vim
colorschemes not already covered by Textual's own builtins (which already
ship gruvbox/nord/dracula/monokai-style options — see textual.theme.
BUILTIN_THEMES). These are period-appropriate approximations of vim's own
palettes, not byte-exact ports — vim's terminal-256-color schemes don't map
onto Textual's primary/secondary/accent/surface/panel token model 1:1.
"""

from __future__ import annotations

from textual.theme import Theme

VIM_THEMES: list[Theme] = [
    Theme(
        name="vim-desert",
        dark=True,
        background="#333333",
        surface="#3f3f3f",
        panel="#4a4a4a",
        foreground="#f1cd8f",
        primary="#f0e68c",  # khaki - Statement
        secondary="#d2b48c",  # tan
        accent="#40ffff",  # cyan - Identifier
        success="#40ff40",  # green - Type
        warning="#ffdead",  # navajowhite - Special
        error="#ffa0a0",  # light red - Constant/String
    ),
    Theme(
        name="vim-torte",
        dark=True,
        background="#1c1c1c",
        surface="#282828",
        panel="#333333",
        foreground="#d0d0c8",
        primary="#f0e68c",  # khaki - Statement
        secondary="#a8a8a0",  # muted gray
        accent="#40ffff",  # cyan - Identifier
        success="#60ff60",  # green - Type
        warning="#ffdead",  # navajowhite - Special
        error="#ffa0a0",  # light red - Constant/String
    ),
    Theme(
        name="vim-evening",
        dark=True,
        background="#0e0e23",
        surface="#1a1a35",
        panel="#26264a",
        foreground="#d6d6d6",
        primary="#f0e68c",  # khaki - Statement
        secondary="#80a0ff",  # blue - Comment
        accent="#40ffff",  # cyan - Identifier
        success="#60ff60",  # green - Type
        warning="#ffb964",  # orange - PreProc
        error="#ffa0a0",  # light red - Constant/String
    ),
    Theme(
        name="vim-elflord",
        dark=True,
        background="#0a0a2a",
        surface="#141438",
        panel="#1f1f46",
        foreground="#c0c0c0",
        primary="#00c0c0",  # cyan - Statement/Identifier
        secondary="#80a0ff",  # blue - Comment
        accent="#c000c0",  # magenta - PreProc/Constant
        success="#60c060",  # green - Type
        warning="#ffb964",  # orange
        error="#c04040",  # red - String
    ),
    Theme(
        name="vim-slate",
        dark=True,
        background="#232333",
        surface="#2e2e40",
        panel="#3a3a4d",
        foreground="#c0c0c0",
        primary="#4a6f8a",  # steel blue
        secondary="#8ba3ad",  # slate gray-blue
        accent="#5fd7d7",  # cyan
        success="#87af5f",  # muted green
        warning="#d7af5f",  # muted gold
        error="#d75f5f",  # muted red
    ),
]
