"""Runnable headless check for the Fetch-all (F) per-assignment progress.

Follows tests/tata_app_check.py: App.run_test() + Pilot on a tmp course
layout, no pytest-asyncio. Skips real Canvas by monkeypatching
main_mod._run_fetch with a recorder. Asserts: loader-driven target count,
per-target sequential calls with entry.out (/raw) + mode fallback, live
panel states (pending/running/done/failed), completion summary, no raw
paths in the panel, and the empty-list notify path.

Run: uv run tests/tata_fetchall_check.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.tata_app as tata_app_mod
from rich.text import Text as RichText
from src.cli_options import FetchCliOptions
from src.tata_app import TataApp
from src.tata_workspace import ConfirmationModal
from textual.pilot import Pilot
from textual.widgets import DataTable, Static

AIDS = [1001, 1002, 1003]

# aid -> assigned label in the aliased fixture (1001 gets a bracket name)
LABELS = {1001: "Week [1]", 1002: "1002", 1003: "1003"}


def _make_course(
    assignments_dir: Path,
    name: str,
    course_id: int,
    with_entries: bool,
    with_aliases: bool = False,
) -> None:
    """One course with 3 assignment dirs; optionally [[fetch.assignments]]."""
    course_dir = assignments_dir / name
    course_dir.mkdir(parents=True)
    entries = "".join(
        f'[[fetch.assignments]]\nassignment_id = {aid}\nout = "{aid}/raw"\n'
        for aid in AIDS
    )
    (course_dir / "config.toml").write_text(
        f"[fetch]\ncourse_id = {course_id}\n" + (entries if with_entries else ""),
        encoding="utf-8",
    )
    if with_aliases:
        # Markup-hostile display name: brackets must be escaped on render.
        (course_dir / "alias.toml").write_text(
            '[assignment]\n"1001" = "Week [1]"\n', encoding="utf-8"
        )
    for aid in AIDS:
        a_dir = course_dir / str(aid)
        a_dir.mkdir()
        (a_dir / "raw").mkdir()
        (a_dir / "config.toml").write_text(
            f"[fetch]\nassignment_id = {aid}\n", encoding="utf-8"
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


def _text(widget: Static) -> str:
    return str(widget.content)


def _plain(widget: Static) -> str:
    """Display text of a markup Static (content holds the markup source)."""
    return RichText.from_markup(str(widget.content)).plain


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], timeout: float = 30.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause()
        if predicate():
            return
        await asyncio.sleep(0.02)
    msg = "timeout waiting for predicate"
    raise AssertionError(msg)


async def _check_fetch_all(root: Path) -> None:
    """All-ok run: 3 targets, live states, completion summary, no raw paths."""
    calls: list = []
    tata_app_mod.main_mod._run_fetch = _make_recorder(calls, 0.25)
    app = TataApp(root_dir=root)
    async with app.run_test(size=(120, 40)) as pilot:
        table = app.query_one("#dashboard-table", DataTable)
        panel = app.query_one("#dash-progress", Static)
        status = app.query_one("#dash-status", Static)
        await _wait_for(pilot, lambda: table.row_count == 1)
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
        await _wait_for(pilot, lambda: len(calls) == 1)
        await pilot.pause()
        assert panel.display
        assert "▶ Week [1]" in _plain(panel), _plain(panel)
        assert "○ 1002" in _plain(panel), _plain(panel)
        assert "Fetching 1/3" in _text(status), _text(status)

        # Completion: all done, sequential per-target calls with entry.out.
        await _wait_for(pilot, lambda: app.state.active_job is None)
        await pilot.pause()
        assert len(calls) == 3, calls
        course = app.state.current_course
        assert course is not None
        for call, aid in zip(calls, AIDS, strict=True):
            assert call.assignment == aid
            assert call.out == f"{aid}/raw", call.out
            assert call.mode == "auto", call.mode
            assert call.course == 111111, call.course
            assert call.config == course.config_path

        text = _plain(panel)
        assert panel.display, "panel hidden after completion"
        for aid in AIDS:
            assert f"✓ {LABELS[aid]}" in text, text
        assert "Fetch complete: 3/3 ok" in _text(status), _text(status)
        assert "data/" not in text
        assert "/raw" not in text
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
        await _wait_for(pilot, lambda: table.row_count == 1)
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        notices: list[tuple] = []
        app.notify = lambda *args, **kwargs: notices.append((args, kwargs))  # type: ignore[method-assign]
        await pilot.press("F")
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for(pilot, lambda: app.state.active_job is None)
        await pilot.pause()

        assert len(calls) == 3, "failed target must not stop the run"
        text = _plain(panel)
        assert "✗ 1002" in text, text
        assert "✓ Week [1]" in text, text
        assert "✓ 1003" in text, text
        assert "Fetch complete: 2/3 ok, 1 failed" in _text(status), _text(status)
        assert any(
            "1 failed" in args[0] and kwargs.get("severity") == "warning"
            for args, kwargs in notices
        ), notices


async def _check_fetch_all_empty(root: Path) -> None:
    """No [[fetch.assignments]] list: warn, no modal, no fetch calls."""
    calls: list = []
    tata_app_mod.main_mod._run_fetch = _make_recorder(calls, 0.01)
    app = TataApp(root_dir=root)
    async with app.run_test(size=(120, 40)) as pilot:
        table = app.query_one("#dashboard-table", DataTable)
        await _wait_for(pilot, lambda: table.row_count == 1)
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        notices: list[tuple] = []
        app.notify = lambda *args, **kwargs: notices.append((args, kwargs))  # type: ignore[method-assign]
        await pilot.press("F")
        await pilot.pause()

        assert calls == [], calls
        assert not isinstance(app.screen, ConfirmationModal)
        assert notices, "no notify emitted"
        assert "No assignments configured" in notices[0][0][0], notices
        assert app.state.active_job is None


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_course(
            root / "data", "c1-first", 111111, with_entries=True,
            with_aliases=True,
        )
        await _check_fetch_all(root)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_course(
            root / "data", "c1-first", 111111, with_entries=True,
            with_aliases=True,
        )
        await _check_fetch_all_failure(root)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_course(root / "data", "c1-first", 111111, with_entries=False)
        await _check_fetch_all_empty(root)

    print("tata_fetchall check OK")


if __name__ == "__main__":
    asyncio.run(main())
