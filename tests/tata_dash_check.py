"""Runnable headless check for the T6c dashboard key wiring (TATA).

Covers: c import-course gate (.env) + modal cancel, c import-assignment modal
(monkeypatched fetch), F/p confirm modals (cancel paths), s score review
(graded / ungraded / assignment-level guard), o/g config tab switching with
context, 1-5 state filter, tab switch -> Plagiarism reload.

Run: uv run tests/tata_dash_check.py
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from e2e_common import (  # isort: skip - seeds repo-root sys.path before src imports
    COURSE,
    cell,
    make_course,
    spy_notify,
    text,
    wait_for,
    write_aliases,
)
from rich.text import Text as RichText
from src import cli as main_mod
from src.shared.aliases import load_alias_file
from src.shared.cli_options import FetchCliOptions
from src.tui import app as tata_app_mod
from src.tui.app import (
    AliasEditorModal,
    AssignmentSetupModal,
    DashboardScreen,
    ImportAssignmentModal,
    ImportCourseModal,
    TataApp,
)
from src.tui.plagiarism import PlagiarismScreen
from src.tui.plagiarism_detail import AssignmentPairDetailScreen
from src.tui.score_review import ScoreReviewScreen
from src.tui.settings import SettingsScreen
from src.tui.workspace import ConfirmationModal
from textual.containers import Vertical
from textual.pilot import Pilot
from textual.widgets import Button, Checkbox, DataTable, Input, Select, Static


def _fix(root: Path, *, env: bool = False) -> None:
    """Standard dash fixture: c1-first with a1 (flagged + graded) and a2."""
    make_course(
        root / "data",
        assignments={"a1": 1001, "a2": 1002},
        graded="first",
        pairs="minimal",
        env=env,
    )
    # alias.toml display names: course dir aliases itself + both assignments
    write_aliases(
        root / "data" / COURSE / "alias.toml",
        course_alias="My Course",
        assignment_alias={"a1": "My Alias", "a2": "Second Alias"},
    )


def _assert_modal_gone(app: TataApp, modal_type: type) -> None:
    assert not isinstance(app.screen, modal_type), app.screen


async def _check_import_course_gate_without_env() -> None:
    """Global + no .env: c -> error notify, no modal."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _fix(root)
        app = TataApp(root_dir=root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            notices, _spy = spy_notify(app)
            table = app.query_one("#dashboard-table", DataTable)
            table.focus()
            await pilot.press("c")
            await pilot.pause()
            assert any("Canvas environment missing" in msg for msg, _sev in notices), (
                notices
            )
            assert not isinstance(app.screen, ImportCourseModal)


async def _check_import_course_modal_with_env(
    monkeypatch: Callable, tmp_root: Path | None = None
) -> None:
    """Global + .env: c -> ImportCourseModal; cancel -> no side effects."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _fix(root, env=True)
        orig_list_courses = tata_app_mod.list_courses
        monkeypatch(
            tata_app_mod,
            "list_courses",
            lambda _canvas: [(111111, "c1-first"), (777, "hw-course")],
        )
        try:
            app = TataApp(root_dir=root)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                table = app.query_one("#dashboard-table", DataTable)
                table.focus()
                await pilot.press("c")
                await wait_for(pilot, lambda: isinstance(app.screen, ImportCourseModal))
                modal = app.screen
                assert isinstance(modal, ImportCourseModal)
                # background worker populates the Select
                await wait_for(pilot, lambda: modal.query_one(Select).value == 111111)
                # cancel: escape -> no new dirs
                await pilot.press("escape")
                await wait_for(
                    pilot, lambda: not isinstance(app.screen, ImportCourseModal)
                )
                dirs = sorted(p.name for p in (root / "data").iterdir() if p.is_dir())
                assert dirs == [COURSE], dirs
                assert app.state.dashboard_level == "global"
        finally:
            monkeypatch(tata_app_mod, "list_courses", orig_list_courses)


class _FakeProviders:
    """Deterministic provider registry stand-in for the setup modal."""

    def __init__(self, names: list[str]) -> None:
        self.providers = {n: object() for n in names}


@contextmanager
def _fake_providers(names: list[str]) -> Iterator[None]:
    """Patch ``src.tui.app.get_providers`` (repo provider.toml is not a
    fixture); restores on exit."""
    import src.tui.app as ta

    orig = ta.get_providers
    ta.get_providers = lambda: _FakeProviders(names)
    try:
        yield
    finally:
        ta.get_providers = orig


async def _check_import_assignment_modal(pilot: Pilot, app: TataApp) -> None:
    """Course + c: ImportAssignmentModal -> AssignmentSetupModal (defaults
    confirmed) -> import calls main._run_fetch."""
    data = app.state.assignments_dir
    (data / "rubrics").mkdir()
    (data / "rubrics" / "alpha.toml").write_text("", encoding="utf-8")
    (data / "prompt").mkdir()
    (data / "prompt" / "p1.md").write_text("", encoding="utf-8")
    calls: list[FetchCliOptions] = []
    orig_fetch = main_mod._run_fetch

    def fake_fetch(args: FetchCliOptions) -> None:
        calls.append(args)

    main_mod._run_fetch = fake_fetch
    try:
        with _fake_providers(["gamma", "delta"]):
            await pilot.press("c")
            await wait_for(pilot, lambda: isinstance(app.screen, ImportAssignmentModal))
            modal = app.screen
            assert isinstance(modal, ImportAssignmentModal)
            await wait_for(pilot, lambda: modal.query_one(Select).value == 777)
            await pilot.click("#import")
            # setup modal: first rubric/provider, prompts all checked by default
            await wait_for(pilot, lambda: isinstance(app.screen, AssignmentSetupModal))
            setup = app.screen
            assert isinstance(setup, AssignmentSetupModal)
            rubric = setup.query_one("#setup-rubric", Select)
            assert rubric.value == "alpha.toml"
            provider = setup.query_one("#setup-provider", Select)
            assert provider.value == "delta"
            prompts = list(setup.query_one("#setup-prompts", Vertical).query(Checkbox))
            assert [p.label for p in prompts] == ["p1.md"]
            assert all(p.value for p in prompts)
            assert not setup.query_one("#import", Button).disabled
            await pilot.click("#import")
            await wait_for(pilot, lambda: bool(calls))
            await wait_for(pilot, lambda: app.query_one(DashboardScreen)._job is None)
            assert len(calls) == 1
            arg = calls[0]
            assert arg.course == 111111
            assert arg.assignment == 777
            status = text(app.query_one("#dash-status", Static))
            assert "Done in" in status, status
            # The setup flow really leaves <course>/777/config.toml (config +
            # alias writes are asserted in tata_modal_check._check_import_flow);
            # drop it so the remaining key-wiring checks run on the standard
            # 2-assignment fixture below.
            shutil.rmtree(app.state.assignments_dir / COURSE / "777")
            app.query_one(DashboardScreen)._rescan_course()
            await pilot.pause()
    finally:
        main_mod._run_fetch = orig_fetch


async def _check_score_review(pilot: Pilot, app: TataApp) -> None:
    """Course + s: graded -> ScoreReviewScreen; ungraded -> warning notify."""
    table = app.query_one("#dashboard-table", DataTable)
    # a1 (cursor row 0) has graded json -> push ScoreReviewScreen
    await pilot.press("s")
    await wait_for(pilot, lambda: isinstance(app.screen, ScoreReviewScreen))
    await pilot.press("escape")
    await wait_for(pilot, lambda: not isinstance(app.screen, ScoreReviewScreen))

    # move to a2 (no graded) -> warning, no push
    notices, _spy = spy_notify(app)
    table.focus()
    await pilot.press("down")
    await pilot.pause()
    await pilot.press("s")
    await pilot.pause()
    assert any("No graded files" in msg for msg, _sev in notices), notices
    assert not isinstance(app.screen, ScoreReviewScreen)


async def _check_s_guard_at_assignment(pilot: Pilot, app: TataApp) -> None:
    """s at assignment level -> workspace's own key; dashboard never pushes review."""
    table = app.query_one("#dashboard-table", DataTable)
    table.focus()
    await pilot.press(
        "down"
    )  # a2 has no graded; keeps cursor on assignment level below
    await pilot.press("enter")
    await pilot.pause()
    assert app.state.dashboard_level == "assignment"
    await pilot.press("s")
    await pilot.pause()
    assert not isinstance(app.screen, ScoreReviewScreen)
    # back up to course for the next checks
    await pilot.press("escape")
    await pilot.pause()
    assert app.state.dashboard_level == "course"


async def _check_fetch_all_confirm(pilot: Pilot, app: TataApp) -> None:
    """F -> ConfirmationModal; cancel with escape."""
    table = app.query_one("#dashboard-table", DataTable)
    table.focus()
    await pilot.press("F")
    await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationModal))
    await pilot.press("escape")
    await wait_for(pilot, lambda: not isinstance(app.screen, ConfirmationModal))
    assert app.query_one(DashboardScreen)._job is None


async def _check_plagiarism_confirm(pilot: Pilot, app: TataApp) -> None:
    """p -> ConfirmationModal; cancel with escape."""
    table = app.query_one("#dashboard-table", DataTable)
    table.focus()
    await pilot.press("p")
    await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationModal))
    await pilot.press("escape")
    await wait_for(pilot, lambda: not isinstance(app.screen, ConfirmationModal))
    assert app.query_one(DashboardScreen)._job is None


async def _check_config_keys(pilot: Pilot, app: TataApp) -> None:
    """o (course) -> Settings tab context=course; g (global) -> context=global."""
    table = app.query_one("#dashboard-table", DataTable)
    table.focus()
    await pilot.press("o")
    await pilot.pause()
    settings = app.query_one(SettingsScreen)
    tabbed = app.query_one("#shell-tabs")
    assert tabbed.active == "tab-settings", tabbed.active
    assert settings.current_context == "course", settings.current_context
    # back to dashboard, up to global, then g -> global context
    app.switch_tab("tab-dashboard")
    await pilot.pause()
    table.focus()
    await pilot.press("escape")
    await pilot.pause()
    assert app.state.dashboard_level == "global"
    table.focus()
    await pilot.press("g")
    await pilot.pause()
    assert tabbed.active == "tab-settings"
    assert settings.current_context == "global", settings.current_context


async def _check_filter(pilot: Pilot, app: TataApp) -> None:
    """1-4 filter (the 5/Flagged filter was removed): 3 -> partial (the
    fixture's state), 2 -> empty; pressing 5 is a no-op now."""
    app.switch_tab("tab-dashboard")
    await pilot.pause()
    # enter course again
    table = app.query_one("#dashboard-table", DataTable)
    table.focus()
    await pilot.press("enter")
    await pilot.pause()
    assert app.state.dashboard_level == "course"
    assert table.row_count == 2
    await pilot.press("3")
    await pilot.pause()
    assert table.row_count == 2, table.row_count  # both assignments are partial
    await pilot.press("2")
    await pilot.pause()
    assert table.row_count == 0, table.row_count  # none are done
    assert app.query_one("#dash-empty", Static).display
    await pilot.press("5")  # feedback 5: the flagged binding is gone
    await pilot.pause()
    assert table.row_count == 0
    await pilot.press("1")
    await pilot.pause()
    assert table.row_count == 2
    assert app.query_one(DashboardScreen)._filter is None


async def _check_search_sort(pilot: Pilot, app: TataApp) -> None:
    """Item 4: live search filters rows (search AND filter), header click
    toggles per-column sort, default order = display name asc."""
    dash = app.query_one(DashboardScreen)
    table = app.query_one("#dashboard-table", DataTable)
    search = app.query_one("#search-input", Input)
    assert app.state.dashboard_level == "course"
    assert search.display
    assert table.row_count == 2
    # live search: substring match on display names (case-insensitive)
    search.value = "my"
    await wait_for(pilot, lambda: table.row_count == 1)
    assert cell(table, 0, 0) == "My Alias", cell(table, 0, 0)
    search.value = "second"
    await wait_for(pilot, lambda: table.row_count == 1)
    assert cell(table, 0, 0) == "Second Alias", cell(table, 0, 0)
    # no match -> empty state
    search.value = "zzz-no-such"
    await wait_for(pilot, lambda: table.row_count == 0)
    assert text(app.query_one("#dash-empty", Static)) == (
        "No assignments match the filter/search."
    )
    search.value = ""
    await wait_for(pilot, lambda: table.row_count == 2)
    # default order = display name asc (My Alias before Second Alias)
    assert cell(table, 0, 0) == "My Alias", cell(table, 0, 0)
    assert cell(table, 1, 0) == "Second Alias", cell(table, 1, 0)
    # header click on column 0: asc -> desc (double-click interval avoided
    # by a real sleep between clicks; see feedback-3 double-click notes)
    table.focus()
    await pilot.pause()
    await pilot.click("#dashboard-table", offset=(5, 1))
    await pilot.pause()
    assert dash._sort == (0, False), dash._sort
    assert cell(table, 0, 0) == "My Alias", cell(table, 0, 0)
    await asyncio.sleep(0.6)  # clear the double-click window
    await pilot.click("#dashboard-table", offset=(5, 1))
    await pilot.pause()
    assert dash._sort == (0, True), dash._sort
    assert cell(table, 0, 0) == "Second Alias", cell(table, 0, 0)
    # search AND filter compose: filter 3 (partial) + search "second"
    await pilot.press("3")
    await pilot.pause()
    assert table.row_count == 2
    search.value = "second"
    await wait_for(pilot, lambda: table.row_count == 1)
    assert cell(table, 0, 0) == "Second Alias", cell(table, 0, 0)
    search.value = ""
    await pilot.press("1")
    await pilot.pause()
    # global level: course list is searchable too; sort resets on nav
    await pilot.press("escape")
    await pilot.pause()
    assert app.state.dashboard_level == "global"
    assert dash._sort is None
    search.value = "my"
    await wait_for(pilot, lambda: table.row_count == 1)
    assert cell(table, 0, 0) == "My Course", cell(table, 0, 0)
    search.value = ""
    await wait_for(pilot, lambda: table.row_count == 1)


async def _check_search_keystroke(pilot: Pilot, app: TataApp) -> None:
    """MAJOR-D: real keystrokes accumulate in #search-input. Every
    Input.Changed rebuilds the table via render_level, whose _refocus() must
    not grab focus back from the input mid-typing (that swallowed chars 2+)."""
    table = app.query_one("#dashboard-table", DataTable)
    search = app.query_one("#search-input", Input)
    table.focus()
    await pilot.press("enter")
    await pilot.pause()
    assert app.state.dashboard_level == "course"
    assert table.row_count == 2
    search.focus()
    await pilot.pause()
    for ch in "sec":
        await pilot.press(ch)
        await pilot.pause()
    # regression: all three keystrokes landed in the input, focus not stolen
    assert search.value == "sec", search.value
    assert search.has_focus, "focus stolen from #search-input after typing"
    await wait_for(pilot, lambda: table.row_count == 1)
    assert cell(table, 0, 0) == "Second Alias", cell(table, 0, 0)
    # real-key clear (ctrl+u deletes left-of-cursor = all), still focused
    await pilot.press("ctrl+u")
    await pilot.pause()
    assert search.value == "", search.value
    await wait_for(pilot, lambda: table.row_count == 2)
    # user can leave the input (Tab) and drive the rebuilt table again
    await pilot.press("tab")
    await pilot.pause()
    assert not search.has_focus, "tab did not leave #search-input"
    table.focus()
    await pilot.press("down")
    await pilot.pause()
    assert table.cursor_row in {0, 1}, table.cursor_row
    # leave no search residue for the following checks
    await pilot.press("escape")
    await pilot.pause()
    assert app.state.dashboard_level == "global"


async def _check_plagiarism_embed(pilot: Pilot, app: TataApp) -> None:
    """S4 embed: global level has no \"pairs\" column and the pane is hidden;
    course level shows the pane (upper table + lower pane, both nonzero)."""
    app.switch_tab("tab-dashboard")
    await pilot.pause()
    plag = app.query_one(PlagiarismScreen)
    table = app.query_one("#dashboard-table", DataTable)
    assert app.state.dashboard_level == "global"
    assert not plag.display
    # feedback 3: the shared-threshold ">80% pairs" column is gone
    labels = [str(c.label) for c in table.columns.values()]
    assert labels == [
        "Course",
        "Assignments",
        "Raw",
        "Proc",
        "Grad",
        "Avg score",
        "Last run",
    ], labels
    assert not any(label.endswith("pairs") for label in labels), labels
    # enter the course: pane visible, loaded (no no-course empty state)
    table.focus()
    await pilot.press("enter")
    await pilot.pause()
    assert app.state.dashboard_level == "course"
    assert plag.display
    assert not plag.query_one("#plag-empty", Static).display
    # upper half = assignment table, lower half = plagiarism pane; both live
    assert table.region.height > 0
    assert plag.region.height > 0
    assert plag.region.y >= table.region.y + table.region.height
    assert table.region.height >= 10
    # pane compactness: buttons row is at content height, no blank filler
    assert plag.query_one("#plag-buttons").region.height <= 4
    # feedback 3 regression: Enter on a plagiarism pane row pushes its detail
    # screen — it must NOT navigate the dashboard into the assignment view
    pairs_table = plag.query_one("#pairs-table", DataTable)
    assert pairs_table.row_count == 1, pairs_table.row_count
    stack_len = len(app.screen_stack)
    pairs_table.focus()
    await pilot.press("enter")
    await wait_for(pilot, lambda: isinstance(app.screen, AssignmentPairDetailScreen))
    assert app.state.dashboard_level == "course", app.state.dashboard_level
    assert len(app.screen_stack) == stack_len + 1
    await pilot.press("escape")
    await wait_for(
        pilot, lambda: not isinstance(app.screen, AssignmentPairDetailScreen)
    )
    assert app.state.dashboard_level == "course", app.state.dashboard_level
    assert len(app.screen_stack) == stack_len
    # assignment level: pane hidden again (embedded at course level only)
    table.focus()
    await pilot.press("enter")
    await pilot.pause()
    assert app.state.dashboard_level == "assignment"
    assert not plag.display
    await pilot.press("escape")
    await pilot.pause()
    assert app.state.dashboard_level == "course"
    # back to global: pane hidden again
    await pilot.press("escape")
    await pilot.pause()
    assert app.state.dashboard_level == "global"
    assert not plag.display
    assert table.region.height >= 25


async def _check_aliases(pilot: Pilot, app: TataApp) -> None:
    """Alias display names: Global/Course tables + breadcrumb + ws topbar."""
    app.switch_tab("tab-dashboard")
    await pilot.pause()
    table = app.query_one("#dashboard-table", DataTable)
    table.focus()
    await pilot.press("escape")
    await pilot.pause()
    assert app.state.dashboard_level == "global"
    assert cell(table, 0, 0) == "My Course", cell(table, 0, 0)
    await pilot.press("enter")
    await pilot.pause()
    assert app.state.dashboard_level == "course"
    breadcrumb = text(app.query_one("#breadcrumb", Static))
    assert "My Course" in breadcrumb, breadcrumb
    assert table.row_count == 2
    assert cell(table, 0, 0) == "My Alias", cell(table, 0, 0)
    assert cell(table, 1, 0) == "Second Alias", cell(table, 1, 0)
    table.move_cursor(row=0)
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()
    assert app.state.dashboard_level == "assignment"
    topbar = text(app.query_one("#ws-topbar", Static))
    assert "My Alias" in topbar, topbar


async def _check_alias_brackets() -> None:
    """Alias names containing markup brackets: escaped, plain text displayed."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        course_dir = root / "data" / COURSE
        (course_dir / "a1").mkdir(parents=True)
        (course_dir / "config.toml").write_text(
            "[fetch]\ncourse_id = 111111\n", encoding="utf-8"
        )
        (course_dir / "a1" / "config.toml").write_text("", encoding="utf-8")
        (course_dir / "alias.toml").write_text(
            '[course]\n"111111" = "My Course [S]"\n[assignment]\n"a1" = "Week [1]"\n',
            encoding="utf-8",
        )
        app = TataApp(root_dir=root)
        async with app.run_test(size=(120, 40)) as pilot:
            table = app.query_one("#dashboard-table", DataTable)
            await wait_for(pilot, lambda: table.row_count == 1)
            # global-level cell: escaped markup renders the literal name
            assert cell(table, 0, 0) == "My Course [S]", cell(table, 0, 0)
            table.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert app.state.dashboard_level == "course"
            topbar = text(app.query_one("#topbar", Static))
            breadcrumb = text(app.query_one("#breadcrumb", Static))
            # display text keeps literal brackets; the stored content is
            # markup-escaped, so rendering raises no MarkupError
            assert RichText.from_markup(topbar).plain == (
                "TATA · Dashboard [Course: My Course [S]]   Canvas: ? (.env missing)"
            ), topbar
            assert RichText.from_markup(breadcrumb).plain == "Global / My Course [S]"
            assert table.row_count == 1
            assert cell(table, 0, 0) == "Week [1]", cell(table, 0, 0)


async def _check_alias_editor_course() -> None:
    """Global level `a` -> AliasEditorModal: edit the selected course's
    single alias -> Save -> global data/alias.toml updated + display
    refreshes; esc cancels without writing."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_course(root / "data", assignments={"a1": 1001})
        app = TataApp(root_dir=root)
        async with app.run_test(size=(120, 40)) as pilot:
            table = app.query_one("#dashboard-table", DataTable)
            await wait_for(pilot, lambda: table.row_count == 1)
            table.focus()
            await pilot.press("a")
            await wait_for(pilot, lambda: isinstance(app.screen, AliasEditorModal))
            modal = app.screen
            assert isinstance(modal, AliasEditorModal)
            assert text(modal.query_one(".alias-key", Static)) == "111111"
            name_input = modal.query_one("#alias-name", Input)
            assert name_input.value == ""  # no alias yet
            name_input.value = "Renamed Course"
            modal.query_one("#save", Button).press()
            await wait_for(pilot, lambda: not isinstance(app.screen, AliasEditorModal))
            aliases = load_alias_file(root / "data" / "alias.toml")
            assert aliases["course"]["111111"] == "Renamed Course"
            await pilot.pause()
            assert cell(table, 0, 0) == "Renamed Course", cell(table, 0, 0)
            # esc cancels: no write
            table.focus()
            await pilot.press("a")
            await wait_for(pilot, lambda: isinstance(app.screen, AliasEditorModal))
            modal = app.screen
            assert isinstance(modal, AliasEditorModal)
            assert text(modal.query_one(".alias-key", Static)) == "111111"
            modal.query_one("#alias-name", Input).value = "Should Not Save"
            await pilot.press("escape")
            await wait_for(pilot, lambda: not isinstance(app.screen, AliasEditorModal))
            aliases = load_alias_file(root / "data" / "alias.toml")
            assert aliases["course"]["111111"] == "Renamed Course"


async def _check_alias_editor_assignment() -> None:
    """Course level `a` -> AliasEditorModal: edit the selected assignment's
    single alias (course alias.toml [assignment]): rename -> Save -> file +
    table refresh; empty name deletes the alias (display falls back to the
    dir name). `a` stays Analyze at the workspace level (covered by
    tata_workspace_check)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_course(root / "data", assignments={"a1": 1001})
        write_aliases(
            root / "data" / COURSE / "alias.toml",
            assignment_alias={"a1": "My Alias"},
        )
        app = TataApp(root_dir=root)
        async with app.run_test(size=(120, 40)) as pilot:
            table = app.query_one("#dashboard-table", DataTable)
            await wait_for(pilot, lambda: table.row_count == 1)
            table.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert app.state.dashboard_level == "course"
            assert cell(table, 0, 0) == "My Alias", cell(table, 0, 0)
            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("a")
            await wait_for(pilot, lambda: isinstance(app.screen, AliasEditorModal))
            modal = app.screen
            assert isinstance(modal, AliasEditorModal)
            assert "a1" in text(modal.query_one(".alias-key", Static))
            name_input = modal.query_one("#alias-name", Input)
            assert name_input.value == "My Alias"
            name_input.value = "Renamed Alias"
            modal.query_one("#save", Button).press()
            await wait_for(pilot, lambda: not isinstance(app.screen, AliasEditorModal))
            aliases = load_alias_file(root / "data" / COURSE / "alias.toml")
            assert aliases["assignment"]["a1"] == "Renamed Alias"
            await pilot.pause()
            assert cell(table, 0, 0) == "Renamed Alias", cell(table, 0, 0)
            # empty name deletes the alias -> display falls back to dir name
            table.focus()
            await pilot.press("a")
            await wait_for(pilot, lambda: isinstance(app.screen, AliasEditorModal))
            modal = app.screen
            assert isinstance(modal, AliasEditorModal)
            modal.query_one("#alias-name", Input).value = ""
            modal.query_one("#save", Button).press()
            await wait_for(pilot, lambda: not isinstance(app.screen, AliasEditorModal))
            aliases = load_alias_file(root / "data" / COURSE / "alias.toml")
            assert not aliases.get("assignment", {})
            await pilot.pause()
            assert cell(table, 0, 0) == "a1", cell(table, 0, 0)


async def main() -> None:
    # no-.env gate does not need .env; separate tmp to keep it clean
    await _check_import_course_gate_without_env()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _fix(root, env=True)

        def monkeypatch(module: object, name: str, fn: object) -> None:
            setattr(module, name, fn)

        await _check_import_course_modal_with_env(monkeypatch)

        def main_mod_assignment(
            _canvas: object, course_id: int
        ) -> list[tuple[int, str]]:
            return [(777, "HW1")]

        orig_la = tata_app_mod.list_assignments
        tata_app_mod.list_assignments = main_mod_assignment
        try:
            app = TataApp(root_dir=root)
            async with app.run_test(size=(120, 40)) as pilot:
                table = app.query_one("#dashboard-table", DataTable)
                table.focus()
                await pilot.press("enter")
                await pilot.pause()
                assert app.state.dashboard_level == "course"
                await _check_import_assignment_modal(pilot, app)
                await _check_score_review(pilot, app)
                await _check_s_guard_at_assignment(pilot, app)
                await _check_fetch_all_confirm(pilot, app)
                await _check_plagiarism_confirm(pilot, app)
                await _check_config_keys(pilot, app)
                await _check_plagiarism_embed(pilot, app)
                await _check_filter(pilot, app)
                await _check_search_sort(pilot, app)
                await _check_search_keystroke(pilot, app)
                await _check_aliases(pilot, app)
        finally:
            tata_app_mod.list_assignments = orig_la

    await _check_alias_brackets()
    await _check_alias_editor_course()
    await _check_alias_editor_assignment()

    print("tata dash check OK")


if __name__ == "__main__":
    asyncio.run(main())
