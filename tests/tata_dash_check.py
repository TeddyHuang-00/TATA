"""Runnable headless check for the T6c dashboard key wiring (TATA).

Covers: c import-course gate (.env) + modal cancel, c import-assignment modal
(monkeypatched fetch), F/p confirm modals (cancel paths), s score review
(graded / ungraded / assignment-level guard), o/g config tab switching with
context, 1-5 state filter, tab switch -> Plagiarism reload.

Run: uv run tests/tata_dash_check.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as main_mod
import src.tata_app as tata_app_mod
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

COURSE = "c1-first"


def _make_course(assignments_dir: Path, *, graded: bool = True) -> None:
    """One course with a1 (flagged + graded) and a2 (partial, no graded)."""
    course_dir = assignments_dir / COURSE
    course_dir.mkdir(parents=True)
    (course_dir / "config.toml").write_text(
        "[fetch]\ncourse_id = 271218\n", encoding="utf-8"
    )
    for name, aid in [("a1", 1001), ("a2", 1002)]:
        a_dir = course_dir / name
        (a_dir / "raw").mkdir(parents=True)
        (a_dir / "processed").mkdir()
        (a_dir / "graded").mkdir()
        (a_dir / "config.toml").write_text(
            f"[fetch]\nassignment_id = {aid}\n", encoding="utf-8"
        )
        (a_dir / "raw" / "x.py").write_text("print(1)", encoding="utf-8")
        (a_dir / "processed" / "x.md").write_text("# x", encoding="utf-8")
        if graded and name == "a1":
            (a_dir / "graded" / "100572.json").write_text(
                json.dumps({"task1": {"rating": "correct"}}), encoding="utf-8"
            )
    pairs = course_dir / "a1" / "plagiarism"
    pairs.mkdir()
    (pairs / "all_pairs.json").write_text(
        json.dumps(
            {
                "version": 1,
                "pairs": [{"max_similarity_pct": 95.0}],
            }
        ),
        encoding="utf-8",
    )
    # alias.toml display names: course dir aliases itself + both assignments
    (course_dir / "alias.toml").write_text(
        '[course]\n"271218" = "My Course"\n'
        '[assignment]\n"1001" = "My Alias"\n"1002" = "Second Alias"\n',
        encoding="utf-8",
    )


def _cell(table: DataTable, row: int, col: int) -> object:
    from textual.coordinate import Coordinate

    cell = table.get_cell_at(Coordinate(row, col))
    return cell.plain if hasattr(cell, "plain") else str(cell)


def _text(widget: Static) -> str:
    return str(widget.content)


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], timeout: float = 30.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause()
        if predicate():
            return
        await asyncio.sleep(0.02)
    message = "timeout waiting for predicate"
    raise AssertionError(message)


def _spy_notify(app: TataApp) -> tuple[list[tuple[str, str | None]], Callable]:
    notices: list[tuple[str, str | None]] = []
    orig = app.notify

    def spy(message: str, *args: object, **kwargs: object) -> None:
        notices.append((str(message), str(kwargs.get("severity")) if "severity" in kwargs else None))
        orig(message, *args, **kwargs)

    app.notify = spy
    return notices, spy


def _assert_modal_gone(app: TataApp, modal_type: type) -> None:
    assert not isinstance(app.screen, modal_type), app.screen


async def _check_import_course_gate_without_env() -> None:
    """Global + no .env: c -> error notify, no modal."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_course(root / "assignments")
        app = TataApp(root_dir=root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            notices, _spy = _spy_notify(app)
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
        _make_course(root / "assignments")
        (root / ".env").write_text(
            "CANVAS_BASE_URL=https://canvas.example.edu\nCANVAS_ACCESS_TOKEN=tok\n",
            encoding="utf-8",
        )
        orig_list_courses = tata_app_mod.list_courses
        monkeypatch(
            tata_app_mod, "list_courses", lambda _canvas: [(271218, "c1-first"), (777, "hw-course")]
        )
        try:
            app = TataApp(root_dir=root)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                table = app.query_one("#dashboard-table", DataTable)
                table.focus()
                await pilot.press("c")
                await _wait_for(
                    pilot, lambda: isinstance(app.screen, ImportCourseModal)
                )
                modal = app.screen
                assert isinstance(modal, ImportCourseModal)
                # background worker populates the Select
                await _wait_for(
                    pilot, lambda: modal.query_one(Select).value == 271218
                )
                # cancel: escape -> no new dirs
                await pilot.press("escape")
                await _wait_for(
                    pilot, lambda: not isinstance(app.screen, ImportCourseModal)
                )
                dirs = sorted(
                    p.name for p in (root / "assignments").iterdir() if p.is_dir()
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
        await _wait_for(pilot, lambda: isinstance(app.screen, ImportAssignmentModal))
        modal = app.screen
        assert isinstance(modal, ImportAssignmentModal)
        await _wait_for(pilot, lambda: modal.query_one(Select).value == 777)
        # default mode = auto (third radio pressed), out defaults to the id
        mode_set = modal.query_one("#modal-mode")
        assert mode_set.pressed_button.id == "mode-auto"  # type: ignore[union-attr]
        await pilot.pause()
        await pilot.click("#import")
        await _wait_for(pilot, lambda: bool(calls))
        await _wait_for(pilot, lambda: app.query_one(DashboardScreen)._job is None)
        assert len(calls) == 1
        arg = calls[0]
        assert arg.course == 271218
        assert arg.assignment == 777
        assert arg.out == "777/raw", arg.out
        assert arg.mode == "auto"
        status = _text(app.query_one("#dash-status", Static))
        assert "Done in" in status, status
    finally:
        main_mod._run_fetch = orig_fetch


async def _check_score_review(pilot: Pilot, app: TataApp) -> None:
    """Course + s: graded -> ScoreReviewScreen; ungraded -> warning notify."""
    table = app.query_one("#dashboard-table", DataTable)
    # a1 (cursor row 0) has graded json -> push ScoreReviewScreen
    await pilot.press("s")
    await _wait_for(pilot, lambda: isinstance(app.screen, ScoreReviewScreen))
    await pilot.press("escape")
    await _wait_for(pilot, lambda: not isinstance(app.screen, ScoreReviewScreen))

    # move to a2 (no graded) -> warning, no push
    notices, _spy = _spy_notify(app)
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
    await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmationModal))
    await pilot.press("escape")
    await _wait_for(pilot, lambda: not isinstance(app.screen, ConfirmationModal))
    assert app.query_one(DashboardScreen)._job is None


async def _check_plagiarism_confirm(pilot: Pilot, app: TataApp) -> None:
    """p -> ConfirmationModal; cancel with escape."""
    table = app.query_one("#dashboard-table", DataTable)
    table.focus()
    await pilot.press("p")
    await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmationModal))
    await pilot.press("escape")
    await _wait_for(pilot, lambda: not isinstance(app.screen, ConfirmationModal))
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
    assert _cell(table, 0, 0) == "My Course", _cell(table, 0, 0)
    await pilot.press("enter")
    await pilot.pause()
    assert app.state.dashboard_level == "course"
    breadcrumb = _text(app.query_one("#breadcrumb", Static))
    assert "My Course" in breadcrumb, breadcrumb
    assert table.row_count == 2
    assert _cell(table, 0, 0) == "My Alias", _cell(table, 0, 0)
    assert _cell(table, 1, 0) == "Second Alias", _cell(table, 1, 0)
    table.move_cursor(row=0)
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()
    assert app.state.dashboard_level == "assignment"
    topbar = _text(app.query_one("#ws-topbar", Static))
    assert "My Alias" in topbar, topbar


async def main() -> None:
    # no-.env gate does not need .env; separate tmp to keep it clean
    await _check_import_course_gate_without_env()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_course(root / "assignments")
        (root / ".env").write_text(
            "CANVAS_BASE_URL=https://canvas.example.edu\nCANVAS_ACCESS_TOKEN=tok\n",
            encoding="utf-8",
        )

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

    print("tata dash check OK")


if __name__ == "__main__":
    asyncio.run(main())
