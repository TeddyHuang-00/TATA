"""Prompts pane (:class:`PromptsPane`) for the Library tab: TextArea editor
over ``data/prompt/*.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, override

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Select, Static, TextArea

from src.tui.css_loader import load_css
from src.tui.pane_shared import (
    grading_reference_configs,
    new_value,
    referencing_configs,
    rewrite_grading_refs,
    validate_name,
)
from src.tui.rubrics_pane import FileNameModal
from src.tui.workspace import ConfirmationModal

if TYPE_CHECKING:
    from src.tui.app import AppState


class PromptsPane(Vertical):
    """Prompt file editor: Select over ``data/prompt/*.md`` + TextArea + Save."""

    DEFAULT_CSS = load_css("library.tcss")

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self._current_file: str | None = None

    def _prompts_dir(self) -> Path:
        return self.state.assignments_dir / "prompt"

    def _file_options(self) -> list[tuple[str, str]]:
        names = sorted(p.name for p in self._prompts_dir().glob("*.md"))
        return [(name, name) for name in names] + [("New prompt…", new_value)]

    # ---------- composition ----------

    @override
    def compose(self) -> ComposeResult:
        yield Static("[b]Prompts[/b]", id="pr-title")
        with Horizontal(id="pr-file-row"):
            yield Select(self._file_options(), id="pr-file", allow_blank=False)
            yield Input("", placeholder="new prompt filename (.md)", id="pr-filename")
        yield Static("", id="pr-status")
        yield TextArea("", id="pr-text")
        with Horizontal(id="pr-actions"):
            yield Button("Save", id="pr-save", variant="primary")
            yield Button("Rename", id="pr-rename", disabled=True)
            yield Button("Delete", id="pr-delete", disabled=True)

    @override
    def on_mount(self) -> None:
        self._on_file_change(str(self.query_one("#pr-file", Select).value))

    # ---------- file selection ----------

    def reload_files(self) -> None:
        """Reload the file list (called when the Library tab activates)."""
        old = self._current_file
        self.query_one("#pr-file", Select).set_options(self._file_options())
        if old in {value for _, value in self.query_one("#pr-file", Select)._options}:
            self.query_one("#pr-file", Select).value = old

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "pr-file":
            self._on_file_change(str(event.value))

    def _on_file_change(self, name: str) -> None:
        if name == new_value:
            self._current_file = None
            self.query_one("#pr-filename", Input).display = "block"
            self.query_one("#pr-text", TextArea).text = ""
            self._set_status("")
            self._sync_buttons()
            return
        self.query_one("#pr-filename", Input).display = "none"
        if not name:
            self._current_file = None
            self.query_one("#pr-text", TextArea).text = ""
            self._set_status("[warning]No prompt files yet in data/prompt.[/warning]")
            self._sync_buttons()
            return
        path = self._prompts_dir() / name
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            self._set_status(f"[red]Could not load {name}: {exc}[/red]")
            self._sync_buttons()
            return
        self._current_file = name
        self.query_one("#pr-text", TextArea).text = content
        self._set_status("")
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        has_file = self._current_file is not None
        self.query_one("#pr-rename", Button).disabled = not has_file
        self.query_one("#pr-delete", Button).disabled = not has_file

    def _set_status(self, text: str) -> None:
        self.query_one("#pr-status", Static).update(text)

    # ---------- save ----------

    def action_save(self) -> None:
        text = self.query_one("#pr-text", TextArea).text
        if self._current_file is None:
            name = validate_name(self.query_one("#pr-filename", Input).value, ".md")
            if name is None:
                self._set_status(
                    "[warning]Enter a filename for the new prompt[/warning]"
                )
                return
            path = self._prompts_dir() / name
            if path.exists():
                self._set_status(f"[red]A prompt named {name} already exists[/red]")
                return
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            except OSError as exc:
                self._set_status(f"[red]Write failed: {exc}[/red]")
                return
            self._current_file = name
            self._sync_buttons()
            self.query_one("#pr-file", Select).set_options(self._file_options())
            self.query_one("#pr-file", Select).value = name
            self.query_one("#pr-filename", Input).display = "none"
            self._set_status(f"[green]Created: {name}[/green]")
            self.app.notify(f"Created prompt: {name}", severity="success")
            return
        path = self._prompts_dir() / self._current_file
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            self._set_status(f"[red]Write failed: {exc}[/red]")
            return
        self._set_status(f"[green]Saved: {self._current_file}[/green]")
        self.app.notify(f"Saved prompt: {self._current_file}", severity="success")

    # ---------- file management (delete / rename) ----------

    def action_delete(self) -> None:
        name = self._current_file
        if name is None:
            self._set_status("[warning]Select an existing prompt to delete[/warning]")
            return
        refs = referencing_configs(self.state.assignments_dir, f"prompt/{name}")
        message = f"Delete prompt {name}? This cannot be undone."
        if refs:
            message += (
                f"\n\n{len(refs)} assignment config(s) reference prompt/{name}"
                " and will be broken. Consider updating them first."
            )
        self.app.push_screen(
            ConfirmationModal("Delete prompt", message, [("Delete", "delete")]),
            lambda choice: self._finish_delete(choice, name),
        )

    def _finish_delete(self, choice: str | None, name: str) -> None:
        if choice is None:
            return
        path = self._prompts_dir() / name
        if not path.is_file():
            self._set_status(f"[red]Not found: {name}[/red]")
            return
        try:
            path.unlink()
        except OSError as exc:
            self._set_status(f"[red]Delete failed: {exc}[/red]")
            return
        self._current_file = None
        self.query_one("#pr-text", TextArea).text = ""
        self._sync_buttons()
        self._select_first_file()
        self._set_status(f"[green]Deleted: {name}[/green]")
        self.app.notify(f"Deleted prompt: {name}", severity="warning")

    def action_rename(self) -> None:
        if self._current_file is None:
            self._set_status("[warning]Select an existing prompt to rename[/warning]")
            return
        self.app.push_screen(
            FileNameModal("Rename prompt", self._current_file), self._handle_rename
        )

    def _handle_rename(self, result: str | None) -> None:
        if result is None:
            return
        current = self._current_file
        if current is None:
            return
        new = validate_name(result, ".md")
        if new is None:
            self._set_status(f"[red]Invalid prompt name: {result!r}[/red]")
            return
        if new == current:
            self._set_status("[red]New name is the same as the current name[/red]")
            return
        if (self._prompts_dir() / new).exists():
            self._set_status(f"[red]A prompt named {new} already exists[/red]")
            return
        refs = grading_reference_configs(
            self.state.assignments_dir, f"prompt/{current}"
        )
        message = f"Rename {current} to {new}?"
        if refs:
            message += (
                f"\n\n{len(refs)} assignment config(s) reference prompt/{current}."
                f" They will be updated to prompt/{new}."
            )
        self.app.push_screen(
            ConfirmationModal("Rename prompt", message, [("Rename", "rename")]),
            lambda choice: self._finish_rename(choice, current, new),
        )

    def _finish_rename(self, choice: str | None, old: str, new: str) -> None:
        if choice is None:
            return
        src = self._prompts_dir() / old
        if not src.is_file():
            self._set_status(f"[red]Not found: {old}[/red]")
            return
        try:
            (self._prompts_dir() / new).write_bytes(src.read_bytes())
            src.unlink()
        except OSError as exc:
            self._set_status(f"[red]Rename failed: {exc}[/red]")
            return
        self._current_file = new
        self._sync_buttons()
        self.query_one("#pr-file", Select).set_options(self._file_options())
        self.query_one("#pr-file", Select).value = new
        self._on_file_change(new)
        self._set_status(f"[green]Renamed: {new}[/green]")
        changed, failed = rewrite_grading_refs(
            self.state.assignments_dir, f"prompt/{old}", f"prompt/{new}"
        )
        if failed:
            self.app.notify(
                f"Renamed to {new} but could not update {len(failed)} config "
                f"reference(s): {', '.join(p.name for p in failed)}",
                severity="warning",
            )
        if changed:
            self.app.notify(
                f"Renamed prompt: {new} · updated {len(changed)} config reference(s)",
                severity="success",
            )
        else:
            self.app.notify(f"Renamed prompt: {new}", severity="success")

    def _select_first_file(self) -> None:
        """Point the Select at the first remaining file, or New when empty."""
        options = self._file_options()
        first = next((v for _, v in options if v != new_value), new_value)
        self.query_one("#pr-file", Select).value = first
        self._on_file_change(first)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "pr-save":
            self.action_save()
        elif button_id == "pr-rename":
            self.action_rename()
        elif button_id == "pr-delete":
            self.action_delete()
