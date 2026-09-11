"""Global command palette provider (feedback v10 item 3).

One App-level :class:`~textual.command.Provider` that turns the *current
view's* existing key actions into palette commands: fuzzy-search and run the
same ``action_*`` methods the key bindings call. Textual's native palette
(``ctrl+p``, ``App.ENABLE_COMMAND_PALETTE``) supplies the overlay, the fuzzy
matcher and the system commands (Theme/Quit/Keys/Screenshot); this module
only adds the TATA commands, dispatched by the screen the palette was opened
from (``Provider.screen`` is the calling screen, Textual passes
``app.screen_stack[-2]``).

Contexts, all reusing existing actions (zero new logic):

- assignment workspace (dashboard level ``assignment``): the six stages,
  config edit/toggle, cancel, plus the dashboard-level Settings (``,``) and
  Rescan (``r``) that also work at this level;
- dashboard ``global`` / ``course``: import, settings, rescan, fetch-all,
  plagiarism view, score review, the 1-4 state filters (course only);
- ``SettingsScreen`` / ``PlagiarismViewScreen`` / ``ScoreReviewScreen``:
  that view's own keys.

Everything else degrades to the system commands only (modals, the Library
tab, plagiarism detail screens, any screen without the shell tabs) — a
missing target is never an error.

``src.tui.app`` must never be imported at module level: app.py imports this
module for ``TataApp.COMMANDS``; ``DashboardScreen`` is imported lazily
inside ``_dashboard_entries``.
"""

from __future__ import annotations

from functools import partial

from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.dom import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import TabbedContent

from src.tui.plagiarism import PlagiarismScreen, PlagiarismViewScreen
from src.tui.score_review import ScoreReviewScreen
from src.tui.settings import SettingsScreen
from src.tui.workspace import AssignmentScreen

# A command row: (display name, key hint, target screen/widget, action name).
Row = tuple[str, str, object, str]


def _invoke(target: object, action: str) -> None:
    """Call ``target``'s ``action_<action>`` — the same entry point keys use.

    Bound with ``partial`` on purpose: loop-built lambdas late-bind every
    command to the last entry (measured pitfall — selecting any row ran the
    final action).
    """
    getattr(target, f"action_{action}")()


def _label(name: str, key_hint: str) -> str:
    """Command display text: name plus the key that also runs it."""
    return f"{name}  {key_hint}" if key_hint else name


class TataCommands(Provider):
    """App-wide provider: the calling view's commands, fuzzy-searchable.

    Registered via ``TataApp.COMMANDS`` (``{*App.COMMANDS, TataCommands}`` —
    the spread keeps the native system commands).
    """

    async def search(self, query: str) -> Hits:
        """Fuzzy-match the current view's commands against the query."""
        matcher = self.matcher(query)
        help_text, rows = self._entries()
        for name, key_hint, target, action in rows:
            if (score := matcher.match(name)) > 0:
                label = _label(name, key_hint)
                yield Hit(
                    score,
                    matcher.highlight(label),
                    partial(_invoke, target, action),
                    text=label,
                    help=help_text,
                )

    async def discover(self) -> Hits:
        """The current view's commands, shown before the user types."""
        help_text, rows = self._entries()
        for name, key_hint, target, action in rows:
            yield DiscoveryHit(
                _label(name, key_hint),
                partial(_invoke, target, action),
                text=_label(name, key_hint),
                help=help_text,
            )

    def _entries(self) -> tuple[str, list[Row]]:
        """``(context help, rows)`` for the calling screen.

        A row is ``(name, key hint, target, action)``; the command runs
        ``target.action_<action>()``. Unknown screens / missing widgets yield
        no TATA rows (the system commands still show) — never an exception.
        """
        screen = self.screen
        if isinstance(screen, ModalScreen):
            return ("", [])  # modal on top — no TATA context applies
        if isinstance(screen, SettingsScreen):
            return (
                "Settings",
                [
                    ("Save", "ctrl+s", screen, "save"),
                    ("Reset", "r", screen, "reset"),
                    ("Edit config", "e", screen, "edit_config"),
                    ("Test Canvas", "t", screen, "test_canvas"),
                ],
            )
        if isinstance(screen, PlagiarismViewScreen):
            try:
                pane = screen.query_one(PlagiarismScreen)
            except NoMatches:
                return ("", [])
            return (
                "Plagiarism view",
                [
                    ("Run detection", "p", pane, "run_detect"),
                    ("Run aggregate", "a", pane, "run_aggregate"),
                    ("Cancel job", "x", pane, "cancel_job"),
                    ("Reload", "r", pane, "reload"),
                ],
            )
        if isinstance(screen, ScoreReviewScreen):
            return ("Score review", [("Toggle raw JSON", "j", screen, "toggle_json")])
        return self._dashboard_entries(screen)

    @staticmethod
    def _dashboard_entries(screen: Screen) -> tuple[str, list[Row]]:
        """Dashboard shell contexts; Library/detail screens fall back to none."""
        # Lazy import: app.py imports this module at load time (cycle).
        from src.tui.app import DashboardScreen  # ruff: ignore[import-outside-top-level]

        try:
            tabs = screen.query_one("#shell-tabs", TabbedContent)
            dashboard = screen.query_one(DashboardScreen)
        except NoMatches:
            return ("", [])  # detail screen / CLI shell: system commands only
        if tabs.active != "tab-dashboard":
            return ("", [])  # Library tab has no palette entries (yet)
        level = dashboard.state.dashboard_level
        if level == "global":
            return (
                "Dashboard · Global",
                [
                    ("Import course", "c", dashboard, "import_item"),
                    ("Settings", ",", dashboard, "open_settings"),
                    ("Rescan", "r", dashboard, "rescan"),
                ],
            )
        if level == "course":
            return (
                "Dashboard · Course",
                [
                    ("Import assignment", "c", dashboard, "import_item"),
                    ("Fetch all", "shift+f", dashboard, "fetch_all"),
                    ("Open plagiarism", "p", dashboard, "open_plagiarism"),
                    ("Score review", "s", dashboard, "score_review"),
                    ("Filter: All", "1", dashboard, "filter_all"),
                    ("Filter: Done", "2", dashboard, "filter_done"),
                    ("Filter: Partial", "3", dashboard, "filter_partial"),
                    ("Filter: Not run", "4", dashboard, "filter_not_run"),
                    ("Settings", ",", dashboard, "open_settings"),
                    ("Rescan", "r", dashboard, "rescan"),
                ],
            )
        try:
            workspace = dashboard.query_one(AssignmentScreen)
        except NoMatches:
            return ("", [])
        return (
            "Assignment workspace",
            [
                ("Fetch submissions", "f", workspace, "run_fetch"),
                ("Preprocess", "p", workspace, "run_preprocess"),
                ("Grade\u2026", "g", workspace, "run_grade"),
                ("Score", "s", workspace, "run_score"),
                ("Analyze", "a", workspace, "run_analyze"),
                ("Open score review", "", workspace, "run_score_review"),
                ("Cancel running job", "x", workspace, "cancel_job"),
                ("Edit config", "e", workspace, "edit_config"),
                ("Toggle config", "shift+f", workspace, "toggle_config"),
                # `,`/`r` also work at the assignment level (dashboard
                # bindings, not gated by check_action); the footer lists them.
                ("Settings", ",", dashboard, "open_settings"),
                ("Rescan", "r", dashboard, "rescan"),
            ],
        )
