"""TATA Library tab: rubric + prompt + provider editing (F2).

Hosted by :mod:`src.tui.app` inside the Library TabPane (T4-d). Three inner
TabPanes: *Rubrics* (:class:`RubricsPane`, migrated from the former
``RubricBuilderScreen`` — no Screen, no push_screen), *Prompts*
(:class:`PromptsPane`, TextArea over ``data/prompt/*.md``) and *Providers*
(:class:`ProvidersPane`, one ``data/providers/<name>.toml`` per provider).
Non-Screen
widgets get their styling via ``DEFAULT_CSS`` (class-level ``CSS`` does not
apply to them — lesson c9272e81). All UI copy is English.
"""

from __future__ import annotations

import os
import re
from collections.abc import MutableMapping
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, override

import tomlkit
from instructor import Mode
from openai import OpenAI
from pydantic import ValidationError
from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Label,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from src import REPO_ROOT
from src.shared.provider import ProviderInfo
from src.shared.rubric import Grading, Rating, RubricDefinition, get_rubric_definition
from src.tui.workspace import ConfirmationModal

if TYPE_CHECKING:
    from src.tui.app import AppState

#: Select value for the "New rubric…" file option (never a real file name).
_NEW_VALUE = "__new__"

_RATING_VALUES = tuple(rating.value for rating in Rating)
_GRADING_VALUES = tuple(grading.value for grading in Grading)
_MODE_VALUES = tuple(mode.value for mode in Mode)


# ---------- shared library helpers ----------


def _validate_name(raw: str, suffix: str) -> str | None:
    """Strip, append the suffix, reject path separators; None if invalid."""
    name = raw.strip()
    if not name:
        return None
    if not name.endswith(suffix):
        name += suffix
    if Path(name).name != name:
        return None
    return name


def _referencing_configs(data_dir: Path, needle: str) -> list[Path]:
    """Read-only scan: config.toml files under ``data/`` whose text mentions
    ``needle`` (e.g. ``rubrics/sample.toml``). Course + assignment level only
    (``data/*/config.toml``, ``data/*/*/config.toml``)."""
    hits: list[Path] = []
    for pattern in ("*/config.toml", "*/*/config.toml"):
        for path in data_dir.glob(pattern):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if needle in text:
                hits.append(path)
    return hits


def _grading_reference_configs(data_dir: Path, old: str) -> list[Path]:
    """Parse-based scan: config.toml files whose ``[grading]`` section
    references ``old`` (rubric or a system_prompt entry). Same path patterns
    as :func:`_referencing_configs`; unreadable/unparseable files are skipped.
    """
    hits: list[Path] = []
    for pattern in ("*/config.toml", "*/*/config.toml"):
        for path in data_dir.glob(pattern):
            try:
                doc = tomlkit.parse(path.read_text(encoding="utf-8"))
            except (OSError, tomlkit.exceptions.ParseError):
                continue
            grading = doc.get("grading")
            if not isinstance(grading, MutableMapping):
                continue
            if grading.get("rubric") == old:
                hits.append(path)
                continue
            prompts = grading.get("system_prompt")
            if (isinstance(prompts, list) and old in prompts) or (
                isinstance(prompts, str) and prompts == old
            ):
                hits.append(path)
    return hits


def _rewrite_grading_refs(
    data_dir: Path, old: str, new: str
) -> tuple[list[Path], list[Path]]:
    """Rewrite ``old`` -> ``new`` in every referencing config's ``[grading]``
    (rubric, and every system_prompt list member). Returns (changed, failed).
    """
    changed: list[Path] = []
    failed: list[Path] = []
    for path in _grading_reference_configs(data_dir, old):
        try:
            doc = tomlkit.parse(path.read_text(encoding="utf-8"))
        except (OSError, tomlkit.exceptions.ParseError):
            failed.append(path)
            continue
        grading = doc.get("grading")
        if not isinstance(grading, MutableMapping):
            failed.append(path)
            continue
        if grading.get("rubric") == old:
            grading["rubric"] = new
        prompts = grading.get("system_prompt")
        if isinstance(prompts, list):
            for i, item in enumerate(prompts):
                if item == old:
                    prompts[i] = new
        elif isinstance(prompts, str) and prompts == old:
            grading["system_prompt"] = new
        out = tomlkit.dumps(doc)
        if not out.endswith("\n"):
            out += "\n"
        try:
            path.write_text(out, encoding="utf-8")
        except OSError:
            failed.append(path)
        else:
            changed.append(path)
    return changed, failed


def _provider_reference_configs(data_dir: Path, name: str) -> list[Path]:
    """Parse-based scan: config.toml files whose ``[grading].provider``
    references ``name``. Same path patterns as :func:`_grading_reference_configs`;
    unreadable/unparseable files are skipped."""
    hits: list[Path] = []
    for pattern in ("*/config.toml", "*/*/config.toml"):
        for path in data_dir.glob(pattern):
            try:
                doc = tomlkit.parse(path.read_text(encoding="utf-8"))
            except (OSError, tomlkit.exceptions.ParseError):
                continue
            grading = doc.get("grading")
            if isinstance(grading, MutableMapping) and grading.get("provider") == name:
                hits.append(path)
    return hits


def _resolve_env_placeholders(api_key: str) -> str:
    """Resolve ``${VAR}`` placeholders against os.getenv (same regex as
    :meth:`src.shared.provider.ProviderList.__getitem__`); unresolvable -> \"\"."""
    return re.sub(r"\$\{(\w+?)\}", lambda m: os.getenv(m.group(1), ""), api_key)


def _ping_provider(base_url: str, api_key: str, model: str) -> None:
    """Real connectivity probe: one tiny chat completion. Runs on a worker
    thread; raises on any failure."""
    client = OpenAI(base_url=base_url, api_key=api_key)
    client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "ping"}],
        max_tokens=1,
    )


class FileNameModal(ModalScreen[str | None]):
    """One-input modal: returns the typed name (Enter / OK), None on cancel."""

    BINDINGS: ClassVar = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, title: str, current: str) -> None:
        super().__init__()
        self._title = title
        self._current = current

    def action_cancel(self) -> None:
        self.dismiss(None)

    @override
    def compose(self) -> ComposeResult:
        with Vertical(classes="confirm-modal"):
            yield Static(f"[b]{escape(self._title)}[/b]", classes="modal-title")
            yield Input(value=self._current, id="fnm-input")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("OK", id="ok", variant="primary")

    def _submit(self) -> None:
        name = self.query_one("#fnm-input", Input).value.strip()
        if name:
            self.dismiss(name)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.action_cancel()
        elif event.button.id == "ok":
            self._submit()

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self._submit()


class RubricsPane(Vertical):
    """Rubric editor: file picker + criteria table + one-criterion form.

    Save validates the whole :class:`RubricDefinition` and writes
    ``data/rubrics/<name>.toml`` as a ``[[criterion]]`` array of tables via
    tomlkit.
    """

    DEFAULT_CSS = (Path(__file__).parent / "styles" / "library.tcss").read_text()

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self._criteria: list[dict] = []
        #: "rubrics/<file>" name of the file being saved over, or None (new file).
        self._current_file: str | None = None
        #: Index of the criterion loaded into the form (None = Add mode).
        self._editing_idx: int | None = None

    def _rubrics_dir(self) -> Path:
        return self.state.assignments_dir / "rubrics"

    def _file_options(self) -> list[tuple[str, str]]:
        names = sorted(p.name for p in self._rubrics_dir().glob("*.toml"))
        return [(name, name) for name in names] + [("New rubric…", _NEW_VALUE)]

    # ---------- composition ----------

    @override
    def compose(self) -> ComposeResult:
        with ScrollableContainer():
            yield Static("[b]Rubrics[/b]", id="rb-title")
            with Horizontal(id="rb-file-row"):
                yield Select(self._file_options(), id="rb-file", allow_blank=False)
                yield Input("", placeholder="new rubric filename", id="rb-filename")
            yield Static("", id="rb-error")
            yield DataTable(id="rb-criteria")
            with Vertical(id="rb-form"):
                yield Label("Criterion")
                with Vertical(classes="rb-field"):
                    yield Label("name")
                    yield Input(id="rb-name")
                with Vertical(classes="rb-field"):
                    yield Label("desc")
                    yield TextArea("", id="rb-desc")
                with Vertical(classes="rb-field"):
                    yield Label("rating")
                    yield Select(
                        [(v, v) for v in _RATING_VALUES],
                        id="rb-rating",
                        allow_blank=False,
                    )
                with Vertical(classes="rb-field"):
                    yield Label("grading")
                    yield Select(
                        [(v, v) for v in _GRADING_VALUES],
                        id="rb-grading",
                        allow_blank=False,
                    )
                with Vertical(classes="rb-field"):
                    yield Label("pts")
                    yield Input(id="rb-pts")
                with Vertical(classes="rb-field"):
                    yield Label("custom_scale (comma-separated; grading=custom only)")
                    yield Input("", placeholder="0.0, 0.5, 1.0", id="rb-scale")
            with Horizontal(id="rb-actions"):
                yield Button("Edit", id="rb-edit")
                yield Button("Remove", id="rb-remove", disabled=True)
                yield Button("Add", id="rb-add", variant="primary")
                yield Button("Update", id="rb-update", disabled=True)
                yield Button("Save rubric", id="rb-save")
                yield Button("Rename", id="rb-rename", disabled=True)
                yield Button("Delete", id="rb-delete", disabled=True)

    @override
    def on_mount(self) -> None:
        self._sync_scale_enabled()
        self._on_file_change(str(self.query_one("#rb-file", Select).value))

    # ---------- file selection ----------

    def reload_files(self) -> None:
        """Reload the file list (called when the Library tab activates)."""
        self.query_one("#rb-file", Select).set_options(self._file_options())

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "rb-file":
            self._on_file_change(str(event.value))
        elif event.select.id == "rb-grading":
            self._sync_scale_enabled()

    def _on_file_change(self, value: str) -> None:
        is_new = value == _NEW_VALUE
        self.query_one("#rb-filename", Input).display = "block" if is_new else "none"
        self._current_file = None if is_new else value
        self._sync_file_buttons()
        if is_new:
            # In-memory criteria are kept (Save writes them to the new file).
            return
        self._load_file(value)

    def _sync_file_buttons(self) -> None:
        has_file = self._current_file is not None
        self.query_one("#rb-rename", Button).disabled = not has_file
        self.query_one("#rb-delete", Button).disabled = not has_file

    def _load_file(self, name: str) -> None:
        path = self._rubrics_dir() / name
        try:
            definition = get_rubric_definition(path)
        except Exception as exc:  # unreadable/invalid rubric — keep the pane alive
            self._criteria = []
            self._show_error(f"Could not load {name}: {exc}")
        else:
            self._show_error("")
            self._criteria = [
                {
                    key: value
                    for key, value in criterion.model_dump().items()
                    if value is not None
                }
                for criterion in definition.criterion
            ]
        self._render_table()
        self._clear_form()

    # ---------- criteria table ----------

    @staticmethod
    def _fmt_pts(value: float | int) -> str:
        return f"{value:g}"

    def _render_table(self) -> None:
        table = self.query_one("#rb-criteria", DataTable)
        if not table.columns:
            table.add_column("name")
            table.add_column("rating")
            table.add_column("grading")
            table.add_column("pts")
        table.clear()
        for criterion in self._criteria:
            table.add_row(
                criterion["name"],
                criterion["rating"],
                criterion.get("grading", ""),
                self._fmt_pts(criterion["pts"]),
                key=str(len(table.rows)),
            )
        if table.row_count:
            table.move_cursor(row=0)
        self._sync_action_buttons()

    def _selected_index(self) -> int | None:
        table = self.query_one("#rb-criteria", DataTable)
        if table.row_count == 0:
            return None
        row = table.cursor_row
        if row is None or row < 0 or row >= table.row_count:
            return None
        return row

    def _sync_action_buttons(self) -> None:
        has_selection = self._selected_index() is not None
        self.query_one("#rb-edit", Button).disabled = not has_selection
        self.query_one("#rb-remove", Button).disabled = not has_selection
        self.query_one("#rb-update", Button).disabled = self._editing_idx is None

    def _show_error(self, text: str) -> None:
        self.query_one("#rb-error", Static).update(text)

    # ---------- form ----------

    def _form_criterion(self) -> dict | None:
        name = self.query_one("#rb-name", Input).value.strip()
        desc = self.query_one("#rb-desc", TextArea).text.strip()
        rating = str(self.query_one("#rb-rating", Select).value)
        grading = str(self.query_one("#rb-grading", Select).value)
        pts_text = self.query_one("#rb-pts", Input).value.strip()
        if not name:
            self._show_error("Criterion name cannot be empty")
            return None
        if not desc:
            self._show_error("Criterion description cannot be empty")
            return None
        try:
            pts = float(pts_text)
        except ValueError:
            self._show_error(f"Invalid pts value: {pts_text!r}")
            return None
        criterion: dict = {
            "name": name,
            "desc": desc,
            "rating": rating,
            "grading": grading,
            "pts": int(pts) if pts.is_integer() else pts,
        }
        if grading == Grading.CUSTOM.value:
            scale_text = self.query_one("#rb-scale", Input).value.strip()
            if not scale_text:
                self._show_error("custom_scale is required when grading is custom")
                return None
            try:
                scale = [float(p.strip()) for p in scale_text.split(",") if p.strip()]
            except ValueError:
                self._show_error("custom_scale must be comma-separated numbers")
                return None
            criterion["custom_scale"] = scale
        return criterion

    def _clear_form(self) -> None:
        self._editing_idx = None
        self.query_one("#rb-name", Input).value = ""
        self.query_one("#rb-desc", TextArea).text = ""
        self.query_one("#rb-pts", Input).value = ""
        self.query_one("#rb-scale", Input).value = ""
        self.query_one("#rb-rating", Select).value = Rating.BINARY.value
        self.query_one("#rb-grading", Select).value = Grading.STANDARD.value
        self._sync_scale_enabled()
        self._sync_action_buttons()

    def _sync_scale_enabled(self) -> None:
        grading = str(self.query_one("#rb-grading", Select).value)
        self.query_one("#rb-scale", Input).disabled = grading != Grading.CUSTOM.value

    def action_edit(self) -> None:
        index = self._selected_index()
        if index is None:
            self._show_error("Select a criterion row first")
            return
        criterion = self._criteria[index]
        self._editing_idx = index
        self.query_one("#rb-name", Input).value = criterion["name"]
        self.query_one("#rb-desc", TextArea).text = criterion["desc"]
        self.query_one("#rb-rating", Select).value = criterion["rating"]
        self.query_one("#rb-grading", Select).value = criterion.get(
            "grading", Grading.STANDARD.value
        )
        self.query_one("#rb-pts", Input).value = self._fmt_pts(criterion["pts"])
        self.query_one("#rb-scale", Input).value = ", ".join(
            self._fmt_pts(value) for value in criterion.get("custom_scale", [])
        )
        self._sync_scale_enabled()
        self._sync_action_buttons()

    def action_remove(self) -> None:
        index = self._selected_index()
        if index is None:
            self._show_error("Select a criterion row first")
            return
        del self._criteria[index]
        if self._editing_idx == index:
            self._clear_form()
        self._render_table()

    def action_add(self) -> None:
        criterion = self._form_criterion()
        if criterion is None:
            return
        self._criteria.append(criterion)
        self._show_error("")
        self._render_table()
        self._clear_form()

    def action_update(self) -> None:
        if self._editing_idx is None:
            self._show_error("Nothing to update — select a row and press Edit first")
            return
        criterion = self._form_criterion()
        if criterion is None:
            return
        self._criteria[self._editing_idx] = criterion
        self._show_error("")
        self._render_table()
        self._clear_form()

    # ---------- save ----------

    def _dump(self) -> str:
        doc = tomlkit.document()
        rows = tomlkit.aot()
        for criterion in self._criteria:
            rows.append(
                tomlkit.item({
                    key: value for key, value in criterion.items() if value is not None
                })
            )
        doc["criterion"] = rows
        return tomlkit.dumps(doc)

    @staticmethod
    def _fmt_validation_errors(exc: ValidationError) -> list[str]:
        return [
            f"{'.'.join(str(part) for part in error.get('loc', []))}: "
            f"{error.get('msg')}"
            for error in exc.errors()
        ]

    def _target_file(self) -> str | None:
        if self._current_file is not None:
            return self._current_file
        name = _validate_name(self.query_one("#rb-filename", Input).value, ".toml")
        if name is None:
            self._show_error("Enter a valid filename for the new rubric")
            return None
        return name

    def action_save(self) -> None:
        if not self._criteria:
            self._show_error("Add at least one criterion before saving")
            return
        try:
            RubricDefinition.model_validate({"criterion": self._criteria})
        except ValidationError as exc:
            message = "; ".join(self._fmt_validation_errors(exc))
            self._show_error(message)
            self.app.notify(message, severity="error")
            return
        filename = self._target_file()
        if filename is None:
            return
        path = self._rubrics_dir() / filename
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self._dump(), encoding="utf-8")
        except OSError as exc:
            self._show_error(f"Write failed: {exc}")
            return
        self._current_file = filename
        self._sync_file_buttons()
        # Re-enumerate the file list so the new rubric is selectable; the
        # write succeeded, so reload the same file to mirror the Select.
        self.query_one("#rb-file", Select).set_options(self._file_options())
        self.query_one("#rb-file", Select).value = filename
        self.app.notify(f"Saved rubric: {filename}", severity="success")

    # ---------- file management (delete / rename) ----------

    def action_delete(self) -> None:
        name = self._current_file
        if name is None:
            self._show_error("Select an existing rubric to delete")
            return
        refs = _referencing_configs(self.state.assignments_dir, f"rubrics/{name}")
        message = f"Delete rubric {name}? This cannot be undone."
        if refs:
            message += (
                f"\n\n{len(refs)} assignment config(s) reference rubrics/{name}"
                " and will be broken. Consider updating them first."
            )
        self.app.push_screen(
            ConfirmationModal("Delete rubric", message, [("Delete", "delete")]),
            lambda choice: self._finish_delete(choice, name),
        )

    def _finish_delete(self, choice: str | None, name: str) -> None:
        if choice is None:
            return
        path = self._rubrics_dir() / name
        if not path.is_file():
            self._show_error(f"Not found: {name}")
            return
        try:
            path.unlink()
        except OSError as exc:
            self._show_error(f"Delete failed: {exc}")
            return
        self._current_file = None
        self._criteria = []
        self._render_table()
        self._clear_form()
        self._sync_file_buttons()
        self._select_first_file()
        self.app.notify(f"Deleted rubric: {name}", severity="warning")

    def action_rename(self) -> None:
        if self._current_file is None:
            self._show_error("Select an existing rubric to rename")
            return
        self.app.push_screen(
            FileNameModal("Rename rubric", self._current_file), self._handle_rename
        )

    def _handle_rename(self, result: str | None) -> None:
        if result is None:
            return
        current = self._current_file
        if current is None:
            return
        new = _validate_name(result, ".toml")
        if new is None:
            self._show_error(f"Invalid rubric name: {result!r}")
            return
        if new == current:
            self._show_error("New name is the same as the current name")
            return
        if (self._rubrics_dir() / new).exists():
            self._show_error(f"A rubric named {new} already exists")
            return
        refs = _grading_reference_configs(
            self.state.assignments_dir, f"rubrics/{current}"
        )
        message = f"Rename {current} to {new}?"
        if refs:
            message += (
                f"\n\n{len(refs)} assignment config(s) reference rubrics/{current}."
                f" They will be updated to rubrics/{new}."
            )
        self.app.push_screen(
            ConfirmationModal("Rename rubric", message, [("Rename", "rename")]),
            lambda choice: self._finish_rename(choice, current, new),
        )

    def _finish_rename(self, choice: str | None, old: str, new: str) -> None:
        if choice is None:
            return
        src = self._rubrics_dir() / old
        if not src.is_file():
            self._show_error(f"Not found: {old}")
            return
        try:
            (self._rubrics_dir() / new).write_bytes(src.read_bytes())
            src.unlink()
        except OSError as exc:
            self._show_error(f"Rename failed: {exc}")
            return
        self._current_file = new
        self._sync_file_buttons()
        self.query_one("#rb-file", Select).set_options(self._file_options())
        self.query_one("#rb-file", Select).value = new
        self._on_file_change(new)
        changed, failed = _rewrite_grading_refs(
            self.state.assignments_dir, f"rubrics/{old}", f"rubrics/{new}"
        )
        if failed:
            self.app.notify(
                f"Renamed to {new} but could not update {len(failed)} config "
                f"reference(s): {', '.join(p.name for p in failed)}",
                severity="warning",
            )
        if changed:
            self.app.notify(
                f"Renamed rubric: {new} · updated {len(changed)} config reference(s)",
                severity="success",
            )
        else:
            self.app.notify(f"Renamed rubric: {new}", severity="success")

    def _select_first_file(self) -> None:
        """Point the Select at the first remaining file, or New when empty."""
        first = next(
            (v for _, v in self._file_options() if v != _NEW_VALUE), _NEW_VALUE
        )
        self.query_one("#rb-file", Select).value = first
        self._on_file_change(first)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "rb-edit":
            self.action_edit()
        elif button_id == "rb-remove":
            self.action_remove()
        elif button_id == "rb-add":
            self.action_add()
        elif button_id == "rb-update":
            self.action_update()
        elif button_id == "rb-save":
            self.action_save()
        elif button_id == "rb-rename":
            self.action_rename()
        elif button_id == "rb-delete":
            self.action_delete()


class PromptsPane(Vertical):
    """Prompt file editor: Select over ``data/prompt/*.md`` + TextArea + Save."""

    DEFAULT_CSS = (Path(__file__).parent / "styles" / "library.tcss").read_text()

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self._current_file: str | None = None

    def _prompts_dir(self) -> Path:
        return self.state.assignments_dir / "prompt"

    def _file_options(self) -> list[tuple[str, str]]:
        names = sorted(p.name for p in self._prompts_dir().glob("*.md"))
        return [(name, name) for name in names] + [("New prompt…", _NEW_VALUE)]

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
        if name == _NEW_VALUE:
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
            name = _validate_name(self.query_one("#pr-filename", Input).value, ".md")
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
        refs = _referencing_configs(self.state.assignments_dir, f"prompt/{name}")
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
        new = _validate_name(result, ".md")
        if new is None:
            self._set_status(f"[red]Invalid prompt name: {result!r}[/red]")
            return
        if new == current:
            self._set_status("[red]New name is the same as the current name[/red]")
            return
        if (self._prompts_dir() / new).exists():
            self._set_status(f"[red]A prompt named {new} already exists[/red]")
            return
        refs = _grading_reference_configs(
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
        changed, failed = _rewrite_grading_refs(
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
        first = next((v for _, v in options if v != _NEW_VALUE), _NEW_VALUE)
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


class ProvidersPane(Vertical):
    """Provider registry editor: Select + form + Save/Delete/Test connection.

    One provider per file in ``data/providers/<name>.toml`` with flat
    top-level keys (base_url, api_key, model, mode, temperature), edited
    with tomlkit (comment-preserving). ``providers_dir`` is injectable for
    isolated tests; the default is the repo's ``data/providers``.
    """

    DEFAULT_CSS = (Path(__file__).parent / "styles" / "library.tcss").read_text()

    def __init__(self, state: AppState, providers_dir: Path | None = None) -> None:
        super().__init__()
        self.state = state
        self._providers_dir = providers_dir or (REPO_ROOT / "data" / "providers")
        #: Name of the provider loaded into the form (None = new provider).
        self._current: str | None = None

    def _provider_file(self, name: str) -> Path:
        return self._providers_dir / f"{name}.toml"

    def _doc(self, name: str) -> tomlkit.TOMLDocument:
        """Parse one provider file; an empty document when missing/unreadable."""
        try:
            return tomlkit.parse(self._provider_file(name).read_text(encoding="utf-8"))
        except (OSError, tomlkit.exceptions.ParseError):
            return tomlkit.document()

    def _names(self) -> list[str]:
        return sorted(p.stem for p in self._providers_dir.glob("*.toml"))

    def _options(self) -> list[tuple[str, str]]:
        return [(name, name) for name in self._names()] + [
            ("New provider…", _NEW_VALUE)
        ]

    # ---------- composition ----------

    @override
    def compose(self) -> ComposeResult:
        with ScrollableContainer():
            yield Static("[b]Providers[/b]", id="pv-title")
            with Horizontal(id="pv-file-row"):
                yield Select(self._options(), id="pv-name", allow_blank=False)
                yield Input("", placeholder="new provider name", id="pv-new-name")
            yield Static("", id="pv-status")
            with Vertical(id="pv-form"):
                with Vertical(classes="rb-field"):
                    yield Label("base_url")
                    yield Input(
                        id="pv-base-url", placeholder="https://api.example.com/v1"
                    )
                with Vertical(classes="rb-field"):
                    yield Label("api_key")
                    yield Input(
                        id="pv-api-key", placeholder="literal value or ${ENV_VAR}"
                    )
                with Vertical(classes="rb-field"):
                    yield Label("model")
                    yield Input(id="pv-model", placeholder="model id")
                with Vertical(classes="rb-field"):
                    yield Label("mode")
                    yield Select(
                        [(value, value) for value in _MODE_VALUES],
                        id="pv-mode",
                        allow_blank=False,
                    )
                with Vertical(classes="rb-field"):
                    yield Label("temperature (optional, 0.0-2.0)")
                    yield Input(
                        id="pv-temperature", placeholder="blank = provider default"
                    )
            with Horizontal(id="pv-actions"):
                yield Button("Save", id="pv-save", variant="primary")
                yield Button("Rename", id="pv-rename", disabled=True)
                yield Button("Delete", id="pv-delete", disabled=True)
                yield Button("Test connection", id="pv-test")

    @override
    def on_mount(self) -> None:
        self._on_name_change(str(self.query_one("#pv-name", Select).value))

    # ---------- selection ----------

    def reload_files(self) -> None:
        """Reload the provider list (called when the Library tab activates)."""
        self.query_one("#pv-name", Select).set_options(self._options())

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "pv-name":
            self._on_name_change(str(event.value))

    def _on_name_change(self, value: str) -> None:
        is_new = value == _NEW_VALUE
        self.query_one("#pv-new-name", Input).display = "block" if is_new else "none"
        self._current = None if is_new else value
        if is_new:
            self._clear_form()
            return
        self._load_provider(value)

    def _load_provider(self, name: str) -> None:
        doc = self._doc(name)
        if not doc:
            self._set_status(f"[red]Could not load provider {escape(name)}[/red]")
            self._clear_form()
            return
        mode = str(doc.get("mode", _MODE_VALUES[0]))
        if mode not in _MODE_VALUES:
            mode = _MODE_VALUES[0]
        temperature = doc.get("temperature")
        self.query_one("#pv-base-url", Input).value = str(doc.get("base_url", ""))
        self.query_one("#pv-api-key", Input).value = str(doc.get("api_key", ""))
        self.query_one("#pv-model", Input).value = str(doc.get("model", ""))
        self.query_one("#pv-mode", Select).value = mode
        self.query_one("#pv-temperature", Input).value = (
            "" if temperature is None else str(temperature)
        )
        self._set_status("")
        self._sync_buttons()

    def _clear_form(self) -> None:
        self.query_one("#pv-base-url", Input).value = ""
        self.query_one("#pv-api-key", Input).value = ""
        self.query_one("#pv-model", Input).value = ""
        self.query_one("#pv-mode", Select).value = _MODE_VALUES[0]
        self.query_one("#pv-temperature", Input).value = ""
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        disabled = self._current is None
        self.query_one("#pv-rename", Button).disabled = disabled
        self.query_one("#pv-delete", Button).disabled = disabled

    def _set_status(self, text: str) -> None:
        self.query_one("#pv-status", Static).update(text)

    # ---------- form ----------

    def _form_values(self) -> dict | None:
        base_url = self.query_one("#pv-base-url", Input).value.strip()
        api_key = self.query_one("#pv-api-key", Input).value.strip()
        model = self.query_one("#pv-model", Input).value.strip()
        mode = str(self.query_one("#pv-mode", Select).value)
        temperature = self.query_one("#pv-temperature", Input).value.strip()
        if not base_url:
            self._set_status("[red]base_url cannot be empty[/red]")
            return None
        if not api_key:
            self._set_status("[red]api_key cannot be empty[/red]")
            return None
        if not model:
            self._set_status("[red]model cannot be empty[/red]")
            return None
        values: dict = {
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "mode": mode,
        }
        if temperature:
            try:
                values["temperature"] = float(temperature)
            except ValueError:
                self._set_status(
                    f"[red]temperature must be a number: {escape(temperature)}[/red]"
                )
                return None
        return values

    def _target_name(self) -> str | None:
        if self._current is not None:
            return self._current
        raw = self.query_one("#pv-new-name", Input).value.strip()
        if not raw:
            self._set_status("[red]Enter a name for the new provider[/red]")
            return None
        if raw == _NEW_VALUE or Path(raw).name != raw:
            self._set_status(f"[red]Invalid provider name: {escape(raw)}[/red]")
            return None
        if raw in self._names():
            self._set_status(
                f"[red]A provider named {escape(raw)} already exists[/red]"
            )
            return None
        return raw

    def _show_validation_errors(self, exc: ValidationError) -> None:
        message = "; ".join(
            f"{'.'.join(str(part) for part in error.get('loc', []))}: {error.get('msg')}"
            for error in exc.errors()
        )
        self._set_status(f"[red]{escape(message)}[/red]")
        self.app.notify(message, severity="error")

    def _write(self, name: str, doc: tomlkit.TOMLDocument) -> bool:
        out = tomlkit.dumps(doc)
        if not out.endswith("\n"):
            out += "\n"
        try:
            path = self._provider_file(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(out, encoding="utf-8")
        except OSError as exc:
            self._set_status(f"[red]Write failed: {exc}[/red]")
            return False
        return True

    # ---------- save / delete ----------

    def action_save(self) -> None:
        name = self._target_name()
        if name is None:
            return
        values = self._form_values()
        if values is None:
            return
        try:
            ProviderInfo.model_validate(values)
        except ValidationError as exc:
            self._show_validation_errors(exc)
            return
        doc = self._doc(name)
        for key, value in values.items():
            doc[key] = value
        if "temperature" not in values:
            with suppress(KeyError):
                del doc["temperature"]
        if not self._write(name, doc):
            return
        self._current = name
        self.query_one("#pv-name", Select).set_options(self._options())
        self.query_one("#pv-name", Select).value = name
        self.query_one("#pv-new-name", Input).display = "none"
        self._sync_buttons()
        self._set_status(f"[green]Saved provider: {escape(name)}[/green]")
        self.app.notify(f"Saved provider: {name}", severity="success")

    def action_delete(self) -> None:
        name = self._current
        if name is None:
            self._set_status("[warning]Select an existing provider to delete[/warning]")
            return
        refs = _provider_reference_configs(self.state.assignments_dir, name)
        message = f"Delete provider {name}? This cannot be undone."
        if refs:
            message += (
                f"\n\n{len(refs)} assignment config(s) reference {name}"
                " and will be broken. Consider updating them first."
            )
        self.app.push_screen(
            ConfirmationModal("Delete provider", message, [("Delete", "delete")]),
            lambda choice: self._finish_delete(choice, name),
        )

    def _finish_delete(self, choice: str | None, name: str) -> None:
        if choice is None:
            return
        path = self._provider_file(name)
        if not path.exists():
            self._set_status(f"[red]Not found: {escape(name)}[/red]")
            return
        try:
            path.unlink()
        except OSError as exc:
            self._set_status(f"[red]Remove failed: {exc}[/red]")
            return
        self._current = None
        self.query_one("#pv-name", Select).set_options(self._options())
        self._select_first()
        self._set_status(f"[green]Deleted provider: {escape(name)}[/green]")
        self.app.notify(f"Deleted provider: {name}", severity="warning")

    def action_rename(self) -> None:
        if self._current is None:
            self._set_status("[warning]Select an existing provider to rename[/warning]")
            return
        self.app.push_screen(
            FileNameModal("Rename provider", self._current), self._handle_rename
        )

    def _handle_rename(self, result: str | None) -> None:
        if result is None:
            return
        current = self._current
        if current is None:
            return
        raw = result.strip()
        if not raw:
            self._set_status("[red]Enter a provider name[/red]")
            return
        if raw == _NEW_VALUE or Path(raw).name != raw:
            self._set_status(f"[red]Invalid provider name: {escape(raw)}[/red]")
            return
        if raw == current:
            self._set_status("[red]New name is the same as the current name[/red]")
            return
        if raw in self._names():
            self._set_status(
                f"[red]A provider named {escape(raw)} already exists[/red]"
            )
            return
        refs = _provider_reference_configs(self.state.assignments_dir, current)
        message = f"Rename {current} to {raw}?"
        if refs:
            message += (
                f"\n\n{len(refs)} assignment config(s) reference {current}"
                " and will be broken. Consider updating them first."
            )
        self.app.push_screen(
            ConfirmationModal("Rename provider", message, [("Rename", "rename")]),
            lambda choice: self._finish_rename(choice, current, raw, len(refs)),
        )

    def _finish_rename(
        self, choice: str | None, old: str, new: str, n_refs: int
    ) -> None:
        if choice is None:
            return
        old_path = self._provider_file(old)
        if not old_path.exists():
            self._set_status(f"[red]Not found: {escape(old)}[/red]")
            return
        try:
            old_path.rename(self._provider_file(new))
        except OSError as exc:
            self._set_status(f"[red]Rename failed: {exc}[/red]")
            return
        self._current = new
        self.query_one("#pv-name", Select).set_options(self._options())
        self.query_one("#pv-name", Select).value = new
        self._sync_buttons()
        if n_refs:
            self.app.notify(
                f"Renamed provider: {new} · update {n_refs} config reference(s)",
                severity="warning",
            )
        else:
            self.app.notify(f"Renamed provider: {new}", severity="success")
        self._set_status(
            f"[green]Renamed provider: {escape(old)} to {escape(new)}[/green]"
        )

    def _select_first(self) -> None:
        first = next(
            (value for _, value in self._options() if value != _NEW_VALUE), _NEW_VALUE
        )
        self.query_one("#pv-name", Select).value = first

    # ---------- test connection ----------

    def action_test_connection(self) -> None:
        values = self._form_values()
        if values is None:
            return
        base_url = values["base_url"]
        api_key = _resolve_env_placeholders(values["api_key"])
        model = values["model"]
        self._set_status("[dim]Testing connection…[/dim]")

        def probe() -> None:
            try:
                _ping_provider(base_url, api_key, model)
            except Exception as exc:
                ok, message = (
                    False,
                    f"Test connection failed: {type(exc).__name__}: {exc}",
                )
            else:
                ok, message = True, f"Test connection OK: {model}"
            with suppress(RuntimeError):  # app closed mid-probe
                self.app.call_from_thread(self._test_done, ok, message)

        self.run_worker(probe, thread=True, group="library-test")

    def _test_done(self, ok: bool, message: str) -> None:
        color = "green" if ok else "red"
        self._set_status(f"[{color}]{escape(message)}[/{color}]")
        self.app.notify(message, severity="success" if ok else "error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "pv-save":
            self.action_save()
        elif button_id == "pv-rename":
            self.action_rename()
        elif button_id == "pv-delete":
            self.action_delete()
        elif button_id == "pv-test":
            self.action_test_connection()


class LibraryScreen(Vertical):
    """Library tab container: Rubrics + Prompts + Providers sub-tab panes."""

    DEFAULT_CSS = (Path(__file__).parent / "styles" / "library.tcss").read_text()

    def __init__(self, state: AppState) -> None:
        super().__init__(id="library-screen")
        self.state = state

    @override
    def compose(self) -> ComposeResult:
        with TabbedContent(id="library-tabs"):
            with TabPane("Rubrics", id="tab-rubrics"):
                yield RubricsPane(self.state)
            with TabPane("Prompts", id="tab-prompts"):
                yield PromptsPane(self.state)
            with TabPane("Providers", id="tab-providers"):
                yield ProvidersPane(self.state)

    def reload_files(self) -> None:
        """Refresh all pane file lists (tab activation, external edits)."""
        with suppress(Exception):
            self.query_one(RubricsPane).reload_files()
        with suppress(Exception):
            self.query_one(PromptsPane).reload_files()
        with suppress(Exception):
            self.query_one(ProvidersPane).reload_files()

    def _focus_default(self) -> None:
        """Seat focus on the visible sub-tab's file Select."""
        tabs = self.query_one("#library-tabs", TabbedContent)
        if tabs.active == "tab-rubrics":
            target = "#rb-file"
        elif tabs.active == "tab-prompts":
            target = "#pr-file"
        else:
            target = "#pv-name"
        with suppress(Exception):
            self.query_one(target, Select).focus()
