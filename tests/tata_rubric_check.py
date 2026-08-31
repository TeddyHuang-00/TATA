"""Runnable headless check for the Rubrics pane (Library tab, F2).

Mounts the full :class:`src.tui.app.TataApp` over a tmp fixture
(data/rubrics empty or with one sample file), switches to the Library tab and
drives :class:`src.tui.library.RubricsPane` directly (no push_screen).
Asserts:

- the rubric file Select lists the library plus a "New rubric…" option, and
  selecting an existing file loads its criteria into the table;
- Add appends a criterion row, Remove deletes it;
- the custom_scale input is disabled unless grading == custom;
- Save writes a ``[[criterion]]`` TOML that :func:`src.rubric.get_rubric_definition`
  reads back (and the new file appears in the Select); a custom-scale length
  mismatch fails validation without writing anything;
- a pure round-trip: the saved TOML validates back to the same definition.

Run: uv run tests/tata_rubric_check.py
"""

from __future__ import annotations

import asyncio
import tempfile
import tomllib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from e2e_common import wait_for  # isort: skip - seeds repo-root sys.path before src imports
from src.shared.rubric import RubricDefinition, get_rubric_definition
from src.tui.app import TataApp
from src.tui.library import FileNameModal, RubricsPane
from src.tui.workspace import ConfirmationModal
from textual.containers import ScrollableContainer
from textual.pilot import Pilot
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


def _build_fixture(root: Path, *, with_rubric: bool) -> None:
    rubrics = root / "data" / "rubrics"
    rubrics.mkdir(parents=True)
    if with_rubric:
        (rubrics / "sample.toml").write_text(SAMPLE_TOML, encoding="utf-8")


def _add_referencing_config(root: Path) -> None:
    """One assignment config referencing rubrics/sample.toml."""
    course = root / "data" / "c1"
    course.mkdir()
    (course / "config.toml").write_text(
        "[fetch]\ncourse_id = 111111\n", encoding="utf-8"
    )
    (course / "000001").mkdir()
    (course / "000001" / "config.toml").write_text(
        '[grading]\nrubric = "rubrics/sample.toml"\n', encoding="utf-8"
    )


def _modal_message(app: TataApp) -> str:
    """The ConfirmationModal message Static (second Static in .confirm-modal)."""
    return str(app.screen.query_one(".confirm-modal").query(Static)[1].content)


async def _click_pane_button(pilot: Pilot, pane: RubricsPane, selector: str) -> None:
    """Scroll the pane to the bottom, then click — the actions row can sit
    below the visible scroll viewport at 120x44."""
    pane.query_one(ScrollableContainer).scroll_end(animate=False)
    await pilot.pause()
    await pilot.click(selector)


@asynccontextmanager
async def _library_app(
    root: Path,
) -> AsyncIterator[tuple[TataApp, Pilot, RubricsPane]]:
    """TataApp with the Library tab activated; yields (app, pilot, pane)."""
    app = TataApp(root_dir=root)
    async with app.run_test(size=(120, 44)) as pilot:
        await wait_for(pilot, lambda: app.query_one("#shell-tabs").display)
        app.switch_tab("tab-library")
        await pilot.pause()
        yield app, pilot, app.query_one(RubricsPane)


def _error_text(pane: RubricsPane) -> str:
    return str(pane.query_one("#rb-error", Static).content)


def _set_form(
    pane: RubricsPane,
    *,
    name: str,
    desc: str,
    pts: str,
    rating: str = "binary",
    grading: str = "standard",
    scale: str = "",
) -> None:
    pane.query_one("#rb-name", Input).value = name
    pane.query_one("#rb-desc", TextArea).text = desc
    pane.query_one("#rb-pts", Input).value = pts
    pane.query_one("#rb-rating", Select).value = rating
    pane.query_one("#rb-grading", Select).value = grading
    pane.query_one("#rb-scale", Input).value = scale


async def _check_file_select(root: Path) -> None:
    """The file Select lists library rubrics + New; picking one loads criteria."""
    async with _library_app(root) as (_app, pilot, pane):
        file_select = pane.query_one("#rb-file", Select)
        assert file_select.value == "sample.toml"
        assert any(v == "__new__" for _, v in file_select._options)
        table = pane.query_one("#rb-criteria", DataTable)
        assert table.row_count == 1
        assert sorted(pane._criteria[0]) == [
            "desc",
            "grading",
            "name",
            "pts",
            "rating",
        ]
        assert pane._criteria[0]["name"] == "Reflection"
        # switching to New shows the filename input; back restores the file
        file_select.value = "__new__"
        await pilot.pause()
        assert pane.query_one("#rb-filename", Input).display
        pane.query_one("#rb-filename", Input).value = "copy"
        file_select.value = "sample.toml"
        await pilot.pause()
        assert not pane.query_one("#rb-filename", Input).display
        assert len(pane._criteria) == 1  # reloaded from disk, not the new name


async def _check_add_remove(root: Path) -> None:
    """Empty library: New preselected; Add/Remove drive the criteria table."""
    async with _library_app(root) as (_app, _pilot, pane):
        file_select = pane.query_one("#rb-file", Select)
        assert file_select.value == "__new__"
        assert pane.query_one("#rb-filename", Input).display

        _set_form(pane, name="Logic", desc="Branch logic.", pts="5")
        pane.action_add()
        table = pane.query_one("#rb-criteria", DataTable)
        assert table.row_count == 1
        assert pane._criteria[0]["name"] == "Logic"
        assert pane._criteria[0]["pts"] == 5

        table.move_cursor(row=0)
        pane.action_remove()
        assert table.row_count == 0
        assert pane._criteria == []


async def _check_custom_scale_enabled(root: Path) -> None:
    """custom_scale is enabled only when grading == custom."""
    async with _library_app(root) as (_app, pilot, pane):
        scale = pane.query_one("#rb-scale", Input)
        assert scale.disabled  # grading defaults to standard
        pane.query_one("#rb-grading", Select).value = "custom"
        await pilot.pause()
        assert not scale.disabled
        pane.query_one("#rb-grading", Select).value = "strict"
        await pilot.pause()
        assert scale.disabled


async def _check_save_roundtrip(root: Path) -> None:
    """Save writes a [[criterion]] TOML readable by the rubric model."""
    async with _library_app(root) as (_app, _pilot, pane):
        _set_form(
            pane,
            name="Reflection",
            desc="3-5 sentences.",
            pts="10",
            rating="ternary",
            grading="standard",
        )
        pane.action_add()
        _set_form(
            pane,
            name="Bonus",
            desc="Optional.",
            pts="5",
            rating="binary",
            grading="custom",
            scale="0, 5",
        )
        pane.action_add()
        pane.query_one("#rb-filename", Input).value = "new-rubric"
        pane.action_save()

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
        # the pane stays mounted and the new file is selectable
        assert any(
            v == "new-rubric.toml"
            for _, v in pane.query_one("#rb-file", Select)._options
        )
        assert pane.query_one("#rb-file", Select).value == "new-rubric.toml"


async def _check_validation_error(root: Path) -> None:
    """Custom-scale length mismatch: error + notify, nothing written."""
    async with _library_app(root) as (_app, pilot, pane):
        _set_form(
            pane,
            name="Broken",
            desc="d",
            pts="5",
            rating="binary",  # needs 2 values
            grading="custom",
            scale="0.5",
        )
        pane.action_add()
        pane.query_one("#rb-filename", Input).value = "broken"
        pane.action_save()
        await pilot.pause()
        assert "Length of custom grading scale" in _error_text(pane)
        assert not (root / "data" / "rubrics" / "broken.toml").exists()


async def _check_delete(root: Path) -> None:
    """Delete: cancel keeps the file + reference count in the message."""
    async with _library_app(root) as (app, pilot, pane):
        file_select = pane.query_one("#rb-file", Select)
        assert file_select.value == "sample.toml"
        await _click_pane_button(pilot, pane, "#rb-delete")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationModal))
        assert "1 assignment config(s) reference rubrics/sample.toml" in _modal_message(
            app
        )
        await pilot.click("#cancel")
        await wait_for(pilot, lambda: not isinstance(app.screen, ConfirmationModal))
        assert (root / "data" / "rubrics" / "sample.toml").is_file()
        assert file_select.value == "sample.toml"

        await _click_pane_button(pilot, pane, "#rb-delete")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationModal))
        await pilot.click("#delete")
        await wait_for(pilot, lambda: not isinstance(app.screen, ConfirmationModal))
        assert not (root / "data" / "rubrics" / "sample.toml").exists()
        # empty library: Select falls back to New
        assert file_select.value == "__new__"
        assert pane.query_one("#rb-criteria", DataTable).row_count == 0


async def _check_rename(root: Path) -> None:
    """Rename: new file written, old removed, Select + criteria refreshed."""
    async with _library_app(root) as (app, pilot, pane):
        file_select = pane.query_one("#rb-file", Select)
        await _click_pane_button(pilot, pane, "#rb-rename")
        await wait_for(pilot, lambda: isinstance(app.screen, FileNameModal))
        app.screen.query_one("#fnm-input", Input).value = "renamed"
        await pilot.click("#ok")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationModal))
        assert "1 assignment config(s) reference rubrics/sample.toml" in _modal_message(
            app
        )
        await pilot.click("#rename")
        await wait_for(pilot, lambda: not isinstance(app.screen, ConfirmationModal))
        await pilot.pause()
        assert not (root / "data" / "rubrics" / "sample.toml").exists()
        assert (root / "data" / "rubrics" / "renamed.toml").read_text(
            encoding="utf-8"
        ) == SAMPLE_TOML
        assert file_select.value == "renamed.toml"
        assert pane.query_one("#rb-criteria", DataTable).row_count == 1
        config = (root / "data" / "c1" / "000001" / "config.toml").read_text(
            encoding="utf-8"
        )
        assert 'rubric = "rubrics/renamed.toml"' in config
        assert "rubrics/sample.toml" not in config


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
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_fixture(root, with_rubric=True)
        _add_referencing_config(root)
        await _check_delete(root)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_fixture(root, with_rubric=True)
        _add_referencing_config(root)
        await _check_rename(root)
    print("tata rubric check OK")


if __name__ == "__main__":
    asyncio.run(main())
