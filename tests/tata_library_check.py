"""Runnable headless check for the Library tab (F2).

Mounts the full :class:`src.tui.app.TataApp` over a tmp fixture
(data/rubrics + data/prompt), switches to the Library tab and asserts:

- the shell has four tabs; the Library tab hosts Rubrics + Prompts sub-tab
  panes, and the Rubrics pane lists the library rubric;
- the Prompts pane loads ``data/prompt/*.md`` into a TextArea; editing plus
  the Save button writes the file back to disk;
- prompt file management: create (draft + Save writes the file), delete
  (ConfirmationModal cancel keeps the file, confirm removes it, referencing
  assignment configs are reported in the message) and rename (new file
  written, old removed, Select re-pointed).

Run: uv run tests/tata_library_check.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from e2e_common import wait_for  # isort: skip - seeds repo-root sys.path before src imports
from src.tui.app import TataApp
from src.tui.library import FileNameModal, LibraryScreen, PromptsPane, RubricsPane
from src.tui.workspace import ConfirmationModal
from textual.widgets import Input, Select, Static, TabbedContent, TabPane, TextArea

SAMPLE_TOML = (
    "# schema: ../../config/rubric.schema.json\n"
    "[[criterion]]\n"
    'name = "Reflection"\n'
    'desc = "A generic description."\n'
    "pts = 10\n"
    'rating = "ternary"\n'
    'grading = "standard"\n'
)

PROMPT_ONE = "# Hello\nworld\n"
PROMPT_TWO = "# Lab\ndo it\n"


def _build_fixture(root: Path) -> None:
    rubrics = root / "data" / "rubrics"
    rubrics.mkdir(parents=True)
    (rubrics / "sample.toml").write_text(SAMPLE_TOML, encoding="utf-8")
    prompts = root / "data" / "prompt"
    prompts.mkdir()
    (prompts / "hello.md").write_text(PROMPT_ONE, encoding="utf-8")
    (prompts / "lab.md").write_text(PROMPT_TWO, encoding="utf-8")
    # one assignment config referencing prompt/hello.md (reference-count path)
    course = root / "data" / "c1"
    course.mkdir()
    (course / "config.toml").write_text(
        "[fetch]\ncourse_id = 111111\n", encoding="utf-8"
    )
    (course / "000001").mkdir()
    (course / "000001" / "config.toml").write_text(
        '[grading]\nrubric = "rubrics/sample.toml"\n'
        'system_prompt = ["prompt/hello.md", "prompt/lab.md"]\n',
        encoding="utf-8",
    )


def _modal_message(app: TataApp) -> str:
    """The ConfirmationModal message Static (second Static in .confirm-modal)."""
    return str(app.screen.query_one(".confirm-modal").query(Static)[1].content)


async def _check_shell_and_rubrics(root: Path) -> None:
    """Four shell tabs; Library tab with Rubrics + Prompts sub-panes."""
    app = TataApp(root_dir=root)
    async with app.run_test(size=(120, 44)) as pilot:
        await wait_for(pilot, lambda: app.query_one("#shell-tabs").display)
        tabs = app.query_one("#shell-tabs", TabbedContent)
        panes = tabs.query_one("ContentSwitcher").children
        assert [pane.id for pane in panes] == [
            "tab-dashboard",
            "tab-plagiarism",
            "tab-library",
            "tab-settings",
        ]
        app.switch_tab("tab-library")
        await pilot.pause()
        library = app.query_one(LibraryScreen)
        assert library.display
        sub_tabs = library.query_one("#library-tabs", TabbedContent)
        assert [pane.id for pane in sub_tabs.query(TabPane)] == [
            "tab-rubrics",
            "tab-prompts",
        ]
        # the Rubrics pane (active sub-tab) lists the library rubric
        rubrics = library.query_one(RubricsPane)
        assert rubrics.query_one("#rb-file", Select).value == "sample.toml"
        assert rubrics.query_one("#rb-criteria").row_count == 1


async def _check_prompt_edit_save(root: Path) -> None:
    """Prompts pane: TextArea loads the file; Save writes the edit back."""
    app = TataApp(root_dir=root)
    async with app.run_test(size=(120, 44)) as pilot:
        await wait_for(pilot, lambda: app.query_one("#shell-tabs").display)
        app.switch_tab("tab-library")
        await pilot.pause()
        library = app.query_one(LibraryScreen)
        sub_tabs = library.query_one("#library-tabs", TabbedContent)
        sub_tabs.active = "tab-prompts"
        await pilot.pause()
        prompts = library.query_one(PromptsPane)
        file_select = prompts.query_one("#pr-file", Select)
        assert file_select.value == "hello.md"
        editor = prompts.query_one("#pr-text", TextArea)
        assert editor.text == PROMPT_ONE

        editor.text = "# Edited\n"
        await pilot.click("#pr-save")
        await pilot.pause()
        path = root / "data" / "prompt" / "hello.md"
        assert path.read_text(encoding="utf-8") == "# Edited\n"
        # switching files and back reloads from disk
        file_select.value = "lab.md"
        await pilot.pause()
        assert editor.text == PROMPT_TWO


async def _check_prompt_create_delete(root: Path) -> None:
    """Prompt file management: create (draft + Save) and delete (cancel +
    confirm; referencing assignment configs reported in the message)."""
    app = TataApp(root_dir=root)
    async with app.run_test(size=(120, 44)) as pilot:
        await wait_for(pilot, lambda: app.query_one("#shell-tabs").display)
        app.switch_tab("tab-library")
        await pilot.pause()
        library = app.query_one(LibraryScreen)
        library.query_one("#library-tabs", TabbedContent).active = "tab-prompts"
        await pilot.pause()
        prompts = library.query_one(PromptsPane)
        file_select = prompts.query_one("#pr-file", Select)
        editor = prompts.query_one("#pr-text", TextArea)

        # -- create: New prompt… -> filename -> Save writes the draft --
        file_select.value = "__new__"
        await pilot.pause()
        assert prompts.query_one("#pr-filename", Input).display
        editor.text = "# Brand prompt\nfresh\n"
        prompts.query_one("#pr-filename", Input).value = "brand"
        await pilot.click("#pr-save")
        await pilot.pause()
        brand_path = root / "data" / "prompt" / "brand.md"
        assert brand_path.read_text(encoding="utf-8") == "# Brand prompt\nfresh\n"
        assert file_select.value == "brand.md"
        assert not prompts.query_one("#pr-filename", Input).display

        # -- delete: cancel keeps the file --
        file_select.value = "hello.md"
        await pilot.pause()
        await pilot.click("#pr-delete")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationModal))
        assert "1 assignment config(s) reference prompt/hello.md" in _modal_message(app)
        await pilot.click("#cancel")
        await wait_for(pilot, lambda: not isinstance(app.screen, ConfirmationModal))
        assert (root / "data" / "prompt" / "hello.md").is_file()
        assert file_select.value == "hello.md"

        # -- delete: confirm removes, Select falls back to the first file --
        await pilot.click("#pr-delete")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationModal))
        await pilot.click("#delete")
        await wait_for(pilot, lambda: not isinstance(app.screen, ConfirmationModal))
        assert not (root / "data" / "prompt" / "hello.md").exists()
        assert file_select.value == "brand.md"


async def _check_prompt_rename(root: Path) -> None:
    """Rename: name modal -> confirmation -> new file, old removed."""
    app = TataApp(root_dir=root)
    async with app.run_test(size=(120, 44)) as pilot:
        await wait_for(pilot, lambda: app.query_one("#shell-tabs").display)
        app.switch_tab("tab-library")
        await pilot.pause()
        library = app.query_one(LibraryScreen)
        library.query_one("#library-tabs", TabbedContent).active = "tab-prompts"
        await pilot.pause()
        prompts = library.query_one(PromptsPane)
        file_select = prompts.query_one("#pr-file", Select)
        editor = prompts.query_one("#pr-text", TextArea)

        # -- rename: name modal -> confirmation -> new file, old removed --
        file_select.value = "lab.md"
        await pilot.pause()
        await pilot.click("#pr-rename")
        await wait_for(pilot, lambda: isinstance(app.screen, FileNameModal))
        app.screen.query_one("#fnm-input", Input).value = "renamed"
        await pilot.click("#ok")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationModal))
        assert "reference prompt/lab.md" in _modal_message(app)
        await pilot.click("#rename")
        await wait_for(pilot, lambda: not isinstance(app.screen, ConfirmationModal))
        await pilot.pause()
        assert not (root / "data" / "prompt" / "lab.md").exists()
        assert (root / "data" / "prompt" / "renamed.md").read_text(
            encoding="utf-8"
        ) == PROMPT_TWO
        assert file_select.value == "renamed.md"
        assert editor.text == PROMPT_TWO
        config_text = (root / "data" / "c1" / "000001" / "config.toml").read_text(
            encoding="utf-8"
        )
        assert "prompt/renamed.md" in config_text
        assert "prompt/hello.md" in config_text
        assert "prompt/lab.md" not in config_text


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_fixture(root)
        await _check_shell_and_rubrics(root)
        await _check_prompt_edit_save(root)
        await _check_prompt_create_delete(root)
        await _check_prompt_rename(root)
    print("tata library check OK")


if __name__ == "__main__":
    asyncio.run(main())
