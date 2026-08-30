"""Runnable headless check for the T6c dashboard key wiring (TATA).

Covers: c import-course gate (.env) + modal cancel, c import-assignment modal
(monkeypatched fetch), F/p confirm modals (cancel paths), s score review
(graded / ungraded / assignment-level guard), o/g config tab switching with
context, 1-5 state filter, tab switch -> Plagiarism reload.

Run: uv run tests/tata_dash_check.py
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable
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
from src import cli as main_mod, tata_app as tata_app_mod
from src.cli_options import FetchCliOptions
from src.score_review import ScoreReviewScreen
from src.tata_app import (
    DashboardScreen,
    ImportAssignmentModal,
    ImportCourseModal,
    TataApp,
)
from src.tata_plagiarism import PlagiarismScreen
from src.tata_settings import SettingsScreen
from src.tata_workspace import ConfirmationModal
from textual.pilot import Pilot
from textual.widgets import DataTable, Select, Static


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
        assignment_alias={"1001": "My Alias", "1002": "Second Alias"},
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
            assert any(
                "Canvas environment missing" in msg for msg, _sev in notices
            ), notices
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
            tata_app_mod, "list_courses", lambda _canvas: [(111111, "c1-first"), (777, "hw-course")]
        )
        try:
            app = TataApp(root_dir=root)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                table = app.query_one("#dashboard-table", DataTable)
                table.focus()
                await pilot.press("c")
                await wait_for(
                    pilot, lambda: isinstance(app.screen, ImportCourseModal)
                )
                modal = app.screen
                assert isinstance(modal, ImportCourseModal)
                # background worker populates the Select
                await wait_for(
                    pilot, lambda: modal.query_one(Select).value == 111111
                )
                # cancel: escape -> no new dirs
                await pilot.press("escape")
                await wait_for(
                    pilot, lambda: not isinstance(app.screen, ImportCourseModal)
                )
                dirs = sorted(
                    p.name for p in (root / "data").iterdir() if p.is_dir()
                )
                assert dirs == [COURSE], dirs
                assert app.state.dashboard_level == "global"
        finally:
            monkeypatch(tata_app_mod, "list_courses", orig_list_courses)


async def _check_import_assignment_modal(pilot: Pilot, app: TataApp) -> None:
    """Course + c: ImportAssignmentModal; confirmed import calls main._run_fetch."""
    calls: list[FetchCliOptions] = []
    orig_fetch = main_mod._run_fetch

    def fake_fetch(args: FetchCliOptions) -> None:
        calls.append(args)

    main_mod._run_fetch = fake_fetch
    try:
        await pilot.press("c")
        await wait_for(pilot, lambda: isinstance(app.screen, ImportAssignmentModal))
        modal = app.screen
        assert isinstance(modal, ImportAssignmentModal)
        await wait_for(pilot, lambda: modal.query_one(Select).value == 777)
        # default mode = auto (third radio pressed), out defaults to the id
        mode_set = modal.query_one("#modal-mode")
        assert mode_set.pressed_button.id == "mode-auto"  # type: ignore[union-attr]
        await pilot.pause()
        await pilot.click("#import")
        await wait_for(pilot, lambda: bool(calls))
        await wait_for(pilot, lambda: app.query_one(DashboardScreen)._job is None)
        assert len(calls) == 1
        arg = calls[0]
        assert arg.course == 111111
        assert arg.assignment == 777
        assert arg.out == "777/raw", arg.out
        assert arg.mode == "auto"
        status = text(app.query_one("#dash-status", Static))
        assert "Done in" in status, status
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
    await pilot.press("down")  # a2 has no graded; keeps cursor on assignment level below
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
    """1-5 filter: 5 -> flagged only; 1 -> all."""
    app.switch_tab("tab-dashboard")
    await pilot.pause()
    # enter course again
    table = app.query_one("#dashboard-table", DataTable)
    table.focus()
    await pilot.press("enter")
    await pilot.pause()
    assert app.state.dashboard_level == "course"
    assert table.row_count == 2
    await pilot.press("5")
    await pilot.pause()
    assert table.row_count == 1, table.row_count
    await pilot.press("1")
    await pilot.pause()
    assert table.row_count == 2
    assert app.query_one(DashboardScreen)._filter is None


async def _check_plagiarism_reload(pilot: Pilot, app: TataApp) -> None:
    """switch_tab -> reload_all has effect (empty hidden once a course is set)."""
    app.switch_tab("tab-plagiarism")
    await pilot.pause()
    plag = app.query_one(PlagiarismScreen)
    assert not plag.query_one("#plag-empty", Static).display
    app.switch_tab("tab-dashboard")
    await pilot.pause()


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
        (course_dir / "a1" / "config.toml").write_text(
            "[fetch]\nassignment_id = 1001\n", encoding="utf-8"
        )
        (course_dir / "alias.toml").write_text(
            '[course]\n"111111" = "My Course [S]"\n'
            '[assignment]\n"1001" = "Week [1]"\n',
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


async def main() -> None:
    # no-.env gate does not need .env; separate tmp to keep it clean
    await _check_import_course_gate_without_env()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _fix(root, env=True)

        def monkeypatch(module: object, name: str, fn: object) -> None:
            setattr(module, name, fn)

        await _check_import_course_modal_with_env(monkeypatch)

        def main_mod_assignment(_canvas: object, course_id: int) -> list[tuple[int, str]]:
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
                await _check_plagiarism_reload(pilot, app)
                await _check_filter(pilot, app)
                await _check_aliases(pilot, app)
        finally:
            tata_app_mod.list_assignments = orig_la

    await _check_alias_brackets()

    print("tata dash check OK")


if __name__ == "__main__":
    asyncio.run(main())
