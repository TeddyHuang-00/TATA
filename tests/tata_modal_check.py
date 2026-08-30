"""Runnable headless check for T3 modal geometry + native '?' help panel.

Builds the real TataApp on a tmp course fixture, opens the F ConfirmationModal
and asserts it is centered (horizontally AND vertically) on BOTH a 120x40 and
an 80x30 app box — the 80x30 run also asserts the modal fits (region.width
<= 80, no right-edge clipping). Then checks that '?' mounts Textual's native
HelpPanel (and the custom HelpModal is gone entirely).

Run: uv run tests/tata_modal_check.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from e2e_common import make_course, wait_for  # isort: skip - seeds repo-root sys.path before src imports
from src import tata_workspace as tw
from src.tata_app import TataApp
from src.tata_workspace import AssignmentScreen, ConfirmationModal
from textual.containers import Vertical
from textual.pilot import Pilot
from textual.widgets import DataTable, HelpPanel


async def _check_modal_centered(
    app: TataApp, pilot: Pilot, size: tuple[int, int]
) -> None:
    assert isinstance(app.screen, ConfirmationModal), type(app.screen)
    await pilot.pause()  # let the layout settle
    assert app.screen.styles.align_horizontal == "center", (
        app.screen.styles.align_horizontal
    )
    assert app.screen.styles.align_vertical == "middle", (
        app.screen.styles.align_vertical
    )
    box = app.screen.query_one(".confirm-modal", Vertical)
    region = box.region
    width, height = size
    assert region.width <= width, f"modal clipped on {size} terminal: {region}"
    assert abs(region.x - (width - region.width) // 2) <= 1, region
    assert abs(region.y - (height - region.height) // 2) <= 1, region


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
        make_course(root / "data", entries=True)
        app = TataApp(root_dir=root)
        async with app.run_test(size=(120, 40)) as pilot:
            table = app.query_one("#dashboard-table", DataTable)
            await wait_for(pilot, lambda: table.row_count == 1)
            table.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert app.state.dashboard_level == "course"
            # F -> ConfirmationModal (push) -> centered
            await pilot.press("F")
            await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationModal))
            await _check_modal_centered(app, pilot, (120, 40))
            await pilot.press("escape")
            await wait_for(pilot, lambda: not isinstance(app.screen, ConfirmationModal))
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
    # 80x30: the modal must not clip (width 92 > 80 previously overflowed)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_course(root / "data", entries=True)
        app = TataApp(root_dir=root)
        async with app.run_test(size=(80, 30)) as pilot:
            table = app.query_one("#dashboard-table", DataTable)
            await wait_for(pilot, lambda: table.row_count == 1)
            table.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert app.state.dashboard_level == "course"
            await pilot.press("F")
            await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationModal))
            await _check_modal_centered(app, pilot, (80, 30))
    print("tata_modal check OK")


if __name__ == "__main__":
    asyncio.run(main())
