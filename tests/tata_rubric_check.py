"""Runnable headless check for the Rubric builder screen (Settings v2).

Pushes :class:`src.tata_rubric.RubricBuilderScreen` on a minimal App over a
tmp fixture (data/rubrics empty or with one sample file) and asserts:

- the rubric file Select lists the library plus a "New rubric…" option, and
  selecting an existing file loads its criteria into the table;
- Add appends a criterion row, Remove deletes it;
- the custom_scale input is disabled unless grading == custom;
- Save writes a ``[[criterion]]`` TOML that :func:`src.rubric.get_rubric_definition`
  reads back, and a custom-scale length mismatch fails validation without
  writing anything;
- a pure round-trip: the saved TOML validates back to the same definition.

Run: uv run tests/tata_rubric_check.py
"""

from __future__ import annotations

import asyncio
import tempfile
import tomllib
from pathlib import Path

from e2e_common import wait_for  # isort: skip - seeds repo-root sys.path before src imports
from src.rubric import RubricDefinition, get_rubric_definition
from src.tata_app import AppState
from src.tata_rubric import RubricBuilderScreen
from textual.app import App
from textual.widgets import DataTable, Input, Select, Static, TextArea

SAMPLE_TOML = (
    "# schema: ../../config/rubric.schema.json\n"
    "[[criterion]]\n"
    'name = "Reflection"\n'
    'desc = "A generic description."\n'
    "pts = 10\n"
    'rating = "ternary"\n'
    'grading = "standard"\n'
)


class _RubricTestApp(App[None]):
    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self.popped = False

    def on_mount(self) -> None:
        self.push_screen(RubricBuilderScreen(self.state), callback=self._on_closed)

    def _on_closed(self, _result: object) -> None:
        self.popped = True


def _make_state(root: Path) -> AppState:
    state = AppState(root_dir=root)
    state.env_state = {"has_env": False, "base_url": None, "token_set": False}
    return state


def _build_fixture(root: Path, *, with_rubric: bool) -> None:
    rubrics = root / "data" / "rubrics"
    rubrics.mkdir(parents=True)
    if with_rubric:
        (rubrics / "sample.toml").write_text(SAMPLE_TOML, encoding="utf-8")


def _error_text(screen: RubricBuilderScreen) -> str:
    return str(screen.query_one("#rb-error", Static).content)


def _set_form(
    screen: RubricBuilderScreen,
    *,
    name: str,
    desc: str,
    pts: str,
    rating: str = "binary",
    grading: str = "standard",
    scale: str = "",
) -> None:
    screen.query_one("#rb-name", Input).value = name
    screen.query_one("#rb-desc", TextArea).text = desc
    screen.query_one("#rb-pts", Input).value = pts
    screen.query_one("#rb-rating", Select).value = rating
    screen.query_one("#rb-grading", Select).value = grading
    screen.query_one("#rb-scale", Input).value = scale


async def _check_file_select(root: Path) -> None:
    """The file Select lists library rubrics + New; picking one loads criteria."""
    state = _make_state(root)
    app = _RubricTestApp(state)
    async with app.run_test(size=(120, 44)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, RubricBuilderScreen)
        screen = app.screen
        file_select = screen.query_one("#rb-file", Select)
        assert file_select.value == "sample.toml"
        assert any(v == "__new__" for _, v in file_select._options)
        table = screen.query_one("#rb-criteria", DataTable)
        assert table.row_count == 1
        assert sorted(screen._criteria[0]) == [
            "desc",
            "grading",
            "name",
            "pts",
            "rating",
        ]
        assert screen._criteria[0]["name"] == "Reflection"
        # switching to New shows the filename input; back restores the file
        file_select.value = "__new__"
        await pilot.pause()
        assert screen.query_one("#rb-filename", Input).display
        screen.query_one("#rb-filename", Input).value = "copy"
        file_select.value = "sample.toml"
        await pilot.pause()
        assert not screen.query_one("#rb-filename", Input).display
        assert len(screen._criteria) == 1  # reloaded from disk, not the new name


async def _check_add_remove(root: Path) -> None:
    """Empty library: New preselected; Add/Remove drive the criteria table."""
    state = _make_state(root)
    app = _RubricTestApp(state)
    async with app.run_test(size=(120, 44)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, RubricBuilderScreen)
        screen = app.screen
        file_select = screen.query_one("#rb-file", Select)
        assert file_select.value == "__new__"
        assert screen.query_one("#rb-filename", Input).display

        _set_form(screen, name="Logic", desc="Branch logic.", pts="5")
        screen.action_add()
        table = screen.query_one("#rb-criteria", DataTable)
        assert table.row_count == 1
        assert screen._criteria[0]["name"] == "Logic"
        assert screen._criteria[0]["pts"] == 5

        table.move_cursor(row=0)
        screen.action_remove()
        assert table.row_count == 0
        assert screen._criteria == []


async def _check_custom_scale_enabled(root: Path) -> None:
    """custom_scale is enabled only when grading == custom."""
    state = _make_state(root)
    app = _RubricTestApp(state)
    async with app.run_test(size=(120, 44)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, RubricBuilderScreen)
        screen = app.screen
        scale = screen.query_one("#rb-scale", Input)
        assert scale.disabled  # grading defaults to standard
        screen.query_one("#rb-grading", Select).value = "custom"
        await pilot.pause()
        assert not scale.disabled
        screen.query_one("#rb-grading", Select).value = "strict"
        await pilot.pause()
        assert scale.disabled


async def _check_save_roundtrip(root: Path) -> None:
    """Save writes a [[criterion]] TOML readable by the rubric model."""
    state = _make_state(root)
    app = _RubricTestApp(state)
    async with app.run_test(size=(120, 44)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, RubricBuilderScreen)
        screen = app.screen
        _set_form(
            screen,
            name="Reflection",
            desc="3-5 sentences.",
            pts="10",
            rating="ternary",
            grading="standard",
        )
        screen.action_add()
        _set_form(
            screen,
            name="Bonus",
            desc="Optional.",
            pts="5",
            rating="binary",
            grading="custom",
            scale="0, 5",
        )
        screen.action_add()
        screen.query_one("#rb-filename", Input).value = "new-rubric"
        screen.action_save()
        await wait_for(pilot, lambda: app.popped)

        path = root / "data" / "rubrics" / "new-rubric.toml"
        assert path.is_file()
        text = path.read_text(encoding="utf-8")
        assert "[[criterion]]" in text
        assert "criterion = [" not in text  # array of tables, not inline array
        assert "# schema: ../../config/rubric.schema.json" in text

        loaded = get_rubric_definition(path)
        assert len(loaded.criterion) == 2
        assert loaded.criterion[0].name == "Reflection"
        assert loaded.criterion[1].custom_scale == [0.0, 5.0]
        # pure round-trip: re-validating the raw TOML yields the same definition
        reparsed = RubricDefinition.model_validate(tomllib.loads(text))
        assert reparsed.model_dump() == loaded.model_dump()


async def _check_validation_error(root: Path) -> None:
    """Custom-scale length mismatch: error + notify, nothing written."""
    state = _make_state(root)
    app = _RubricTestApp(state)
    async with app.run_test(size=(120, 44)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, RubricBuilderScreen)
        screen = app.screen
        _set_form(
            screen,
            name="Broken",
            desc="d",
            pts="5",
            rating="binary",  # needs 2 values
            grading="custom",
            scale="0.5",
        )
        screen.action_add()
        screen.query_one("#rb-filename", Input).value = "broken"
        screen.action_save()
        await pilot.pause()
        assert "Length of custom grading scale" in _error_text(screen)
        assert not (root / "data" / "rubrics" / "broken.toml").exists()
        assert not app.popped


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_fixture(root, with_rubric=True)
        await _check_file_select(root)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_fixture(root, with_rubric=False)
        await _check_add_remove(root)
        await _check_custom_scale_enabled(root)
        await _check_save_roundtrip(root)
        await _check_validation_error(root)
    print("tata rubric check OK")


if __name__ == "__main__":
    asyncio.run(main())
