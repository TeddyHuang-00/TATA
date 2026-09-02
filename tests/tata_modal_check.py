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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from e2e_common import COURSE, make_course, wait_for  # isort: skip - seeds repo-root sys.path before src imports
from src.shared.aliases import load_alias_file
from src.tui import workspace as tw
from src.tui.app import AssignmentSetupModal, DashboardScreen, TataApp
from src.tui.scan import CourseInfo
from src.tui.workspace import AssignmentScreen, ConfirmationModal
from textual.containers import Vertical
from textual.pilot import Pilot
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    HelpPanel,
    Select,
    Static,
)


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
    # toggle: the second '?' closes the panel
    await pilot.press("?")
    await pilot.pause()
    assert not app.screen.query(HelpPanel), "toggle must close the panel"


class _FakeProviders:
    """Deterministic provider registry stand-in for the setup modal."""

    def __init__(self, names: list[str]) -> None:
        self.providers = {n: object() for n in names}


@contextmanager
def _fake_providers(names: list[str]) -> Iterator[None]:
    """Patch ``src.tui.app.get_providers`` (the repo provider.toml is not a
    fixture); restores on exit."""
    import src.tui.app as ta

    orig = ta.get_providers
    ta.get_providers = lambda: _FakeProviders(names)
    try:
        yield
    finally:
        ta.get_providers = orig


def _options_values(select: Select) -> list[object]:
    return [value for _, value in select._options]


async def _check_assignment_setup() -> None:
    """Assignment quick-setup modal: library listing, defaults, enablement,
    dismiss payload (and the empty-registry disable case)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        data = root / "data"
        make_course(data, entries=True)
        (data / "rubrics").mkdir()
        (data / "rubrics" / "alpha.toml").write_text("", encoding="utf-8")
        (data / "rubrics" / "beta.toml").write_text("", encoding="utf-8")
        (data / "prompt").mkdir()
        (data / "prompt" / "p1.md").write_text("", encoding="utf-8")
        (data / "prompt" / "p2.md").write_text("", encoding="utf-8")
        with _fake_providers(["gamma", "delta"]):
            captured: list[object] = []
            app = TataApp(root_dir=root)
            async with app.run_test(size=(120, 40)) as pilot:
                app.push_screen(
                    AssignmentSetupModal(app.state), callback=captured.append
                )
                await wait_for(
                    pilot, lambda: isinstance(app.screen, AssignmentSetupModal)
                )
                screen = app.screen
                rubric = screen.query_one("#setup-rubric", Select)
                assert rubric.value == "alpha.toml"  # first sorted
                assert _options_values(rubric) == ["alpha.toml", "beta.toml"]
                provider = screen.query_one("#setup-provider", Select)
                assert provider.value == "delta"  # first sorted
                assert _options_values(provider) == ["delta", "gamma"]
                prompts = list(
                    screen.query_one("#setup-prompts", Vertical).query(Checkbox)
                )
                assert [p.label for p in prompts] == ["p1.md", "p2.md"]  # sorted
                assert all(p.value for p in prompts)  # all checked by default
                assert not screen.query_one("#import", Button).disabled
                # no prompt selected -> Import disabled
                for p in prompts:
                    p.value = False
                await pilot.pause()
                assert screen.query_one("#import", Button).disabled
                # re-check one -> enabled; Import dismisses with the payload
                prompts[0].value = True
                await pilot.pause()
                assert not screen.query_one("#import", Button).disabled
                await pilot.click("#import")
                await wait_for(pilot, lambda: bool(captured))
                assert captured == [
                    {
                        "rubric": "rubrics/alpha.toml",
                        "system_prompt": ["prompt/p1.md"],
                        "provider": "delta",
                    }
                ]
            # empty provider registry -> Import disabled + error shown
            app2 = TataApp(root_dir=root)
            with _fake_providers([]):
                async with app2.run_test(size=(120, 40)) as pilot:
                    app2.push_screen(AssignmentSetupModal(app2.state))
                    await wait_for(
                        pilot, lambda: isinstance(app2.screen, AssignmentSetupModal)
                    )
                    assert app2.screen.query_one("#import", Button).disabled
                    error = app2.screen.query_one("#setup-error", Static)
                    assert "No providers configured" in str(error.content)


async def _check_import_flow() -> None:
    """Confirmed setup writes <dir>/config.toml + the course alias, then the
    fetch job runs (fetch stubbed out)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        data = root / "data"
        make_course(data, entries=True)
        (data / "rubrics").mkdir()
        (data / "rubrics" / "alpha.toml").write_text("", encoding="utf-8")
        (data / "prompt").mkdir()
        (data / "prompt" / "p1.md").write_text("", encoding="utf-8")
        app = TataApp(root_dir=root)
        async with app.run_test(size=(120, 40)) as pilot:
            dash = app.query_one(DashboardScreen)
            app.state.current_course = CourseInfo(
                dir_name=COURSE,  # from e2e_common
                config_path=data / COURSE / "config.toml",
                course_id=111111,
            )
            app.state.dashboard_level = "course"
            orig_fetch = DashboardScreen._fetch_one
            DashboardScreen._fetch_one = staticmethod(lambda course, aid: None)
            try:
                dash._on_assignment_setup(
                    999999,
                    "My Assignment",
                    {
                        "rubric": "rubrics/alpha.toml",
                        "system_prompt": ["prompt/p1.md"],
                        "provider": "delta",
                    },
                )
            finally:
                DashboardScreen._fetch_one = orig_fetch
            await wait_for(pilot, lambda: app.state.active_job is None)
            config = data / COURSE / "999999" / "config.toml"
            cfg_text = config.read_text(encoding="utf-8")
            assert "# schema:" not in cfg_text
            assert 'rubric = "rubrics/alpha.toml"' in cfg_text
            assert 'system_prompt = ["prompt/p1.md"]' in cfg_text
            assert 'provider = "delta"' in cfg_text
            aliases = load_alias_file(data / COURSE / "alias.toml")
            assert aliases["assignment"]["999999"] == "My Assignment"


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
            await pilot.press("?")
            await pilot.pause()
            assert not app.screen.query(HelpPanel), "toggle must close the panel"
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
    # P1: assignment quick-setup modal + import flow (config.toml + aliases)
    await _check_assignment_setup()
    await _check_import_flow()
    print("tata_modal check OK")


if __name__ == "__main__":
    asyncio.run(main())
