"""Runnable headless check for the global command palette (v10 item 3).

Drives the real ``TataApp`` (``App.run_test`` + Pilot) on a tmp course layout
and checks Textual's native palette overlay wired to ``TataCommands``:

- ``ctrl+p`` opens (``CommandPalette.is_open``) and escape closes; the
  priority binding also opens while an Input holds focus;
- the discovery list per view context — dashboard global / course /
  assignment, Settings, Plagiarism view, Score review — exactly matches the
  implemented rows, and the level scoping agrees with batch 1's
  ``check_action`` (the 1-4 filters and F/p/s are course-only, import is
  absent at the assignment level; Settings ``,`` and Rescan ``r`` apply at
  every level, including the assignment workspace);
- selecting a hit really runs that view's action: typing "gra" raises the
  Grade confirmation modal (escape -> no job started), and a spy proves "fet"
  runs Fetch — not the last entry (partial vs late-bound lambda);
- modals and the Library tab degrade to the system commands only;
- the Footer carries the pinned Palette key exactly once.

No network and no real stage runs — the only stage action exercised is the
Grade *confirmation* (dismissed).

Run: uv run python tests/tata_palette_check.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from e2e_common import make_course, wait_for  # isort: skip - seeds repo-root sys.path before src imports
from src.tui.app import TataApp
from src.tui.commands import TataCommands
from src.tui.plagiarism import PlagiarismViewScreen
from src.tui.score_review import ScoreReviewScreen
from src.tui.settings import SettingsScreen
from src.tui.workspace import AssignmentScreen, ConfirmationModal
from textual.command import CommandInput, CommandPalette, Hit
from textual.pilot import Pilot
from textual.widgets import DataTable, Input, Static
from textual.widgets._footer import FooterKey

# Discovery labels of TataCommands (name + key hint), per context. System
# commands (Quit/Theme/Keys/...) are interleaved by the palette and filtered
# out via the provider's help text.
GLOBAL_ROWS = ["Import course  c", "Settings  ,", "Rescan  r"]
COURSE_ROWS = [
    "Import assignment  c",
    "Fetch all  shift+f",
    "Open plagiarism  p",
    "Score review  s",
    "Filter: All  1",
    "Filter: Done  2",
    "Filter: Partial  3",
    "Filter: Not run  4",
    "Settings  ,",
    "Rescan  r",
]
WORKSPACE_ROWS = [
    "Fetch submissions  f",
    "Preprocess  p",
    "Grade\u2026  g",
    "Score  s",
    "Analyze  a",
    "Open score review",
    "Cancel running job  x",
    "Edit config  e",
    "Toggle config  shift+f",
    "Settings  ,",
    "Rescan  r",
]
SETTINGS_ROWS = ["Save  ctrl+s", "Reset  r", "Edit config  e", "Test Canvas  t"]
PLAGIARISM_ROWS = ["Run detection  p", "Run aggregate  a", "Cancel job  x", "Reload  r"]
SCORE_REVIEW_ROWS = ["Toggle raw JSON  j"]

# Help text = context label; doubles as the marker that separates our hits
# from the system command hits in the shared option list.
HELPS = {
    "Dashboard · Global",
    "Dashboard · Course",
    "Assignment workspace",
    "Settings",
    "Plagiarism view",
    "Score review",
}


def _fix(root: Path) -> None:
    """Course fixture with valid stage state: fetch entries, graded + scored."""
    make_course(
        root / "data",
        assignments={"a1": 1001, "a2": 1002},
        entries=True,
        graded="all",
        scored=True,
        fetch_cache=True,
        logs=True,
    )


def _hit_texts(app: TataApp) -> list[str]:
    """Every palette hit label (TATA + system)."""
    options = app.screen.query_one("CommandList").options
    return [str(o.hit.text) for o in options if hasattr(o, "hit")]


def _rows(app: TataApp) -> list[str]:
    """Discovery labels yielded by TataCommands for the current context."""
    options = app.screen.query_one("CommandList").options
    return [
        str(o.hit.text) for o in options if hasattr(o, "hit") and o.hit.help in HELPS
    ]


async def _open(app: TataApp, pilot: Pilot) -> CommandPalette:
    """Open the palette and wait for the discovery list to be complete."""
    await pilot.press("ctrl+p")
    await wait_for(pilot, lambda: CommandPalette.is_open(app))
    palette = app.screen
    # Dispatch source check: Textual hands providers app.screen_stack[-2].
    providers = [p for p in palette._providers if isinstance(p, TataCommands)]
    assert len(providers) == 1, providers
    calling = palette._calling_screen
    assert providers[0].screen is calling, (providers[0].screen, calling)
    await wait_for(pilot, lambda: palette.has_class("-ready"))
    return palette


async def _close(app: TataApp, pilot: Pilot) -> None:
    await pilot.press("escape")
    await wait_for(pilot, lambda: not CommandPalette.is_open(app))


async def _select_only_hit(app: TataApp, pilot: Pilot, query: str, needle: str) -> Hit:
    """Type ``query``, wait for the filtered list, select the single hit.

    Waiting for "exactly the hit" (not the stale discovery list) closes the
    race between the query re-gather and the enter press; the selected hit is
    returned while the palette is still open.
    """
    for char in query:
        await pilot.press(char)

    def only_hit() -> bool:
        texts = _hit_texts(app)
        return len(texts) == 1 and needle in texts[0]

    await wait_for(pilot, only_hit)
    options = app.screen.query_one("CommandList").options
    hit = next(o.hit for o in options if hasattr(o, "hit"))
    await pilot.press("enter")
    await wait_for(pilot, lambda: not CommandPalette.is_open(app))
    return hit


async def _enter_course(app: TataApp, pilot: Pilot) -> None:
    table = app.query_one("#dashboard-table", DataTable)
    await wait_for(pilot, lambda: table.row_count == 1)
    table.focus()
    await pilot.press("enter")
    await wait_for(pilot, lambda: app.state.dashboard_level == "course")


async def _enter_assignment(app: TataApp, pilot: Pilot) -> None:
    await _enter_course(app, pilot)
    await pilot.press("enter")
    await wait_for(pilot, lambda: app.state.dashboard_level == "assignment")


async def _check_open_close(root: Path) -> None:
    """ctrl+p opens / escape closes; priority binding works over an Input;
    the Footer shows the pinned Palette key exactly once."""
    app = TataApp(root_dir=root)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: app.state.courses != [])
        # the custom binding replaces the default injection: same key,
        # priority (Input-safe) — see App.BINDINGS in src/tui/app.py
        bindings = app._bindings.key_to_bindings.get("ctrl+p", [])
        assert any(b.action == "command_palette" and b.priority for b in bindings)
        assert all(b.key == "ctrl+p" for b in bindings), bindings

        keys = [k for k in app.screen.query(FooterKey) if k.key == "ctrl+p"]
        assert len(keys) == 1, [(k.key, k.description) for k in keys]
        assert str(keys[0].description) == "Palette", keys[0].description
        assert keys[0].has_class("-command-palette")

        # open with the live-search Input focused (priority beats the Input)
        search = app.query_one("#search-input", Input)
        search.focus()
        await pilot.pause()
        assert search.has_focus
        await _open(app, pilot)
        assert isinstance(app.focused, CommandInput), app.focused
        await _close(app, pilot)
        assert not CommandPalette.is_open(app)
        # focus restored to the widget that had it (Textual screen resume)
        assert app.focused is search, app.focused

        # open + close again with the table focused; keys keep working after
        table = app.query_one("#dashboard-table", DataTable)
        table.focus()
        await pilot.pause()
        await _open(app, pilot)
        await _close(app, pilot)
        assert app.focused is table, app.focused


async def _check_contexts(root: Path) -> None:
    """Discovery rows per context (dashboard levels + pushed views + fallbacks)."""
    app = TataApp(root_dir=root)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: app.state.courses != [])

        await _open(app, pilot)
        assert _rows(app) == GLOBAL_ROWS, _rows(app)
        # system commands still supplied (App.COMMANDS spread)
        assert "Quit" in _hit_texts(app)
        assert "Theme" in _hit_texts(app)
        await _close(app, pilot)

        await _enter_assignment(app, pilot)  # global -> course -> assignment
        # assignment level first (workspace rows), then walk back up
        await _open(app, pilot)
        assert _rows(app) == WORKSPACE_ROWS, _rows(app)
        await _close(app, pilot)

        # assignment-level Settings view
        await pilot.press("comma")
        await wait_for(pilot, lambda: isinstance(app.screen, SettingsScreen))
        await _open(app, pilot)
        assert _rows(app) == SETTINGS_ROWS, _rows(app)
        await _close(app, pilot)
        await pilot.press("escape")
        await wait_for(pilot, lambda: not isinstance(app.screen, SettingsScreen))

        # back to the course level (batch 1 scoping: filters/F/p/s are
        # course-only, import is hidden at the assignment level)
        await pilot.press("escape")
        await wait_for(pilot, lambda: app.state.dashboard_level == "course")
        await _open(app, pilot)
        assert _rows(app) == COURSE_ROWS, _rows(app)
        await _close(app, pilot)

        table = app.query_one("#dashboard-table", DataTable)
        # Plagiarism view (course level, p) — the pushed Screen is
        # PlagiarismViewScreen; the pane is queried inside it
        table.focus()
        await pilot.press("p")
        await wait_for(pilot, lambda: isinstance(app.screen, PlagiarismViewScreen))
        await _open(app, pilot)
        assert _rows(app) == PLAGIARISM_ROWS, _rows(app)
        await _close(app, pilot)
        await pilot.press("escape")
        await wait_for(pilot, lambda: not isinstance(app.screen, PlagiarismViewScreen))

        # Score review (course level, s -> graded fixture present)
        table.focus()
        await pilot.press("s")
        await wait_for(pilot, lambda: isinstance(app.screen, ScoreReviewScreen))
        await _open(app, pilot)
        assert _rows(app) == SCORE_REVIEW_ROWS, _rows(app)
        await _close(app, pilot)
        await pilot.press("escape")
        await wait_for(pilot, lambda: not isinstance(app.screen, ScoreReviewScreen))


async def _check_degrade(root: Path) -> None:
    """Non-view contexts show the system commands only (never an error)."""
    app = TataApp(root_dir=root)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: app.state.courses != [])
        await _enter_course(app, pilot)

        # modal open: no TATA context applies — system commands only
        app.query_one("#dashboard-table", DataTable).focus()
        await pilot.press("F")  # fetch-all confirmation (fixture has entries)
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationModal))
        await _open(app, pilot)
        assert _rows(app) == [], _rows(app)
        assert "Quit" in _hit_texts(app)
        await _close(app, pilot)
        await pilot.press("escape")
        await wait_for(pilot, lambda: not isinstance(app.screen, ConfirmationModal))

        # Library tab: no palette entries yet (documented limit)
        app.switch_tab("tab-library")
        await pilot.pause()
        await _open(app, pilot)
        assert _rows(app) == [], _rows(app)
        await _close(app, pilot)
        app.switch_tab("tab-dashboard")
        await pilot.pause()


async def _check_execution(root: Path) -> None:
    """Real selection path: spy for "fet", Grade modal for "gra"."""
    app = TataApp(root_dir=root)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: app.state.courses != [])
        await _enter_assignment(app, pilot)
        ws = app.query_one(AssignmentScreen)

        # Late-binding guard: shadow the target action AND the last entry's
        # action; a loop-built late-bound lambda would run toggle_config here.
        calls: list[str] = []
        ws.action_run_fetch = lambda: calls.append("run_fetch")
        ws.action_toggle_config = lambda: calls.append("toggle_config")
        await _open(app, pilot)
        hit = await _select_only_hit(app, pilot, "fet", "Fetch submissions")
        # the hit text/help match the command they belong to
        assert "Fetch submissions" in str(hit.text), hit.text
        assert "f" in str(hit.text), hit.text
        assert str(hit.help) == "Assignment workspace", hit.help
        assert calls == ["run_fetch"], calls
        del ws.action_run_fetch
        del ws.action_toggle_config

        # "gra" -> Grade confirmation modal; escape -> no job started
        await _open(app, pilot)
        hit = await _select_only_hit(app, pilot, "gra", "Grade")
        assert str(hit.help) == "Assignment workspace", hit.help
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationModal))
        modal = app.screen
        body = " ".join(str(widget.content) for widget in modal.query(Static))
        assert "Grade" in body, body
        assert "submissions" in body, body
        await pilot.press("escape")
        await wait_for(pilot, lambda: not isinstance(app.screen, ConfirmationModal))
        assert ws._job is None
        assert app.state.active_job is None


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _fix(root)
        await _check_open_close(root)
        await _check_contexts(root)
        await _check_degrade(root)
        await _check_execution(root)
    print("tata_palette check OK")


if __name__ == "__main__":
    asyncio.run(main())
