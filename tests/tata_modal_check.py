"""Runnable headless check for T3 modal geometry + native '?' help panel.

Builds the real TataApp on a tmp course fixture, opens the F ConfirmationModal
and asserts it is centered (horizontally AND vertically) on the 120x40 app
box, then checks that '?' mounts Textual's native HelpPanel (and the custom
HelpModal is gone entirely).

Run: uv run tests/tata_modal_check.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.tata_workspace as tw
from src.tata_app import TataApp
from src.tata_workspace import AssignmentScreen, ConfirmationModal
from textual.containers import Vertical
from textual.pilot import Pilot
from textual.widgets import DataTable, HelpPanel

COURSE = "c1-first"


def _make_course(assignments_dir: Path) -> None:
    course_dir = assignments_dir / COURSE
    course_dir.mkdir(parents=True)
    (course_dir / "config.toml").write_text(
        "[fetch]\ncourse_id = 271218\n"
        '[[fetch.assignments]]\nassignment_id = 1001\nout = "a1/raw"\n',
        encoding="utf-8",
    )
    a_dir = course_dir / "a1"
    (a_dir / "raw").mkdir(parents=True)
    (a_dir / "processed").mkdir()
    (a_dir / "config.toml").write_text(
        "[fetch]\nassignment_id = 1001\n", encoding="utf-8"
    )
    (a_dir / "raw" / "x.py").write_text("print(1)", encoding="utf-8")
    (a_dir / "processed" / "x.md").write_text("# x", encoding="utf-8")


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], timeout: float = 30.0
) -> None:
    import asyncio as _asyncio
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause()
        if predicate():
            return
        await _asyncio.sleep(0.02)
    message = "timeout waiting for predicate"
    raise AssertionError(message)


async def _check_modal_centered(app: TataApp, pilot: Pilot) -> None:
    assert isinstance(app.screen, ConfirmationModal), type(app.screen)
    await pilot.pause()  # let the layout settle
    assert app.screen.styles.align_horizontal == "center", app.screen.styles.align_horizontal
    assert app.screen.styles.align_vertical == "middle", app.screen.styles.align_vertical
    box = app.screen.query_one(".confirm-modal", Vertical)
    region = box.region
    assert abs(region.x - (120 - region.width) // 2) <= 1, region
    assert abs(region.y - (40 - region.height) // 2) <= 1, region


async def _check_native_help(app: TataApp, pilot: Pilot) -> None:
    # course level: '?' mounts Textual's native HelpPanel, no custom modal
    await pilot.press("?")
    await pilot.pause()
    assert app.screen.query(HelpPanel), "native HelpPanel not mounted"
    assert not app.screen.query(".confirm-modal"), "custom modal still in use"


async def main() -> None:
    assert not hasattr(tw, "HelpModal"), "HelpModal class must be deleted"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_course(root / "assignments")
        app = TataApp(root_dir=root)
        async with app.run_test(size=(120, 40)) as pilot:
            table = app.query_one("#dashboard-table", DataTable)
            await _wait_for(pilot, lambda: table.row_count == 1)
            table.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert app.state.dashboard_level == "course"
            # F -> ConfirmationModal (push) -> centered
            await pilot.press("F")
            await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmationModal))
            await _check_modal_centered(app, pilot)
            await pilot.press("escape")
            await _wait_for(pilot, lambda: not isinstance(app.screen, ConfirmationModal))
            # native '?' keys panel on the dashboard screen
            await _check_native_help(app, pilot)
            # ...and inside the assignment workspace too
            await pilot.press("enter")
            await pilot.pause()
            assert app.state.dashboard_level == "assignment"
            assert app.query_one(AssignmentScreen).display
            await pilot.press("?")
            await pilot.pause()
            assert app.screen.query(HelpPanel)
            await pilot.press("escape")
            await pilot.pause()
            assert app.state.dashboard_level == "course"
    print("tata_modal check OK")


if __name__ == "__main__":
    asyncio.run(main())
