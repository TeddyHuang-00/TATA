"""Runnable headless check for the TATA Settings screen (S5, T6b).

Mounts :class:`src.tata_settings.SettingsScreen` in a minimal test App (the
TataApp wiring lands in T6c) over a tmp three-layer fixture (global /
course / assignment ``config.toml``) and asserts:

- the no-course state: Global-only context, disabled Course/Assignment fields,
  English hint;
- the two/three-context select and field state per context;
- assignment-context save writes only the edited keys (toml load verified),
  the weight-sum validation rejects sum != 1 (no write), and the saved file
  parses through :func:`src.assignment_config.load_assignment_file`;
- course-context save preserves ``[[fetch.assignments]]``;
- global-context save keeps the other keys;
- reset (r) and the Canvas test env guard (t).

Run: uv run tests/tata_settings_check.py
"""

from __future__ import annotations

import asyncio
import tempfile
import tomllib
from pathlib import Path

from e2e_common import wait_for  # isort: skip - seeds repo-root sys.path before src imports
from src.assignment_config import load_assignment_file
from src.config_edit import dump_toml
from src.tata_app import AppState
from src.tata_scan import scan_courses
from src.tata_settings import SettingsScreen
from textual.app import App, ComposeResult
from textual.widgets import Checkbox, Input, Select, Static, TabbedContent

GLOBAL_TOML = "[plagiarism]\ncopydetect_weight = 0.9\nembedding_weight = 0.1\ndisplay_threshold = 0.75\n"

COURSE_TOML = """[fetch]
course_id = 111111
mode = "attach"

[[fetch.assignments]]
assignment_id = 1001
out = "a1/raw"
"""

ASSIGNMENT_TOML = """[grading]
rubric = "rubrics/a1.toml"
system_prompt = "prompt/system.md"
provider = "ollama"
max_parallel_tasks = 4

[fetch]
assignment_id = 1001

[assignment]
raw_dir = "incoming"
logs_dir = "logs"

[plagiarism]
copydetect_weight = 0.95
embedding_weight = 0.05
extensions = [".py"]

[processing]
remove_base64_images = false
strip_canvas_suffix = false
"""

_ASSIGNMENT_CFG = "data/c1/a1/config.toml"
_COURSE_CFG = "data/c1/config.toml"
_GLOBAL_CFG = "data/config.toml"


class _SettingsTestApp(App[None]):
    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state

    def compose(self) -> ComposeResult:
        yield SettingsScreen(self.state)


def _close(a: float, b: float) -> bool:
    return abs(a - b) < 1e-9


def _build_fixture(root: Path) -> None:
    (root / "data").mkdir(parents=True)
    (root / "data" / "config.toml").write_text(GLOBAL_TOML, encoding="utf-8")
    course_dir = root / "data" / "c1"
    course_dir.mkdir()
    (course_dir / "config.toml").write_text(COURSE_TOML, encoding="utf-8")
    a_dir = course_dir / "a1"
    a_dir.mkdir()
    (a_dir / "config.toml").write_text(ASSIGNMENT_TOML, encoding="utf-8")


def _make_state(root: Path, *, course: bool, assignment: bool) -> AppState:
    state = AppState(root_dir=root)
    state.env_state = {"has_env": False, "base_url": None, "token_set": False}
    if course:
        state.current_course = scan_courses(state.assignments_dir)[0]
    if assignment:
        assert state.current_course is not None
        state.load_assignments(state.current_course)
        state.current_assignment = state.assignments[0]
    return state


def _status_text(screen: SettingsScreen) -> str:
    return str(screen.query_one("#settings-status", Static).content)


def _check_dump_roundtrip() -> None:
    """The custom TOML writer must round-trip the shapes our configs hold."""
    original = {
        "fetch": {
            "course_id": 111111,
            "mode": "attach",
            "assignments": [
                {"assignment_id": 1001, "out": "a1/raw"},
                {"assignment_id": 1002, "mode": "text", "out": "b1/raw"},
            ],
        },
        "plagiarism": {
            "copydetect_weight": 0.9,
            "embedding_weight": 0.1,
            "extensions": [".py", ".ipynb"],
        },
        "hooks": {
            "dir": "hooks",
            "mounts": {
                "before_preprocess": "scripts/x.py",
                "after_score": ["a.py", "b.py"],
            },
        },
        "processing": {"remove_base64_images": True, "screenshot_pages": 2},
    }
    parsed = tomllib.loads(dump_toml(original))
    assert parsed == original, parsed


async def _check_no_course(root: Path) -> None:
    """No course selected: Global-only context, everything else disabled."""
    state = _make_state(root, course=False, assignment=False)
    app = _SettingsTestApp(state)
    async with app.run_test(size=(120, 44)) as pilot:
        await pilot.pause()
        screen = app.query_one(SettingsScreen)
        assert screen.current_context == "global"
        assert screen.available_contexts() == ["global"]
        assert [v for v, _ in screen.context_options()] == ["global"]
        assert screen.query_one("#ctx-select", Select).value == "global"
        assert screen.query_one("#f-grading-rubric", Input).disabled
        assert screen.query_one("#f-fetch-course_id", Input).disabled
        assert screen.query_one("#f-assignment-raw_dir", Input).disabled
        # global config exists -> the Global [plagiarism] defaults are editable
        assert not screen.query_one("#f-plagiarism-copydetect_weight", Input).disabled
        hint = screen.query_one("#ctx-hint", Static)
        assert "No course selected" in str(hint.content), hint.content
        assert "Will write:" in _status_text(screen)
        assert "Canvas .env: not found" in str(
            screen.query_one("#canvas-env", Static).content
        )


async def _check_course_only(root: Path) -> None:
    """Course selected, no assignment: Course fields editable, others disabled."""
    state = _make_state(root, course=True, assignment=False)
    app = _SettingsTestApp(state)
    async with app.run_test(size=(120, 44)) as pilot:
        await pilot.pause()
        screen = app.query_one(SettingsScreen)
        assert screen.available_contexts() == ["global", "course"]
        screen.set_context("course")
        await pilot.pause()
        assert screen.current_context == "course"
        course_id = screen.query_one("#f-fetch-course_id", Input)
        assert not course_id.disabled
        assert course_id.value == "111111"
        assert screen.query_one("#f-fetch-mode", Select).value == "attach"
        assert screen.query_one("#f-grading-rubric", Input).disabled
        assert screen.query_one("#f-assignment-raw_dir", Input).disabled
        assert not screen.query_one("#f-plagiarism-copydetect_weight", Input).disabled
        assert "a1/raw" in str(screen.query_one("#canvas-fetch-list", Static).content)


async def _check_assignment_load_and_save(root: Path) -> None:
    """Assignment context: layered values shown, ctrl+s writes edited keys."""
    state = _make_state(root, course=True, assignment=True)
    app = _SettingsTestApp(state)
    async with app.run_test(size=(120, 44)) as pilot:
        await pilot.pause()
        screen = app.query_one(SettingsScreen)
        assert [v for v, _ in screen.context_options()] == [
            "global",
            "course",
            "assignment",
        ]
        screen.set_context("assignment")
        await pilot.pause()
        assert screen.current_context == "assignment"

        rubric = screen.query_one("#f-grading-rubric", Input)
        assert not rubric.disabled
        assert rubric.value == "rubrics/a1.toml"
        assert screen.query_one("#f-grading-provider", Select).value == "ollama"
        copydetect = screen.query_one("#f-plagiarism-copydetect_weight", Input)
        assert copydetect.value == "0.95"  # assignment wins over the global 0.9
        assert (
            screen.query_one("#f-plagiarism-display_threshold", Input).value == "0.75"
        )  # global-layer default
        assert screen.query_one("#f-plagiarism-extensions", Input).value == ".py"
        assert (
            screen.query_one("#f-processing-remove_base64_images", Checkbox).value
            is False
        )

        copydetect.value = "0.8"
        screen.query_one("#f-plagiarism-embedding_weight", Input).value = "0.2"
        await pilot.press("ctrl+s")
        await wait_for(pilot, lambda: "Saved to" in _status_text(screen))
        assignment_cfg = root / _ASSIGNMENT_CFG
        saved = tomllib.loads(assignment_cfg.read_text(encoding="utf-8"))
        assert _close(saved["plagiarism"]["copydetect_weight"], 0.8)
        assert _close(saved["plagiarism"]["embedding_weight"], 0.2)
        assert saved["plagiarism"]["extensions"] == [".py"]
        assert saved["grading"]["rubric"] == "rubrics/a1.toml"
        # the saved file parses through the existing layered loader
        parsed = load_assignment_file(assignment_cfg)
        assert _close(
            parsed.plagiarism.copydetect_weight + parsed.plagiarism.embedding_weight,
            1.0,
        )


async def _check_validation_reset(root: Path) -> None:
    """Sum != 1 rejected (no write); reset reloads; Canvas test env guard."""
    state = _make_state(root, course=True, assignment=True)
    app = _SettingsTestApp(state)
    async with app.run_test(size=(120, 44)) as pilot:
        await pilot.pause()
        screen = app.query_one(SettingsScreen)
        screen.set_context("assignment")
        await pilot.pause()
        copydetect = screen.query_one("#f-plagiarism-copydetect_weight", Input)
        assert copydetect.value == "0.8"  # from the previous save

        copydetect.value = "0.9"
        screen.query_one("#f-plagiarism-embedding_weight", Input).value = "0.3"
        screen.action_save()
        await pilot.pause()
        assert "≠ 1.00" in _status_text(screen), _status_text(screen)
        assignment_cfg = root / _ASSIGNMENT_CFG
        saved = tomllib.loads(assignment_cfg.read_text(encoding="utf-8"))
        assert _close(saved["plagiarism"]["copydetect_weight"], 0.8)  # unchanged

        rubric = screen.query_one("#f-grading-rubric", Input)
        rubric.value = "zzz.toml"
        await pilot.press("r")
        await pilot.pause()
        assert rubric.value == "rubrics/a1.toml"

        screen.action_tab_canvas()
        assert app.query_one("#settings-tabs", TabbedContent).active == "tab-canvas"
        screen.action_test_canvas()
        await pilot.pause()
        assert ".env missing" in _status_text(screen)


async def _check_course_edit(root: Path) -> None:
    """Course context: mode edit keeps course_id and [[fetch.assignments]]."""
    state = _make_state(root, course=True, assignment=True)
    app = _SettingsTestApp(state)
    async with app.run_test(size=(120, 44)) as pilot:
        await pilot.pause()
        screen = app.query_one(SettingsScreen)
        screen.set_context("course")
        await pilot.pause()
        screen.query_one("#f-fetch-mode", Select).value = "text"
        screen.action_save()
        await pilot.pause()
        course_cfg = root / _COURSE_CFG
        saved_course = tomllib.loads(course_cfg.read_text(encoding="utf-8"))
        assert saved_course["fetch"]["mode"] == "text"
        assert saved_course["fetch"]["course_id"] == 111111
        assert len(saved_course["fetch"]["assignments"]) == 1
        assert saved_course["fetch"]["assignments"][0]["out"] == "a1/raw"
        load_assignment_file(root / _ASSIGNMENT_CFG)  # still parses


async def _check_global_edit(root: Path) -> None:
    """Global context: threshold edit keeps the other global keys."""
    state = _make_state(root, course=True, assignment=True)
    app = _SettingsTestApp(state)
    async with app.run_test(size=(120, 44)) as pilot:
        await pilot.pause()
        screen = app.query_one(SettingsScreen)
        screen.set_context("global")
        await pilot.pause()
        threshold = screen.query_one("#f-plagiarism-display_threshold", Input)
        assert threshold.value == "0.75"
        threshold.value = "0.78"
        screen.action_save()
        await pilot.pause()
        global_cfg = root / _GLOBAL_CFG
        saved_global = tomllib.loads(global_cfg.read_text(encoding="utf-8"))
        assert _close(saved_global["plagiarism"]["display_threshold"], 0.78)
        assert _close(saved_global["plagiarism"]["copydetect_weight"], 0.9)
        assert _close(saved_global["plagiarism"]["embedding_weight"], 0.1)


async def _check_weights_course_global(root: Path) -> None:
    """Course/global: editing one weight uses the effective layer (M4)."""
    state = _make_state(root, course=True, assignment=True)
    app = _SettingsTestApp(state)
    async with app.run_test(size=(120, 44)) as pilot:
        await pilot.pause()
        screen = app.query_one(SettingsScreen)
        # course layer has no [plagiarism]: effective defaults 0.95/0.05; a
        # one-key edit to 0.99 sums 1.04 -> rejected, nothing written.
        screen.set_context("course")
        await pilot.pause()
        copydetect = screen.query_one("#f-plagiarism-copydetect_weight", Input)
        assert copydetect.value == "0.95"
        copydetect.value = "0.99"
        screen.action_save()
        await pilot.pause()
        assert "≠ 1.00" in _status_text(screen), _status_text(screen)
        saved = tomllib.loads((root / _COURSE_CFG).read_text(encoding="utf-8"))
        assert "plagiarism" not in saved, saved
        # global layer starts from the file's 0.9/0.1; editing only
        # copydetect to 0.95 sums 1.05 -> rejected, file unchanged.
        screen.set_context("global")
        await pilot.pause()
        copydetect = screen.query_one("#f-plagiarism-copydetect_weight", Input)
        assert copydetect.value == "0.9"
        copydetect.value = "0.95"
        screen.action_save()
        await pilot.pause()
        assert "≠ 1.00" in _status_text(screen), _status_text(screen)
        saved_global = tomllib.loads((root / _GLOBAL_CFG).read_text(encoding="utf-8"))
        assert _close(saved_global["plagiarism"]["copydetect_weight"], 0.9)


async def main() -> None:
    _check_dump_roundtrip()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_fixture(root)
        await _check_no_course(root)
        await _check_course_only(root)
        await _check_assignment_load_and_save(root)
        await _check_validation_reset(root)
        await _check_course_edit(root)
        await _check_global_edit(root)
        await _check_weights_course_global(root)
    print("tata settings check OK")


if __name__ == "__main__":
    asyncio.run(main())
