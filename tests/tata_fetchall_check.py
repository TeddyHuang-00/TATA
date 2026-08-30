"""Runnable headless check for the Fetch-all (F) per-assignment progress.

Follows tests/tata_app_check.py: App.run_test() + Pilot on a tmp course
layout, no pytest-asyncio. Skips real Canvas by monkeypatching
main_mod._run_fetch with a recorder. Asserts: loader-driven target count,
per-target sequential calls with entry id, live
panel states (pending/running/done/failed), completion summary, no raw
paths in the panel, and the empty-list notify path.

Run: uv run tests/tata_fetchall_check.py
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from e2e_common import COURSE, make_course, spy_notify, text, wait_for, write_aliases  # isort: skip - seeds repo-root sys.path before src imports
from rich.text import Text as RichText
from src import tata_app as tata_app_mod
from src.cli_options import FetchCliOptions
from src.tata_app import TataApp
from src.tata_workspace import ConfirmationModal
from textual.widgets import DataTable, Static

AIDS = [1001, 1002, 1003]

# aid -> assigned label in the aliased fixture (1001 gets a bracket name)
LABELS = {1001: "Week [1]", 1002: "1002", 1003: "1003"}


def _build(assignments_dir: Path, *, entries: bool, aliases: bool = False) -> None:
    """One course with 3 assignment dirs; optionally [[fetch.assignments]]."""
    make_course(
        assignments_dir,
        course=COURSE,
        course_id=111111,
        assignments={str(aid): aid for aid in AIDS},
        entries=entries,
    )
    if aliases:
        # Markup-hostile display name: brackets must be escaped on render.
        write_aliases(
            assignments_dir / COURSE / "alias.toml",
            assignment_alias={"1001": "Week [1]"},
        )


def _make_recorder(
    calls: list, sleep_seconds: float, fail_on: int | None = None
) -> Callable:
    def recorder(options: FetchCliOptions) -> None:
        calls.append(options)
        time.sleep(sleep_seconds)
        if fail_on is not None and options.assignment == fail_on:
            msg = "boom"
            raise RuntimeError(msg)

    return recorder


def _plain(widget: Static) -> str:
    """Display text of a markup Static (content holds the markup source)."""
    return RichText.from_markup(str(widget.content)).plain


async def _check_fetch_all(root: Path) -> None:
    """All-ok run: 3 targets, live states, completion summary, no raw paths."""
    calls: list = []
    tata_app_mod.main_mod._run_fetch = _make_recorder(calls, 0.25)
    app = TataApp(root_dir=root)
    async with app.run_test(size=(120, 40)) as pilot:
        table = app.query_one("#dashboard-table", DataTable)
        panel = app.query_one("#dash-progress", Static)
        status = app.query_one("#dash-status", Static)
        await wait_for(pilot, lambda: table.row_count == 1)
        assert not panel.display, "panel visible before any fetch-all"

        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.state.dashboard_level == "course"

        # F -> confirmation modal (n = len(cfg.assignments)) -> Fetch (focused)
        await pilot.press("F")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationModal)
        await pilot.press("enter")

        # Mid-run: first target running, others pending, live panel shown.
        await wait_for(pilot, lambda: len(calls) == 1)
        await pilot.pause()
        assert panel.display
        assert "▶ Week [1]" in _plain(panel), _plain(panel)
        assert "○ 1002" in _plain(panel), _plain(panel)
        assert "Fetching 1/3" in text(status), text(status)

        # Completion: all done, sequential per-target calls with entry id.
        await wait_for(pilot, lambda: app.state.active_job is None)
        await pilot.pause()
        assert len(calls) == 3, calls
        course = app.state.current_course
        assert course is not None
        for call, aid in zip(calls, AIDS, strict=True):
            assert call.assignment == aid
            assert call.course == 111111, call.course
            assert call.config == course.config_path

        panel_text = _plain(panel)
        assert panel.display, "panel hidden after completion"
        for aid in AIDS:
            assert f"✓ {LABELS[aid]}" in panel_text, panel_text
        assert "Fetch complete: 3/3 ok" in text(status), text(status)
        assert "data/" not in panel_text
        assert "/raw" not in panel_text
        assert app.state.active_job is None


async def _check_fetch_all_failure(root: Path) -> None:
    """One target fails: the run continues and the summary counts failures."""
    calls: list = []
    tata_app_mod.main_mod._run_fetch = _make_recorder(calls, 0.05, fail_on=1002)
    app = TataApp(root_dir=root)
    async with app.run_test(size=(120, 40)) as pilot:
        table = app.query_one("#dashboard-table", DataTable)
        panel = app.query_one("#dash-progress", Static)
        status = app.query_one("#dash-status", Static)
        await wait_for(pilot, lambda: table.row_count == 1)
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        notices, _orig = spy_notify(app)
        await pilot.press("F")
        await pilot.pause()
        await pilot.press("enter")
        await wait_for(pilot, lambda: app.state.active_job is None)
        await pilot.pause()

        assert len(calls) == 3, "failed target must not stop the run"
        panel_text = _plain(panel)
        assert "✗ 1002" in panel_text, panel_text
        assert "✓ Week [1]" in panel_text, panel_text
        assert "✓ 1003" in panel_text, panel_text
        assert "Fetch complete: 2/3 ok, 1 failed" in text(status), text(status)
        assert any("1 failed" in msg and sev == "warning" for msg, sev in notices), (
            notices
        )


async def _check_fetch_all_empty(root: Path) -> None:
    """No [[fetch.assignments]] list: warn, no modal, no fetch calls."""
    calls: list = []
    tata_app_mod.main_mod._run_fetch = _make_recorder(calls, 0.01)
    app = TataApp(root_dir=root)
    async with app.run_test(size=(120, 40)) as pilot:
        table = app.query_one("#dashboard-table", DataTable)
        await wait_for(pilot, lambda: table.row_count == 1)
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        notices, _orig = spy_notify(app)
        await pilot.press("F")
        await pilot.pause()

        assert calls == [], calls
        assert not isinstance(app.screen, ConfirmationModal)
        assert notices, "no notify emitted"
        assert "No assignments configured" in notices[0][0], notices
        assert app.state.active_job is None


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build(root / "data", entries=True, aliases=True)
        await _check_fetch_all(root)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build(root / "data", entries=True, aliases=True)
        await _check_fetch_all_failure(root)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build(root / "data", entries=False)
        await _check_fetch_all_empty(root)

    print("tata_fetchall check OK")


if __name__ == "__main__":
    asyncio.run(main())
