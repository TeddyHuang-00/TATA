"""Runnable headless check for the Library tab (F2).

Mounts the full :class:`src.tui.app.TataApp` over a tmp fixture
(data/rubrics + data/prompt), switches to the Library tab and asserts:

- the shell has four tabs; the Library tab hosts Rubrics + Prompts +
  Providers sub-tab panes, and the Rubrics pane lists the library rubric;
- the Prompts pane loads ``data/prompt/*.md`` into a TextArea; editing plus
  the Save button writes the file back to disk;
- prompt file management: create (draft + Save writes the file), delete
  (ConfirmationModal cancel keeps the file, confirm removes it, referencing
  assignment configs are reported in the message) and rename (new file
  written, old removed, Select re-pointed);
- the Providers pane (over a tmp ``data/providers/`` folder via
  ``providers_dir`` injection — never the real one): add, edit, delete with
  reference count, and test connection (OpenAI client patched; captures the
  resolved base_url/api_key/model; success and failure paths).

Run: uv run tests/tata_library_check.py
"""

from __future__ import annotations

import asyncio
import math
import os
import tempfile
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import override

from e2e_common import wait_for  # isort: skip - seeds repo-root sys.path before src imports
from src.tui import library as tui_library
from src.tui.app import AppState, TataApp
from src.tui.library import (
    FileNameModal,
    LibraryScreen,
    PromptsPane,
    ProvidersPane,
    RubricsPane,
)
from src.tui.workspace import ConfirmationModal
from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.widgets import (
    Button,
    Input,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

SAMPLE_TOML = (
    "[[criterion]]\n"
    'name = "Reflection"\n'
    'desc = "A generic description."\n'
    "pts = 10\n"
    'rating = "ternary"\n'
    'grading = "standard"\n'
)

PROMPT_ONE = "# Hello\nworld\n"
PROMPT_TWO = "# Lab\ndo it\n"

PROVIDER_OLLAMA = (
    'base_url = "http://localhost:11434/v1"\n'
    'api_key = "ollama"\n'
    'model = "qwen3.8:latest"\n'
    'mode = "markdown_json_mode"\n'
)

PROVIDER_DEEPSEEK = (
    "# main cloud provider\n"
    'base_url = "https://api.deepseek.com"\n'
    'api_key = "${DEEPSEEK_API_KEY}"\n'
    'model = "deepseek-chat"\n'
    'mode = "tool_call"\n'
    "temperature = 0.3\n"
)


def _build_fixture(root: Path) -> None:
    rubrics = root / "data" / "rubrics"
    rubrics.mkdir(parents=True)
    (rubrics / "sample.toml").write_text(SAMPLE_TOML, encoding="utf-8")
    prompts = root / "data" / "prompt"
    prompts.mkdir()
    (prompts / "hello.md").write_text(PROMPT_ONE, encoding="utf-8")
    (prompts / "lab.md").write_text(PROMPT_TWO, encoding="utf-8")
    config_dir = root / "data" / "providers"
    config_dir.mkdir(parents=True)
    (config_dir / "ollama.toml").write_text(PROVIDER_OLLAMA, encoding="utf-8")
    (config_dir / "deepseek.toml").write_text(PROVIDER_DEEPSEEK, encoding="utf-8")
    # one assignment config referencing prompt/hello.md (reference-count path)
    course = root / "data" / "c1"
    course.mkdir()
    (course / "config.toml").write_text(
        "[fetch]\ncourse_id = 111111\n", encoding="utf-8"
    )
    (course / "000001").mkdir()
    (course / "000001" / "config.toml").write_text(
        '[grading]\nrubric = "rubrics/sample.toml"\n'
        'system_prompt = ["prompt/hello.md", "prompt/lab.md"]\n'
        'provider = "ollama"\n',
        encoding="utf-8",
    )


class ProviderHost(App[None]):
    """Minimal host for ProvidersPane with an injected providers dir

    (test isolation: the pane never touches the repo's data/providers/).
    """

    def __init__(self, pane: ProvidersPane) -> None:
        super().__init__()
        self._pane = pane

    @override
    def compose(self) -> ComposeResult:
        yield self._pane


def _modal_message(app: App) -> str:
    """The ConfirmationModal message Static (second Static in .confirm-modal)."""
    return str(app.screen.query_one(".confirm-modal").query(Static)[1].content)


async def _check_shell_and_rubrics(root: Path) -> None:
    """Three shell tabs; Library tab with Rubrics + Prompts + Providers sub-panes."""
    app = TataApp(root_dir=root)
    async with app.run_test(size=(120, 44)) as pilot:
        await wait_for(pilot, lambda: app.query_one("#shell-tabs").display)
        tabs = app.query_one("#shell-tabs", TabbedContent)
        panes = tabs.query_one("ContentSwitcher").children
        assert [pane.id for pane in panes] == [
            "tab-dashboard",
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
            "tab-providers",
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


async def _check_provider_add(root: Path, provider_dir: Path) -> None:
    """Add: New provider -> name + fields -> Save writes the flat file."""
    pane = ProvidersPane(AppState(root_dir=root), providers_dir=provider_dir)
    app = ProviderHost(pane)
    async with app.run_test(size=(120, 44)) as pilot:
        await wait_for(pilot, lambda: pane.query_one("#pv-name", Select).value)
        select = pane.query_one("#pv-name", Select)

        select.value = "__new__"
        await pilot.pause()
        assert pane.query_one("#pv-new-name", Input).display
        pane.query_one("#pv-new-name", Input).value = "pilot"
        pane.query_one("#pv-base-url", Input).value = "http://localhost:9999/v1"
        pane.query_one("#pv-api-key", Input).value = "${TEST_API_KEY}"
        pane.query_one("#pv-model", Input).value = "pilot-model"
        pane.query_one("#pv-mode", Select).value = "tool_call"
        pane.query_one("#pv-temperature", Input).value = "0.5"
        await pilot.click("#pv-save")
        await pilot.pause()
        text = (provider_dir / "pilot.toml").read_text(encoding="utf-8")
        assert "# schema:" not in text
        doc = tomllib.loads(text)
        assert doc == {
            "base_url": "http://localhost:9999/v1",
            "api_key": "${TEST_API_KEY}",
            "model": "pilot-model",
            "mode": "tool_call",
            "temperature": 0.5,
        }
        assert select.value == "pilot"


async def _check_provider_edit(root: Path, provider_dir: Path) -> None:
    """Edit: model + temperature update; other provider files untouched."""
    pane = ProvidersPane(AppState(root_dir=root), providers_dir=provider_dir)
    app = ProviderHost(pane)
    async with app.run_test(size=(120, 44)) as pilot:
        await wait_for(pilot, lambda: pane.query_one("#pv-name", Select).value)
        select = pane.query_one("#pv-name", Select)

        select.value = "ollama"
        await pilot.pause()
        assert (
            pane.query_one("#pv-base-url", Input).value == "http://localhost:11434/v1"
        )
        pane.query_one("#pv-model", Input).value = "qwen3.9"
        pane.query_one("#pv-temperature", Input).value = "0.5"
        await pilot.click("#pv-save")
        await pilot.pause()
        doc = tomllib.loads((provider_dir / "ollama.toml").read_text(encoding="utf-8"))
        assert doc["model"] == "qwen3.9"
        assert math.isclose(doc["temperature"], 0.5)
        deepseek = tomllib.loads(
            (provider_dir / "deepseek.toml").read_text(encoding="utf-8")
        )
        assert deepseek["model"] == "deepseek-chat"
        assert math.isclose(deepseek["temperature"], 0.3)


async def _check_provider_delete(root: Path, provider_dir: Path) -> None:
    """Delete: reference count shown; cancel keeps; confirm removes."""
    pane = ProvidersPane(AppState(root_dir=root), providers_dir=provider_dir)
    app = ProviderHost(pane)
    async with app.run_test(size=(120, 44)) as pilot:
        await wait_for(pilot, lambda: pane.query_one("#pv-name", Select).value)
        select = pane.query_one("#pv-name", Select)

        select.value = "ollama"
        await pilot.pause()
        await pilot.click("#pv-delete")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationModal))
        assert "1 assignment config(s) reference ollama" in _modal_message(app)
        await pilot.click("#cancel")
        await wait_for(pilot, lambda: not isinstance(app.screen, ConfirmationModal))
        assert (provider_dir / "ollama.toml").is_file()
        await pilot.click("#pv-delete")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationModal))
        await pilot.click("#delete")
        await wait_for(pilot, lambda: not isinstance(app.screen, ConfirmationModal))
        assert not (provider_dir / "ollama.toml").exists()
        assert (provider_dir / "deepseek.toml").is_file()


async def _check_provider_rename(root: Path, provider_dir: Path) -> None:
    """Rename: modal flow moves the file (comment + values kept); existing
    name rejected without a confirmation."""
    pane = ProvidersPane(AppState(root_dir=root), providers_dir=provider_dir)
    app = ProviderHost(pane)
    async with app.run_test(size=(120, 44)) as pilot:
        await wait_for(pilot, lambda: pane.query_one("#pv-name", Select).value)
        select = pane.query_one("#pv-name", Select)
        status = pane.query_one("#pv-status", Static)

        # -- rename deepseek -> cohere (no refs: plain confirmation) --
        select.value = "deepseek"
        await pilot.pause()
        await pilot.click("#pv-rename")
        await wait_for(pilot, lambda: isinstance(app.screen, FileNameModal))
        app.screen.query_one("#fnm-input", Input).value = "cohere"
        await pilot.click("#ok")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationModal))
        message = _modal_message(app)
        assert "Rename deepseek to cohere?" in message
        assert "assignment config(s)" not in message
        await pilot.click("#rename")
        await wait_for(pilot, lambda: not isinstance(app.screen, ConfirmationModal))
        assert not (provider_dir / "deepseek.toml").exists()
        cohere = provider_dir / "cohere.toml"
        text = cohere.read_text(encoding="utf-8")
        assert "# main cloud provider" in text  # comment moved with the file
        doc = tomllib.loads(text)
        assert doc == {
            "base_url": "https://api.deepseek.com",
            "api_key": "${DEEPSEEK_API_KEY}",
            "model": "deepseek-chat",
            "mode": "tool_call",
            "temperature": 0.3,
        }
        assert select.value == "cohere"
        assert not pane.query_one("#pv-rename", Button).disabled

        # -- rename to an existing name is rejected without a confirmation --
        await pilot.click("#pv-rename")
        await wait_for(pilot, lambda: isinstance(app.screen, FileNameModal))
        app.screen.query_one("#fnm-input", Input).value = "pilot"
        await pilot.click("#ok")
        await wait_for(pilot, lambda: not isinstance(app.screen, FileNameModal))
        await pilot.pause()
        assert "already exists" in str(status.content)
        assert not isinstance(app.screen, ConfirmationModal)
        assert (provider_dir / "pilot.toml").is_file()
        assert (provider_dir / "cohere.toml").is_file()

        # -- path separator / reserved "__new__" are rejected (no confirm) --
        await _check_rename_rejected(pilot, app, status, "a/b", "Invalid provider name")
        await _check_rename_rejected(
            pilot, app, status, "__new__", "Invalid provider name"
        )
        # -- empty name is rejected by the modal itself (OK stays useless) --
        await _check_rename_empty_rejected(pilot, app)


async def _check_rename_rejected(
    pilot: Pilot,
    app: ProviderHost,
    status: Static,
    bad: str,
    needle: str,
) -> None:
    await pilot.click("#pv-rename")
    await wait_for(pilot, lambda: isinstance(app.screen, FileNameModal))
    app.screen.query_one("#fnm-input", Input).value = bad
    await pilot.click("#ok")
    await wait_for(pilot, lambda: not isinstance(app.screen, FileNameModal))
    await pilot.pause()
    assert needle in str(status.content), (bad, str(status.content))
    assert not isinstance(app.screen, ConfirmationModal)


async def _check_rename_empty_rejected(pilot: Pilot, app: ProviderHost) -> None:
    await pilot.click("#pv-rename")
    await wait_for(pilot, lambda: isinstance(app.screen, FileNameModal))
    app.screen.query_one("#fnm-input", Input).value = ""
    await pilot.click("#ok")
    await pilot.pause()
    assert isinstance(app.screen, FileNameModal)  # not submitted
    assert not isinstance(app.screen, ConfirmationModal)
    await pilot.press("escape")
    await wait_for(pilot, lambda: not isinstance(app.screen, FileNameModal))


async def _check_provider_rename_ref(root: Path, provider_dir: Path) -> None:
    """Referenced rename: ref count in the confirmation; confirm and cancel
    paths."""
    ref = root / "data" / "c1" / "000002"
    ref.mkdir()
    (ref / "config.toml").write_text('provider_config = "cohere"\n', encoding="utf-8")
    (root / "data" / "c1" / "000001" / "config.toml").write_text(
        '[grading]\nprovider = "cohere"\n', encoding="utf-8"
    )
    pane = ProvidersPane(AppState(root_dir=root), providers_dir=provider_dir)
    app = ProviderHost(pane)
    async with app.run_test(size=(120, 44)) as pilot:
        await wait_for(pilot, lambda: pane.query_one("#pv-name", Select).value)
        select = pane.query_one("#pv-name", Select)

        select.value = "cohere"
        await pilot.pause()
        await pilot.click("#pv-rename")
        await wait_for(pilot, lambda: isinstance(app.screen, FileNameModal))
        app.screen.query_one("#fnm-input", Input).value = "sagemaker"
        await pilot.click("#ok")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationModal))
        assert "1 assignment config(s) reference cohere" in _modal_message(app)
        await pilot.click("#rename")
        await wait_for(pilot, lambda: not isinstance(app.screen, ConfirmationModal))
        assert not (provider_dir / "cohere.toml").exists()
        doc = tomllib.loads(
            (provider_dir / "sagemaker.toml").read_text(encoding="utf-8")
        )
        assert doc["model"] == "deepseek-chat"
        assert select.value == "sagemaker"

        # -- cancel keeps things as they are --
        await pilot.click("#pv-rename")
        await wait_for(pilot, lambda: isinstance(app.screen, FileNameModal))
        app.screen.query_one("#fnm-input", Input).value = "nope"
        await pilot.click("#ok")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationModal))
        await pilot.click("#cancel")
        await wait_for(pilot, lambda: not isinstance(app.screen, ConfirmationModal))
        assert not (provider_dir / "nope.toml").exists()
        assert (provider_dir / "sagemaker.toml").is_file()


async def _check_provider_test(root: Path, provider_dir: Path) -> None:
    """Test connection: patched OpenAI captures resolved args; success and
    failure paths."""
    pane = ProvidersPane(AppState(root_dir=root), providers_dir=provider_dir)
    app = ProviderHost(pane)
    async with app.run_test(size=(120, 44)) as pilot:
        await wait_for(pilot, lambda: pane.query_one("#pv-name", Select).value)
        select = pane.query_one("#pv-name", Select)
        status = pane.query_one("#pv-status", Static)
        select.value = "pilot"
        await pilot.pause()
        assert pane.query_one("#pv-api-key", Input).value == "${TEST_API_KEY}"

        captures: dict[str, str] = {}
        calls: list[dict] = []
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kwargs: calls.append(kwargs)
                )
            )
        )

        def fake_openai(base_url: str, api_key: str) -> SimpleNamespace:
            captures["base_url"] = base_url
            captures["api_key"] = api_key
            return client

        original_openai = tui_library.OpenAI
        original_key = os.environ.get("TEST_API_KEY")
        os.environ["TEST_API_KEY"] = "secret"
        tui_library.OpenAI = fake_openai
        try:
            await pilot.click("#pv-test")
            await wait_for(pilot, lambda: "Test connection OK" in str(status.content))
            assert captures == {
                "base_url": "http://localhost:9999/v1",
                "api_key": "secret",
            }
            assert calls == [
                {
                    "model": "pilot-model",
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                }
            ]
        finally:
            tui_library.OpenAI = original_openai
            if original_key is None:
                del os.environ["TEST_API_KEY"]
            else:
                os.environ["TEST_API_KEY"] = original_key

        boom = "boom"

        def failing_openai(*args: object, **kwargs: object) -> None:
            raise RuntimeError(boom)

        # the first press's "active" animation blocks a fresh click until it ends
        await wait_for(
            pilot, lambda: not pane.query_one("#pv-test", Button).has_class("-active")
        )
        tui_library.OpenAI = failing_openai
        try:
            await pilot.click("#pv-test")
            await wait_for(
                pilot,
                lambda: (
                    "Test connection failed: RuntimeError: boom" in str(status.content)
                ),
            )
        finally:
            tui_library.OpenAI = original_openai


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_fixture(root)
        await _check_shell_and_rubrics(root)
        await _check_prompt_edit_save(root)
        await _check_prompt_create_delete(root)
        await _check_prompt_rename(root)
        provider_dir = root / "data" / "providers"
        await _check_provider_add(root, provider_dir)
        await _check_provider_edit(root, provider_dir)
        await _check_provider_delete(root, provider_dir)
        await _check_provider_rename(root, provider_dir)
        await _check_provider_rename_ref(root, provider_dir)
        await _check_provider_test(root, provider_dir)
    print("tata library check OK")


if __name__ == "__main__":
    asyncio.run(main())
