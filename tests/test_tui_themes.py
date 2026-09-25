"""Unit coverage for pcli's own curated vim-colorscheme-inspired Textual
themes (tui/themes.py) - registration into a real running App is covered
separately in test_tui_app.py (which also exercises PcliApp.on_mount
applying Settings.ui_theme)."""

from textual.theme import Theme

from pcli.tui.themes import VIM_THEMES


def test_vim_themes_are_distinctly_named():
    names = [theme.name for theme in VIM_THEMES]
    assert len(names) == len(set(names))


def test_vim_themes_are_all_prefixed_and_real_theme_instances():
    for theme in VIM_THEMES:
        assert theme.name.startswith("vim-")
        assert isinstance(theme, Theme)


def test_vim_themes_define_the_core_design_tokens():
    """Every theme sets its own background/foreground/primary rather than
    relying on Theme's defaults - a theme that silently fell back to
    whatever Theme.__init__ defaults to wouldn't actually look distinct."""
    for theme in VIM_THEMES:
        assert theme.background is not None
        assert theme.foreground is not None
        assert theme.primary is not None
        assert theme.surface is not None
        assert theme.panel is not None
