"""TATA Library tab: rubric + prompt + provider editing (F2).

Hosted by :mod:`src.tui.app` inside the Library TabPane (T4-d). Three inner
TabPanes: *Rubrics* (:class:`src.tui.rubrics_pane.RubricsPane`), *Prompts*
(:class:`src.tui.prompts_pane.PromptsPane`) and *Providers*
(:class:`src.tui.providers_pane.ProvidersPane`); this module is the container.
Non-Screen widgets get their styling via ``DEFAULT_CSS`` (class-level ``CSS``
does not apply to them — lesson c9272e81). All UI copy is English.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, override

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Select, TabbedContent, TabPane

from src.tui.css_loader import load_css
from src.tui.prompts_pane import PromptsPane
from src.tui.providers_pane import ProvidersPane
from src.tui.rubrics_pane import RubricsPane

if TYPE_CHECKING:
    from src.tui.app import AppState


class LibraryScreen(Vertical):
    """Library tab container: Rubrics + Prompts + Providers sub-tab panes."""

    DEFAULT_CSS = load_css("library.tcss")

    def __init__(self, state: AppState) -> None:
        super().__init__(id="library-screen")
        self.state = state

    @override
    def compose(self) -> ComposeResult:
        with TabbedContent(id="library-tabs"):
            with TabPane("Rubrics", id="tab-rubrics"):
                yield RubricsPane(self.state)
            with TabPane("Prompts", id="tab-prompts"):
                yield PromptsPane(self.state)
            with TabPane("Providers", id="tab-providers"):
                yield ProvidersPane(self.state)

    def reload_files(self) -> None:
        """Refresh all pane file lists (tab activation, external edits)."""
        with suppress(Exception):
            self.query_one(RubricsPane).reload_files()
        with suppress(Exception):
            self.query_one(PromptsPane).reload_files()
        with suppress(Exception):
            self.query_one(ProvidersPane).reload_files()

    def _focus_default(self) -> None:
        """Seat focus on the visible sub-tab's file Select."""
        tabs = self.query_one("#library-tabs", TabbedContent)
        if tabs.active == "tab-rubrics":
            target = "#rb-file"
        elif tabs.active == "tab-prompts":
            target = "#pr-file"
        else:
            target = "#pv-name"
        with suppress(Exception):
            self.query_one(target, Select).focus()
