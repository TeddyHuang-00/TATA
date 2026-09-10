"""Runnable headless check for the TATA Settings screen (S5, T6b — v9 rework).

The settings screen is a fullscreen ``Screen`` whose level (``global`` /
``course`` / ``assignment``) is baked in at construction; it is pushed from
the dashboard (`,`) and popped with escape. This check mounts it in a
minimal host app over a tmp three-layer fixture (global / course / assignment
``config.toml``) and asserts:

- per-level composition: only the level's tabs/fields exist, no context
  dropdown anywhere, the header names the level's course/assignment;
- the no-course global state and the graceful empty state when
  ``data/config.toml`` is missing (save bootstraps the file);
- assignment-level save writes only the edited keys (toml load verified),
  the weight-sum validation rejects sum != 1 (no write), and the saved file
  parses through :func:`src.shared.assignment_config.load_assignment_file`;
- course-level save preserves ``[[fetch.assignments]]``;
- global-level save keeps the other keys;
- field reset (per-field button) falls back to the inherited/default value;
- the Canvas test env guard (t) and the ``.env`` save/mask flow;
- panel framing (T6): the content region carries a single round $primary
  border (the plagiarism-view panel language), unclipped at 100x30, and an
  unparseable-TOML save appends the $EDITOR repair hint to its error;
- regression (user report): with TWO courses, settings opened at course A
  then at course B shows course B in the header and target path — the old
  single-instance/ctx-dropdown design showed course A there.

Run: uv run tests/tata_settings_check.py
"""

from __future__ import annotations

import asyncio
import tempfile
import tomllib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import tomlkit

from e2e_common import make_course, wait_for  # isort: skip - seeds repo-root sys.path before src imports
from dotenv import dotenv_values
from src.shared.aliases import load_alias_file
from src.shared.assignment_config import load_assignment_file
from src.shared.config_edit import dump_toml
from src.tui.app import AppState, TataApp
from src.tui.scan import scan_courses
from src.tui.settings import (
    SettingsScreen,
    _PromptCheckList,
    _SecretInput,
    mask_secret,
)
from textual.app import App
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.pilot import Pilot
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    Select,
    Static,
    TabbedContent,
)

GLOBAL_TOML = "[plagiarism]\ncopydetect_weight = 0.9\nembedding_weight = 0.1\ndisplay_threshold = 0.75\n"

COURSE_TOML = """[fetch]
course_id = 111111

[[fetch.assignments]]
id = 1001
"""

ASSIGNMENT_TOML = """[grading]
rubric = "rubrics/a1.toml"
system_prompt = "prompt/system.md"
provider = "ollama"
max_parallel_tasks = 4

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

_LEVEL_TABS = {
    "global": ["tab-canvas", "tab-plagiarism"],
    "course": ["tab-canvas", "tab-plagiarism"],
    "assignment": ["tab-grading", "tab-plagiarism", "tab-paths"],
}

_ASSIGNMENT_CFG = "data/c1/a1/config.toml"
_COURSE_CFG = "data/c1/config.toml"
_GLOBAL_CFG = "data/config.toml"


class _SettingsTestApp(App[None]):
    """Minimal host: pushes the SettingsScreen for one level (platform shape)."""

    def __init__(self, state: AppState, ctx: str) -> None:
        super().__init__()
        self.state = state
        self.ctx = ctx

    def on_mount(self) -> None:
        self.push_screen(SettingsScreen(self.state, self.ctx, pop_on_escape=True))


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
    rubrics = root / "data" / "rubrics"
    rubrics.mkdir()
    rubric_toml = (
        "[[criterion]]\n"
        'name = "Reflection"\n'
        'desc = "A generic description."\n'
        "pts = 10\n"
        'rating = "ternary"\n'
        'grading = "standard"\n'
    )
    (rubrics / "a1.toml").write_text(rubric_toml, encoding="utf-8")
    (rubrics / "a2.toml").write_text(rubric_toml, encoding="utf-8")
    prompts = root / "data" / "prompt"
    prompts.mkdir()
    (prompts / "system.md").write_text("# System prompt\n", encoding="utf-8")
    (prompts / "lab.md").write_text("# Lab prompt\n", encoding="utf-8")


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


@asynccontextmanager
async def _open(
    state: AppState, ctx: str, size: tuple[int, int] = (120, 44)
) -> AsyncIterator[tuple[App[None], Pilot, SettingsScreen]]:
    """Run the host app with the settings screen for ``ctx`` pushed."""
    app: App[None] = _SettingsTestApp(state, ctx)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen), (
            f"settings screen not pushed for ctx={ctx!r}: {screen!r}"
        )
        assert screen.current_context == ctx
        yield app, pilot, screen


def _status_text(screen: SettingsScreen) -> str:
    return str(screen.query_one("#settings-status", Static).content)


def _title_text(screen: SettingsScreen) -> str:
    return str(screen.query_one("#settings-title", Static).content)


def _field_label(screen: SettingsScreen, fqid: str) -> str:
    return str(screen._field_label(fqid).content)


def _pane_ids(screen: SettingsScreen) -> list[str]:
    tabs = screen.query_one("#settings-tabs", TabbedContent)
    return [pane.id for pane in tabs.query("TabPane")]


def _check_mask_secret() -> None:
    """mask_secret: fixed head/tail, fixed 8-star middle, short/empty rules."""
    assert mask_secret("") == ""
    assert mask_secret("a") == "*" * 8
    assert mask_secret("x" * 8) == "*" * 8
    assert mask_secret("abcdefghijkl") == "abcd********ijkl"
    assert mask_secret("x" * 9) == "xxxx********xxxx"
    assert mask_secret("x" * 100) == "xxxx********xxxx"


def _check_dump_roundtrip() -> None:
    """The custom TOML writer must round-trip the shapes our configs hold."""
    original = {
        "fetch": {
            "course_id": 111111,
            "assignments": [
                {"id": 1001},
                {"id": 1002},
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
        "processing": {"remove_base64_images": True, "visual_evaluation": False},
    }
    parsed = tomllib.loads(dump_toml(original))
    assert parsed == original, parsed


async def _check_level_composition(root: Path) -> None:
    """Each level composes ONLY its tabs/fields; no context dropdown exists."""
    state = _make_state(root, course=True, assignment=True)
    for ctx in ("global", "course", "assignment"):
        async with _open(state, ctx) as (_app, _pilot, screen):
            assert _pane_ids(screen) == _LEVEL_TABS[ctx], (ctx, _pane_ids(screen))
            # the dropped context Select must not come back in any form
            assert not screen.query("#ctx-select")
            assert not screen.query("Select#ctx-select")
            has_canvas = "tab-canvas" in _pane_ids(screen)
            if ctx == "global":
                assert has_canvas
                assert screen.query("#canvas-env")
                assert screen.query("#canvas-url")
                assert screen.query("#f-plagiarism-copydetect_weight")
                assert not screen.query("#canvas-fetch-list")
                assert not screen.query("#f-fetch-course_id")
                assert not screen.query("#f-grading-rubric")
                assert not screen.query("#f-assignment-raw_dir")
            elif ctx == "course":
                assert has_canvas
                assert screen.query("#canvas-fetch-list")
                assert screen.query("#f-fetch-course_id")
                assert screen.query("#f-plagiarism-copydetect_weight")
                assert not screen.query("#canvas-env")
                assert not screen.query("#canvas-url")
                assert not screen.query("#f-grading-rubric")
                assert not screen.query("#f-assignment-raw_dir")
            else:
                assert not has_canvas
                assert screen.query("#f-grading-rubric")
                assert screen.query("#f-plagiarism-copydetect_weight")
                assert screen.query("#f-assignment-raw_dir")
                assert not screen.query("#canvas-env")
                assert not screen.query("#canvas-fetch-list")
                assert not screen.query("#f-fetch-course_id")


async def _check_global_level(root: Path) -> None:
    """Global level: .env + plagiarism only, values from data/config.toml."""
    state = _make_state(root, course=True, assignment=True)
    async with _open(state, "global") as (_app, _pilot, screen):
        assert _title_text(screen) == "Settings · Global"
        # no course hint: the level is fixed, nothing else is editable here
        assert str(screen.query_one("#settings-hint", Static).content) == ""
        copydetect = screen.query_one("#f-plagiarism-copydetect_weight", Input)
        assert not copydetect.disabled
        assert copydetect.value == "0.9"
        assert (
            screen.query_one("#f-plagiarism-display_threshold", Input).value == "0.75"
        )
        assert "Will write:" in _status_text(screen)
        assert str(root / _GLOBAL_CFG) in _status_text(screen)
        assert "Canvas .env: not found" in str(
            screen.query_one("#canvas-env", Static).content
        )
        # level scoping: the grading tab is gone (it belongs to assignments)
        assert screen.check_action("open_tab", (1,)) is True  # 2 = Plagiarism
        assert screen.check_action("open_tab", (2,)) is False  # no 3rd tab
        assert screen.check_action("test_canvas", ()) is True


async def _check_global_missing_file(root: Path) -> None:
    """Global level with no data/config.toml: graceful empty state + bootstrap.

    The old design disabled every field (global_blocked) and refused to
    save; now the fields stay editable, the hint explains the state, and a
    save creates the file.
    """
    (root / "data").mkdir(parents=True)
    state = AppState(root_dir=root)
    state.env_state = {"has_env": False, "base_url": None, "token_set": False}
    async with _open(state, "global") as (_app, pilot, screen):
        hint = str(screen.query_one("#settings-hint", Static).content)
        assert "No global config file" in hint, hint
        copydetect = screen.query_one("#f-plagiarism-copydetect_weight", Input)
        assert not copydetect.disabled
        assert copydetect.value == "0.95"  # schema default, nothing to inherit
        assert "(does not exist yet)" in _status_text(screen)
        # editing + saving bootstraps the file (no $EDITOR trip required)
        copydetect.value = "0.9"
        screen.query_one("#f-plagiarism-embedding_weight", Input).value = "0.1"
        await pilot.press("ctrl+s")
        await wait_for(pilot, lambda: "Saved to" in _status_text(screen))
        global_cfg = root / _GLOBAL_CFG
        assert global_cfg.is_file()
        saved = tomllib.loads(global_cfg.read_text(encoding="utf-8"))
        assert _close(saved["plagiarism"]["copydetect_weight"], 0.9)
        assert _close(saved["plagiarism"]["embedding_weight"], 0.1)


async def _check_course_level(root: Path) -> None:
    """Course level: course_id + fetch list + plagiarism; own raw values."""
    state = _make_state(root, course=True, assignment=False)
    async with _open(state, "course") as (_app, _pilot, screen):
        assert _title_text(screen) == "Settings · Course: c1 (111111)"
        course_id = screen.query_one("#f-fetch-course_id", Input)
        assert not course_id.disabled
        assert course_id.value == "111111"
        assert "1001" in str(screen.query_one("#canvas-fetch-list", Static).content)
        assert not screen.query_one("#f-plagiarism-copydetect_weight", Input).disabled
        assert "Will write:" in _status_text(screen)
        assert str(root / _COURSE_CFG) in _status_text(screen)
        # course edits never touch the lower layers' files
        assert str(root / _ASSIGNMENT_CFG) not in _status_text(screen)


async def _check_assignment_load_and_save(root: Path) -> None:
    """Assignment level: layered values shown, ctrl+s writes edited keys."""
    state = _make_state(root, course=True, assignment=True)
    async with _open(state, "assignment") as (_app, pilot, screen):
        assert _title_text(screen) == "Settings · Assignment: a1"

        rubric = screen.query_one("#f-grading-rubric", Select)
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

        # prompt checklist reflects the effective value (str -> one checked box)
        checklist = screen.query_one("#f-grading-system_prompt", _PromptCheckList)
        assert checklist.value == ["prompt/system.md"]
        assert not checklist.disabled
        # inherited badge: keys set in the LOCAL assignment config have none;
        # display_threshold comes from the global layer with its value shown.
        assert "inherited" not in _field_label(screen, "plagiarism.copydetect_weight")
        assert "inherited" not in _field_label(screen, "grading.rubric")
        assert "inherited from global: 0.75" in _field_label(
            screen, "plagiarism.display_threshold"
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
        assert saved["grading"]["system_prompt"] == "prompt/system.md"  # unchanged
        # the saved file parses through the existing layered loader
        parsed = load_assignment_file(assignment_cfg)
        assert _close(
            parsed.plagiarism.copydetect_weight + parsed.plagiarism.embedding_weight,
            1.0,
        )

        # check a second prompt + save a key that was inherited: the local
        # config now carries it, so the badge disappears after re-render.
        prompt_container = checklist.query_one("#prompt-list", Vertical)
        lab_checkbox = next(
            cb for cb in prompt_container.query(Checkbox) if str(cb.label) == "lab.md"
        )
        lab_checkbox.value = True
        display = screen.query_one("#f-plagiarism-display_threshold", Input)
        display.value = "0.78"
        await pilot.press("ctrl+s")
        await wait_for(pilot, lambda: "Saved to" in _status_text(screen))
        saved = tomllib.loads(assignment_cfg.read_text(encoding="utf-8"))
        assert saved["grading"]["system_prompt"] == [
            "prompt/system.md",
            "prompt/lab.md",
        ]  # row order (config order), not alphabetical
        assert _close(saved["plagiarism"]["display_threshold"], 0.78)
        assert "inherited" not in _field_label(screen, "plagiarism.display_threshold")


async def _check_validation_reset(root: Path) -> None:
    """Sum != 1 rejected (no write); reset reloads; Canvas test env guard."""
    state = _make_state(root, course=True, assignment=True)
    async with _open(state, "assignment") as (_app, pilot, screen):
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

        rubric = screen.query_one("#f-grading-rubric", Select)
        rubric.value = "rubrics/a2.toml"  # another existing option
        await pilot.press("r")
        await pilot.pause()
        assert rubric.value == "rubrics/a1.toml"

        # `t` (and its action) only exists where a Canvas tab does: it is
        # disabled at the assignment level (no Canvas tab here).
        assert screen.check_action("test_canvas", ()) is False

    # Canvas test env guard (course level: Canvas tab exists, .env missing)
    state = _make_state(root, course=True, assignment=True)
    async with _open(state, "course") as (_app, pilot, screen):
        screen.action_open_tab(0)  # Canvas
        assert screen.query_one("#settings-tabs", TabbedContent).active == "tab-canvas"
        screen.action_test_canvas()
        await pilot.pause()
        assert ".env missing" in _status_text(screen)


async def _check_course_edit(root: Path) -> None:
    """Course level: course_id edit keeps [[fetch.assignments]] intact."""
    state = _make_state(root, course=True, assignment=True)
    async with _open(state, "course") as (_app, pilot, screen):
        screen.query_one("#f-fetch-course_id", Input).value = "222222"
        screen.action_save()
        await pilot.pause()
        course_cfg = root / _COURSE_CFG
        saved_course = tomllib.loads(course_cfg.read_text(encoding="utf-8"))
        assert saved_course["fetch"]["course_id"] == 222222
        assert "mode" not in saved_course["fetch"], saved_course["fetch"]
        assert len(saved_course["fetch"]["assignments"]) == 1
        assert saved_course["fetch"]["assignments"][0]["id"] == 1001
        load_assignment_file(root / _ASSIGNMENT_CFG)  # still parses


async def _check_global_edit(root: Path) -> None:
    """Global level: threshold edit keeps the other global keys."""
    state = _make_state(root, course=True, assignment=True)
    async with _open(state, "global") as (_app, pilot, screen):
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


async def _check_rubric_not_in_list(root: Path) -> None:
    """A rubric value missing from data/rubrics keeps itself + a suffix hint."""
    state = _make_state(root, course=True, assignment=True)
    async with _open(state, "assignment") as (_app, pilot, screen):
        cfg = root / _ASSIGNMENT_CFG
        doc = tomlkit.parse(cfg.read_text(encoding="utf-8"))
        doc["grading"]["rubric"] = "rubrics/ghost.toml"
        cfg.write_text(tomlkit.dumps(doc), encoding="utf-8")
        screen.action_reset()
        await pilot.pause()
        rubric = screen.query_one("#f-grading-rubric", Select)
        assert rubric.value == "rubrics/ghost.toml"
        hint = next(
            (
                str(label)
                for label, value in rubric._options
                if value == "rubrics/ghost.toml"
            ),
            "",
        )
        assert "not in list" in hint, hint


async def _check_weights_course_global(root: Path) -> None:
    """Course/global: editing one weight uses the effective layer (M4)."""
    state = _make_state(root, course=True, assignment=True)
    # course layer has no [plagiarism]: effective defaults 0.95/0.05; a
    # one-key edit to 0.99 sums 1.04 -> rejected, nothing written.
    async with _open(state, "course") as (_app, pilot, screen):
        copydetect = screen.query_one("#f-plagiarism-copydetect_weight", Input)
        assert copydetect.value == "0.95"
        copydetect.value = "0.99"
        screen.action_save()
        await pilot.pause()
        assert "≠ 1.00" in _status_text(screen), _status_text(screen)
        # the $EDITOR repair hint is for unparseable TOML only, never for
        # ordinary validation failures (T6)
        assert "$EDITOR" not in _status_text(screen), _status_text(screen)
        saved = tomllib.loads((root / _COURSE_CFG).read_text(encoding="utf-8"))
        assert "plagiarism" not in saved, saved
    # global layer starts from the file's 0.9/0.1; editing only copydetect
    # to 0.95 sums 1.05 -> rejected, file unchanged.
    async with _open(state, "global") as (_app, pilot, screen):
        copydetect = screen.query_one("#f-plagiarism-copydetect_weight", Input)
        assert copydetect.value == "0.9"
        copydetect.value = "0.95"
        screen.action_save()
        await pilot.pause()
        assert "≠ 1.00" in _status_text(screen), _status_text(screen)
        saved_global = tomllib.loads((root / _GLOBAL_CFG).read_text(encoding="utf-8"))
        assert _close(saved_global["plagiarism"]["copydetect_weight"], 0.9)


async def _check_layout(root: Path) -> None:
    """P3: header + tabs visible, panes scroll, actions pinned, level-only tabs."""
    state = _make_state(root, course=True, assignment=True)
    async with _open(state, "assignment") as (_app, pilot, screen):
        # (a) the header renders inside the viewport (no context Select left)
        title = screen.query_one("#settings-title", Static)
        assert "Settings · Assignment" in str(title.content)
        assert title.visible
        region = title.region  # Textual 8: region is relative to the Screen
        assert region.y >= 0, region
        assert region.bottom <= 44, region
        assert region.width > 0, region
        assert not screen.query("#ctx-select")

        # (b) every composed TabPane's content lives in a ScrollableContainer
        tabs = screen.query_one("#settings-tabs", TabbedContent)
        for pane_id in _LEVEL_TABS["assignment"]:
            assert tabs.query_one(
                f"#{pane_id} ScrollableContainer", ScrollableContainer
            )

        # (c) Save/Reset stay inside the viewport (not pushed below the fold)
        for btn_id in ("btn-save", "btn-reset"):
            btn = screen.query_one(f"#{btn_id}", Button)
            assert btn.visible
            region = btn.region
            assert region.y >= 0, region
            assert region.bottom <= 44, region

        # (d) fixed widget heights (label 1 / input-select 3) per active tab
        assert screen.query_one("#f-grading-rubric", Select).region.height >= 3
        screen.action_open_tab(1)  # Plagiarism
        await pilot.pause()
        assert (
            screen.query_one("#f-plagiarism-copydetect_weight", Input).region.height
            >= 3
        )
        screen.action_open_tab(2)  # Paths
        await pilot.pause()
        assert (
            screen.query_one(
                "#f-processing-remove_base64_images", Checkbox
            ).region.height
            >= 3
        )

        # (e) the provider registry display block was removed
        assert not screen.query("#grading-registry")


async def _check_panel_framing(root: Path) -> None:
    """T6 framing: the pushed view frames its content region with a single
    round $primary border (the #plag-tabs panel language) and the layout
    stays unclipped at 100x30."""
    state = _make_state(root, course=True, assignment=True)
    async with _open(state, "assignment") as (_app, _pilot, screen):
        tabs = screen.query_one("#settings-tabs", TabbedContent)
        for edge in (
            tabs.styles.border_top,
            tabs.styles.border_right,
            tabs.styles.border_bottom,
            tabs.styles.border_left,
        ):
            assert edge[0] == "round", edge
            assert edge[1] is not None, edge
        # single border only: no framed shell around/inside the content
        assert screen.styles.border_top[0] == "", screen.styles.border_top

    async with _open(state, "assignment", size=(100, 30)) as (_app, _pilot, screen):
        tabs = screen.query_one("#settings-tabs", TabbedContent)
        assert tabs.region.y >= 0, tabs.region
        assert tabs.region.bottom <= 30, tabs.region
        assert tabs.size.height >= 3, tabs.size
        assert screen.query_one("#f-grading-rubric", Select).region.height >= 3
        for btn_id in ("btn-save", "btn-reset"):
            btn = screen.query_one(f"#{btn_id}", Button)
            assert btn.visible
            assert btn.region.bottom <= 30, btn.region
        status = screen.query_one("#settings-status", Static)
        assert status.region.bottom <= 30, status.region


async def _check_corrupt_config_hint(root: Path) -> None:
    """T6 MINOR: a save against unparseable TOML keeps the precise error and
    appends the $EDITOR repair pointer (e opens the file); nothing written."""
    cfg = root / _COURSE_CFG
    original = cfg.read_text(encoding="utf-8")
    cfg.write_text("this is not = = toml\n", encoding="utf-8")
    state = _make_state(root, course=True, assignment=True)
    try:
        async with _open(state, "course") as (_app, pilot, screen):
            inp = screen.query_one("#f-fetch-course_id", Input)
            inp.value = "222222"
            screen.action_save()
            await pilot.pause()
            status = _status_text(screen)
            assert "Validation failed" in status, status
            assert "invalid TOML in" in status, status
            assert "press e to open it in $EDITOR and fix it" in status, status
            assert cfg.read_text(encoding="utf-8") == "this is not = = toml\n"
    finally:
        cfg.write_text(original, encoding="utf-8")


async def _check_prompt_order(root: Path) -> None:
    """F4: prompt rows sized to content; reorder buttons drive value order."""
    state = _make_state(root, course=True, assignment=True)
    async with _open(state, "assignment") as (_app, pilot, screen):
        checklist = screen.query_one("#f-grading-system_prompt", _PromptCheckList)
        rows = checklist.query_one("#prompt-list", Vertical)
        # rows follow the effective config order (system.md first)
        assert [str(cb.label) for cb in rows.query(Checkbox)] == [
            "system.md",
            "lab.md",
        ]
        assert checklist.value == ["prompt/system.md"]
        # every row is tall enough for its controls (no clipped blank rows):
        # each control's content region has room to render its glyph/text
        assert rows.region.height == sum(row.region.height for row in rows.children), (
            rows.region
        )
        assert all(row.region.height >= 3 for row in rows.children), [
            row.region.height for row in rows.children
        ]
        assert all(cb.content_region.height > 0 for cb in rows.query(Checkbox)), [
            cb.content_region.height for cb in rows.query(Checkbox)
        ]
        assert all(btn.content_region.height > 0 for btn in rows.query(Button)), [
            btn.content_region.height for btn in rows.query(Button)
        ]
        # move the second row up: lab.md above system.md
        await pilot.click("#up-prompt-1")
        await pilot.pause()
        assert [str(cb.label) for cb in rows.query(Checkbox)] == [
            "lab.md",
            "system.md",
        ]
        assert checklist.value == ["prompt/system.md"]  # checked box moved with its row
        # check lab.md too; value order = row order
        await pilot.click("#cb-prompt-0")
        await pilot.pause()
        assert checklist.value == ["prompt/lab.md", "prompt/system.md"]
        # move the first row down; value order follows
        await pilot.click("#down-prompt-0")
        await pilot.pause()
        assert [str(cb.label) for cb in rows.query(Checkbox)] == [
            "system.md",
            "lab.md",
        ]
        assert checklist.value == ["prompt/system.md", "prompt/lab.md"]
        # save persists the row order
        screen.action_save()
        await pilot.pause()
        saved = tomllib.loads((root / _ASSIGNMENT_CFG).read_text(encoding="utf-8"))
        assert saved["grading"]["system_prompt"] == [
            "prompt/system.md",
            "prompt/lab.md",
        ]


async def _check_prompt_list_height(root: Path) -> None:
    """F4: list grows with the file set — height = rows x row height."""
    state = _make_state(root, course=True, assignment=True)
    async with _open(state, "assignment") as (_app, pilot, screen):
        checklist = screen.query_one("#f-grading-system_prompt", _PromptCheckList)
        rows = checklist.query_one("#prompt-list", Vertical)
        # 4 prompt files -> 4 rows; list height equals the sum of row heights
        prompts = root / "data" / "prompt"
        for name in ("extra_a.md", "extra_b.md"):
            (prompts / name).write_text("# extra\n", encoding="utf-8")
        checklist._refresh_files()
        await pilot.pause()
        assert [str(cb.label) for cb in rows.query(Checkbox)] == [
            "extra_a.md",
            "extra_b.md",
            "lab.md",
            "system.md",
        ]
        assert len(rows.children) == 4
        row_heights = [row.region.height for row in rows.children]
        assert rows.region.height == sum(row_heights), rows.region
        assert all(h >= 3 for h in row_heights), row_heights
        assert all(cb.content_region.height > 0 for cb in rows.query(Checkbox)), [
            cb.content_region.height for cb in rows.query(Checkbox)
        ]
        assert all(btn.content_region.height > 0 for btn in rows.query(Button)), [
            btn.content_region.height for btn in rows.query(Button)
        ]


async def _check_field_reset(root: Path) -> None:
    """F5: per-field reset deletes the key; merge view falls back."""
    state = _make_state(root, course=True, assignment=True)
    async with _open(state, "assignment") as (_app, pilot, screen):
        # reset a locally set key: the key vanishes, the global value returns
        screen.action_open_tab(1)  # Plagiarism
        await pilot.pause()
        await pilot.click("#reset-plagiarism-copydetect_weight")
        await pilot.pause()
        assignment_cfg = root / _ASSIGNMENT_CFG
        saved = tomllib.loads(assignment_cfg.read_text(encoding="utf-8"))
        assert "copydetect_weight" not in saved["plagiarism"], saved["plagiarism"]
        assert _close(saved["plagiarism"]["embedding_weight"], 0.05)  # others intact
        assert saved["plagiarism"]["extensions"] == [".py"]
        assert saved["grading"]["rubric"] == "rubrics/a1.toml"
        copydetect = screen.query_one("#f-plagiarism-copydetect_weight", Input)
        assert copydetect.value == "0.9"  # inherited from the global layer
        assert "inherited from global: 0.9" in _field_label(
            screen, "plagiarism.copydetect_weight"
        )
        # resetting a key that is not local is a no-op
        await pilot.click("#reset-plagiarism-display_threshold")
        await pilot.pause()
        saved = tomllib.loads(assignment_cfg.read_text(encoding="utf-8"))
        assert "display_threshold" not in saved.get("plagiarism", {})
        assert "no local value" in _status_text(screen)

    # course layer: reset drops course_id, schema default (None) shows
    async with _open(state, "course") as (_app, pilot, screen):
        screen.action_open_tab(0)  # Canvas
        await pilot.pause()
        await pilot.click("#reset-fetch-course_id")
        await pilot.pause()
        course_cfg = root / _COURSE_CFG
        saved_course = tomllib.loads(course_cfg.read_text(encoding="utf-8"))
        assert "course_id" not in saved_course["fetch"], saved_course["fetch"]
        assert len(saved_course["fetch"]["assignments"]) == 1
        assert screen.query_one("#f-fetch-course_id", Input).value == ""

    # global layer: reset falls back to the schema default
    async with _open(state, "global") as (_app, pilot, screen):
        screen.action_open_tab(1)  # Plagiarism
        await pilot.pause()
        await pilot.click("#reset-plagiarism-display_threshold")
        await pilot.pause()
        saved_global = tomllib.loads((root / _GLOBAL_CFG).read_text(encoding="utf-8"))
        assert "display_threshold" not in saved_global["plagiarism"], saved_global
        assert screen.query_one("#f-plagiarism-display_threshold", Input).value == "0.8"


async def _check_inherited_values(root: Path) -> None:
    """F6: inherited keys show the effective value + source in the badge."""
    state = _make_state(root, course=True, assignment=True)
    async with _open(state, "assignment") as (_app, _pilot, screen):
        # fields display the layered MERGE values (load_assignment_file)
        assert (
            screen.query_one("#f-plagiarism-copydetect_weight", Input).value == "0.95"
        )
        assert (
            screen.query_one("#f-plagiarism-display_threshold", Input).value == "0.75"
        )
        # badge names the source layer + the effective value
        assert "inherited from global: 0.75" in _field_label(
            screen, "plagiarism.display_threshold"
        )
        # schema defaults are labelled as such; unset strings as (not set)
        assert "default: template.ipynb" in _field_label(
            screen, "plagiarism.template_file"
        )
        assert "(not set)" in _field_label(screen, "assignment.processed_dir")
        # local keys keep plain labels
        assert "inherited" not in _field_label(screen, "plagiarism.copydetect_weight")
        assert "inherited" not in _field_label(screen, "grading.system_prompt")


async def _check_titles(root: Path) -> None:
    """Header: level name + alias/id via the display-name helpers."""
    state = _make_state(root, course=True, assignment=True)
    # no alias.toml: dir names, ids shown when known (assignment id is None
    # here — the dir name ``a1`` is not numeric)
    async with _open(state, "course") as (_app, _pilot, screen):
        assert _title_text(screen) == "Settings · Course: c1 (111111)"
    async with _open(state, "assignment") as (_app, _pilot, screen):
        assert _title_text(screen) == "Settings · Assignment: a1"
    async with _open(state, "global") as (_app, _pilot, screen):
        assert _title_text(screen) == "Settings · Global"
    # alias.toml at the global layer [course] and the course layer [assignment]
    (root / "data" / "alias.toml").write_text(
        '[course]\n"111111" = "Data Structures"\n', encoding="utf-8"
    )
    (root / "data" / "c1" / "alias.toml").write_text(
        '[assignment]\n"a1" = "Homework 1"\n', encoding="utf-8"
    )
    load_alias_file.cache_clear()  # the missing-file reads above are cached
    state = _make_state(root, course=True, assignment=True)
    async with _open(state, "course") as (_app, _pilot, screen):
        assert _title_text(screen) == "Settings · Course: Data Structures (111111)"
    async with _open(state, "assignment") as (_app, _pilot, screen):
        assert _title_text(screen) == "Settings · Assignment: Homework 1"


async def _check_canvas_env_edit(root: Path) -> None:
    """Canvas tab (global): save/update .env via the screen, mask the token.

    User feedback 5: the token stays plaintext in ``value`` only; the
    unfocused render and the #canvas-env static show the fixed mask; other
    .env keys and comments survive a save; save creates a missing .env.
    """
    state = _make_state(root, course=False, assignment=False)
    async with _open(state, "global") as (_app, pilot, screen):
        url_input = screen.query_one("#canvas-url", Input)
        token_input = screen.query_one("#canvas-token", _SecretInput)
        env_path = root / ".env"

        # no .env yet: fields empty, statics say not found
        assert not env_path.exists()
        assert url_input.value == ""
        assert token_input.value == ""
        assert "Canvas .env: not found" in str(
            screen.query_one("#canvas-env", Static).content
        )

        # type + Save .env -> .env created with both keys
        url_input.value = "https://canvas.test/"
        token_input.value = "abcd1234567890"
        await pilot.click("#btn-save-env")
        await wait_for(pilot, lambda: bool(state.env_state.get("has_env")))
        vals = dotenv_values(env_path, interpolate=False)
        assert vals["CANVAS_BASE_URL"] == "https://canvas.test/"
        assert vals["CANVAS_ACCESS_TOKEN"] == "abcd1234567890"

        # statics show a masked preview, never the plaintext token
        statics = str(screen.query_one("#canvas-env", Static).content)
        assert "token: abcd********7890" in statics, statics
        assert "abcd1234567890" not in statics, statics

        # unfocused: rendered content is the mask (click moved focus away)
        assert str(token_input.render_line(0).text).rstrip() == "abcd********7890"
        # focused: plaintext while editing
        token_input.focus()
        await pilot.pause()
        assert str(token_input.render_line(0).text).strip() == "abcd1234567890"
        url_input.focus()
        await pilot.pause()
        assert str(token_input.render_line(0).text).rstrip() == "abcd********7890"

        # existing keys + comments survive, and reload picks up disk edits
        with env_path.open("a", encoding="utf-8") as fh:
            fh.write("\n# local note\nDEEPSEEK_API_KEY=sk-existing\n")
        await pilot.click("#btn-reload-env")
        await pilot.pause()
        assert token_input.value == "abcd1234567890"
        token_input.value = "EFGH4567123456"
        await pilot.click("#btn-save-env")
        await wait_for(pilot, lambda: "Saved .env" in _status_text(screen))
        raw = env_path.read_text(encoding="utf-8")
        assert {"# local note", "DEEPSEEK_API_KEY=sk-existing"} <= set(
            raw.splitlines()
        ), raw
        vals = dotenv_values(env_path, interpolate=False)
        assert vals["DEEPSEEK_API_KEY"] == "sk-existing"
        assert vals["CANVAS_BASE_URL"] == "https://canvas.test/"
        assert vals["CANVAS_ACCESS_TOKEN"] == "EFGH4567123456"
        statics = str(screen.query_one("#canvas-env", Static).content)
        assert "token: EFGH********3456" in statics, statics
        assert "EFGH4567123456" not in statics, statics


async def _check_env_buttons_overflow(root: Path) -> None:
    """Canvas tab (global) in a SHORT window: env buttons keep full height.

    With the canvas content taller than the scroll viewport, Textual's
    default ``height: 1fr`` on the Horizontal collapses to 1 row and clips
    the 3-row buttons to a bare border strip (label invisible). The tcss
    override (``height: auto``) must keep the band at its content height and
    the labels rendered in the composited screen.
    """
    state = _make_state(root, course=True, assignment=True)
    async with _open(state, "global", size=(100, 26)) as (app, pilot, screen):
        sc = screen.query_one("#tab-canvas ScrollableContainer")
        # the overflow scenario is real: content taller than the viewport
        assert sc.virtual_size.height > sc.region.height, (
            sc.virtual_size,
            sc.region,
        )
        band: Horizontal = screen.query_one(
            "#tab-canvas ScrollableContainer > Horizontal"
        )
        assert band.region.height >= 3, band.region
        for btn_id in ("btn-save-env", "btn-reload-env"):
            btn = screen.query_one(f"#{btn_id}", Button)
            assert btn.region.height >= 3, btn.region
            assert btn.content_region.height > 0, btn.content_region
        # scroll the band into view and check the labels are really painted:
        # in the broken state only the 1-row top border (a color block)
        # survives, so "Save .env" / "Reload .env" are missing from the SVG.
        band_y = band.region.y - sc.region.y + sc.scroll_offset.y
        sc.scroll_to(y=max(0, band_y - 2), animate=False)
        await pilot.pause()
        svg = app.export_screenshot().replace("&#160;", " ")
        assert "Save .env" in svg, svg
        assert "Reload .env" in svg, svg

    # the assignment-level Grading tab in the same short window still sizes
    # its prompt rows to content (no clipping, order preserved)
    async with _open(state, "assignment", size=(100, 26)) as (_app, pilot, screen):
        checklist = screen.query_one("#f-grading-system_prompt", _PromptCheckList)
        rows = checklist.query_one("#prompt-list", Vertical)
        assert [str(cb.label) for cb in rows.query(Checkbox)] == [
            "system.md",
            "lab.md",
        ]
        assert rows.region.height == sum(row.region.height for row in rows.children), (
            rows.region
        )
        assert all(row.region.height >= 3 for row in rows.children), [
            row.region.height for row in rows.children
        ]
        assert all(cb.content_region.height > 0 for cb in rows.query(Checkbox)), [
            cb.content_region.height for cb in rows.query(Checkbox)
        ]
        assert all(btn.content_region.height > 0 for btn in rows.query(Button)), [
            btn.content_region.height for btn in rows.query(Button)
        ]


async def _check_course_switch_regression() -> None:
    """User report: settings at course A then course B must show B.

    With TWO courses: enter course A, open settings, verify A, close it,
    navigate into course B, open settings again — header and target path
    must show course B. The old implementation reused one settings widget
    and its stale ctx-dropdown/early-returning set_context kept showing
    course A (and `,`/push did not exist at all), so this test fails there.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        make_course(
            root / "data", course="c1-first", course_id=111111, assignments={"a1": 1001}
        )
        make_course(
            root / "data",
            course="c2-second",
            course_id=222222,
            assignments={"b1": 2001},
        )
        app = TataApp(root_dir=root)
        async with app.run_test(size=(120, 44)) as pilot:
            table = app.query_one("#dashboard-table", DataTable)
            await wait_for(pilot, lambda: table.row_count == 2)

            # earlier tasks removed the settings tab: it is a pushed screen
            assert not app.query(SettingsScreen)

            # enter course A (row 0), open settings, verify A
            table.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert app.state.current_course is not None
            assert app.state.current_course.dir_name == "c1-first"
            await pilot.press("comma")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, SettingsScreen), screen
            assert _title_text(screen) == "Settings · Course: c1-first (111111)"
            assert "data/c1-first/config.toml" in _status_text(screen)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, SettingsScreen)

            # back to global, down to course B, open settings again
            table.focus()
            await pilot.press("escape")
            await pilot.pause()
            assert app.state.dashboard_level == "global"
            table.focus()
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app.state.current_course is not None
            assert app.state.current_course.dir_name == "c2-second"
            await pilot.press("comma")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, SettingsScreen), screen
            assert _title_text(screen) == "Settings · Course: c2-second (222222)"
            assert "data/c2-second/config.toml" in _status_text(screen)
            assert "data/c1-first" not in _status_text(screen)
            # and the loaded value is course B's, not course A's
            assert screen.query_one("#f-fetch-course_id", Input).value == "222222"


async def main() -> None:
    _check_mask_secret()
    _check_dump_roundtrip()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_fixture(root)
        await _check_level_composition(root)
        await _check_global_level(root)
        await _check_course_level(root)
        await _check_assignment_load_and_save(root)
        await _check_validation_reset(root)
        await _check_course_edit(root)
        await _check_global_edit(root)
        await _check_rubric_not_in_list(root)
        await _check_weights_course_global(root)
        await _check_corrupt_config_hint(root)
        await _check_panel_framing(root)
        await _check_layout(root)
    with tempfile.TemporaryDirectory() as tmp:
        await _check_global_missing_file(Path(tmp))
    for checker in (
        _check_prompt_order,
        _check_prompt_list_height,
        _check_field_reset,
        _check_inherited_values,
        _check_titles,
        _check_canvas_env_edit,
        _check_env_buttons_overflow,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_fixture(root)
            await checker(root)
    await _check_course_switch_regression()
    print("tata settings check OK")


if __name__ == "__main__":
    asyncio.run(main())
