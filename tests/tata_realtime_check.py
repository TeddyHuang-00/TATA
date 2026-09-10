"""Runnable headless check for cross-panel realtime refresh (TATA).

User-reported stale-panel bugs (settings edits not reflected in the
Dashboard workspace panel; deleted rubrics still offered in the Settings
dropdown; new providers missing until restart) — fixed 2026-09-09. This
check drives the FULL TataApp and asserts (v9: settings is a fullscreen
`push_screen` view opened with `,`, closed with esc):

- S1: Settings save (assignment level, grading.max_parallel_tasks 4 -> 7),
  close settings -> the workspace config panel shows the new value.
- S2: Library Rubrics delete a rubric, open Settings at the assignment
  level -> the rubric Select no longer offers the deleted file.
- S3: Library Providers add a provider, open Settings at the assignment
  level -> the provider Select offers the new provider (mount-time registry
  snapshot regression).
- S4: Settings save (course level, plagiarism.display_threshold 0.5 -> 0.8),
  close settings on the course dashboard -> the plagiarism pane topbar
  shows the new threshold.

Provider reads are made hermetic: the app defaults to the repo's real
``data/providers``; the check points both ``providers_pane.REPO_ROOT`` and
``provider.PROJECT_ROOT`` at the tmp fixture (module globals resolved at
call time) so no provider data outside the tmp fixture is ever read or
written. One exception: ``src.shared.provider`` runs
``dotenv.load_dotenv(PROJECT_ROOT / \".env\")`` at import time (before the
check's patch applies), so the repo's ``.env`` IS read on import — a
read-only import side effect shared by every check script, not by the
runtime under test.

Run: uv run tests/tata_realtime_check.py
"""

from __future__ import annotations

import asyncio
import re
import tempfile
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from e2e_common import (  # isort: skip - seeds repo-root sys.path before src imports
    COURSE,
    make_course,
    text,
    wait_for,
)
from src.tui.app import TataApp
from src.tui.library import LibraryScreen
from src.tui.plagiarism import PlagiarismViewScreen
from src.tui.settings import SettingsScreen
from src.tui.workspace import ConfirmationModal
from textual.pilot import Pilot
from textual.widgets import Button, DataTable, Input, Select, Static, TabbedContent

PROVIDER_OLLAMA = (
    'base_url = "http://localhost:11434/v1"\n'
    'api_key = "ollama"\n'
    'model = "qwen3.8:latest"\n'
    'mode = "markdown_json_mode"\n'
)

RUBRIC_TOML = (
    "[[criterion]]\n"
    'name = "Reflection"\n'
    'desc = "A generic description."\n'
    "pts = 10\n"
    'rating = "ternary"\n'
    'grading = "standard"\n'
)

ASSIGNMENT_CFG = """[grading]
rubric = "rubrics/a1.toml"
system_prompt = "prompt/system.md"
provider = "ollama"
max_parallel_tasks = 4

[processing]
remove_base64_images = false
"""


def _fix(root: Path) -> None:
    """tmp fixture: c1-first/a1 with a loadable assignment config; rubrics
    a1 (referenced) + a2 (deleted by S2); prompt; provider; course-level
    ``[plagiarism] display_threshold = 0.5`` (S4 baseline)."""
    data = root / "data"
    make_course(
        data,
        assignments={"a1": 1001},
        assignment_cfg=ASSIGNMENT_CFG,
        pairs="minimal",
    )
    rubrics = data / "rubrics"
    rubrics.mkdir()
    (rubrics / "a1.toml").write_text(RUBRIC_TOML, encoding="utf-8")
    (rubrics / "a2.toml").write_text(RUBRIC_TOML, encoding="utf-8")
    (data / "prompt").mkdir()
    (data / "prompt" / "system.md").write_text("# System\n", encoding="utf-8")
    providers = data / "providers"
    providers.mkdir()
    (providers / "ollama.toml").write_text(PROVIDER_OLLAMA, encoding="utf-8")
    course_cfg = data / COURSE / "config.toml"
    course_cfg.write_text(
        course_cfg.read_text(encoding="utf-8")
        + "\n[plagiarism]\ndisplay_threshold = 0.5\n",
        encoding="utf-8",
    )


@contextmanager
def _hermetic_providers(root: Path) -> Iterator[None]:
    """Point provider reads/writes at the tmp data root (the app defaults to
    the repo's data/providers; the check must never touch real data). Both
    globals are resolved at call time, so an instance patched before compose
    suffices."""
    import src.shared.provider as provider_mod
    import src.tui.providers_pane as pane_mod

    orig = (provider_mod.PROJECT_ROOT, pane_mod.REPO_ROOT)
    provider_mod.PROJECT_ROOT = root
    pane_mod.REPO_ROOT = root
    try:
        yield
    finally:
        provider_mod.PROJECT_ROOT, pane_mod.REPO_ROOT = orig


async def _enter_course(app: TataApp, pilot: Pilot) -> None:
    """Global -> course level via the dashboard table (first row)."""
    table = app.query_one("#dashboard-table", DataTable)
    table.focus()
    await pilot.press("enter")
    await pilot.pause()
    assert app.state.dashboard_level == "course", app.state.dashboard_level


async def _s1_settings_save_refreshes_workspace(root: Path) -> None:
    """User case 1: setting update in Settings -> Dashboard workspace panel."""
    with _hermetic_providers(root):
        app = TataApp(root_dir=root)
        async with app.run_test(size=(120, 44)) as pilot:
            await wait_for(pilot, lambda: app.query_one(DataTable).row_count > 0)
            await _enter_course(app, pilot)
            await pilot.press("enter")
            await pilot.pause()
            assert app.state.dashboard_level == "assignment"
            body = text(app.query_one("#config-body", Static))
            assert re.search(r"max_parallel\[/b\]\s+4", body), body

            await pilot.press("comma")
            await pilot.pause()
            settings = app.screen
            assert isinstance(settings, SettingsScreen), settings
            assert settings.current_context == "assignment"
            max_parallel = settings.query_one("#f-grading-max_parallel_tasks", Input)
            assert max_parallel.value == "4"
            max_parallel.value = "7"
            settings.action_save()
            await pilot.pause()
            saved = tomllib.loads(
                (root / "data" / COURSE / "a1" / "config.toml").read_text(
                    encoding="utf-8"
                )
            )
            assert saved["grading"]["max_parallel_tasks"] == 7

            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, SettingsScreen)
            body = text(app.query_one("#config-body", Static))
            assert re.search(r"max_parallel\[/b\]\s+7", body), body


async def _s2_deleted_rubric_gone_from_settings(root: Path) -> None:
    """User case 2: deleted rubric no longer offered by the Settings select."""
    with _hermetic_providers(root):
        app = TataApp(root_dir=root)
        async with app.run_test(size=(120, 44)) as pilot:
            app.switch_tab("tab-library")
            await pilot.pause()
            library = app.query_one(LibraryScreen)
            library.query_one("#library-tabs", TabbedContent).active = "tab-rubrics"
            await pilot.pause()
            file_select = library.query_one("#rb-file", Select)
            file_select.value = "a2.toml"
            await pilot.pause()
            library.query_one("#rb-delete", Button).press()
            await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationModal))
            await pilot.click("#delete")
            await wait_for(pilot, lambda: not isinstance(app.screen, ConfirmationModal))
            assert not (root / "data" / "rubrics" / "a2.toml").exists()

            app.switch_tab("tab-dashboard")
            await pilot.pause()
            # grading Selects live at the assignment level (v9 level scoping)
            await _enter_course(app, pilot)
            await pilot.press("enter")
            await pilot.pause()
            assert app.state.dashboard_level == "assignment"
            await pilot.press("comma")
            await pilot.pause()
            settings = app.screen
            assert isinstance(settings, SettingsScreen), settings
            assert settings.current_context == "assignment"
            rubric = settings.query_one("#f-grading-rubric", Select)
            values = {value for _, value in rubric._options}  # type: ignore[attr-defined]
            assert "rubrics/a1.toml" in values, values
            assert "rubrics/a2.toml" not in values, values


async def _s3_added_provider_shows_in_settings(root: Path) -> None:
    """User case 3: provider added in Library appears in Settings (no more
    mount-time registry snapshot)."""
    with _hermetic_providers(root):
        app = TataApp(root_dir=root)
        async with app.run_test(size=(120, 44)) as pilot:
            app.switch_tab("tab-library")
            await pilot.pause()
            library = app.query_one(LibraryScreen)
            library.query_one("#library-tabs", TabbedContent).active = "tab-providers"
            await pilot.pause()
            name_select = library.query_one("#pv-name", Select)
            name_select.value = "__new__"
            await pilot.pause()
            library.query_one("#pv-new-name", Input).value = "pilot"
            library.query_one("#pv-base-url", Input).value = "http://localhost:9999/v1"
            library.query_one("#pv-api-key", Input).value = "${TEST_API_KEY}"
            library.query_one("#pv-model", Input).value = "pilot-model"
            library.query_one("#pv-mode", Select).value = "tool_call"
            library.query_one("#pv-temperature", Input).value = "0.5"
            library.query_one("#pv-save", Button).press()
            await pilot.pause()
            assert (root / "data" / "providers" / "pilot.toml").is_file()

            app.switch_tab("tab-dashboard")
            await pilot.pause()
            # provider Select lives at the assignment level (v9 level scoping)
            await _enter_course(app, pilot)
            await pilot.press("enter")
            await pilot.pause()
            assert app.state.dashboard_level == "assignment"
            await pilot.press("comma")
            await pilot.pause()
            settings = app.screen
            assert isinstance(settings, SettingsScreen), settings
            provider = settings.query_one("#f-grading-provider", Select)
            values = {value for _, value in provider._options}  # type: ignore[attr-defined]
            assert values == {"ollama", "pilot"}, values


async def _s4_threshold_change_updates_plag_pane(root: Path) -> None:
    """Settings plagiarism threshold save -> the pushed plagiarism view's
    pane topbar updates (v9: the pane lives in the fullscreen view)."""
    with _hermetic_providers(root):
        app = TataApp(root_dir=root)
        async with app.run_test(size=(120, 44)) as pilot:
            await wait_for(pilot, lambda: app.query_one(DataTable).row_count > 0)
            await _enter_course(app, pilot)
            table = app.query_one("#dashboard-table", DataTable)
            table.focus()
            await pilot.press("p")
            await wait_for(pilot, lambda: isinstance(app.screen, PlagiarismViewScreen))
            topbar = text(app.screen.query_one("#plag-topbar", Static))
            assert "display threshold 50%" in topbar, topbar
            await pilot.press("escape")
            await wait_for(
                pilot, lambda: not isinstance(app.screen, PlagiarismViewScreen)
            )

            table.focus()
            await pilot.press("comma")
            await pilot.pause()
            settings = app.screen
            assert isinstance(settings, SettingsScreen), settings
            assert settings.current_context == "course"
            thr = settings.query_one("#f-plagiarism-display_threshold", Input)
            assert thr.value == "0.5"
            thr.value = "0.8"
            settings.action_save()
            await pilot.pause()

            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, SettingsScreen)
            table.focus()
            await pilot.press("p")
            await wait_for(pilot, lambda: isinstance(app.screen, PlagiarismViewScreen))
            topbar = text(app.screen.query_one("#plag-topbar", Static))
            assert "display threshold 80%" in topbar, topbar
            await pilot.press("escape")
            await wait_for(
                pilot, lambda: not isinstance(app.screen, PlagiarismViewScreen)
            )


async def main() -> None:
    for check in (
        _s1_settings_save_refreshes_workspace,
        _s2_deleted_rubric_gone_from_settings,
        _s3_added_provider_shows_in_settings,
        _s4_threshold_change_updates_plag_pane,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fix(root)
            await check(root)
    print("tata realtime check OK")


if __name__ == "__main__":
    asyncio.run(main())
