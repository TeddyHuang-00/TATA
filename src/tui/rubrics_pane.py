"""Rubrics pane (:class:`RubricsPane`) for the Library tab, with the shared
modals (``FileNameModal`` / ``AutoGenModal``) the three panes use to avoid an
import cycle among the pane modules.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, override

import tomlkit
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
    TextArea,
)

from src.shared.aliases import assignment_display_name
from src.shared.rubric import Grading, Rating, RubricDefinition, get_rubric_definition
from src.shared.rubric_gen import generate_rubric
from src.tui.css_loader import load_css
from src.tui.pane_shared import (
    grading_reference_configs,
    new_value,
    referencing_configs,
    rewrite_grading_refs,
    validate_name,
)
from src.tui.scan import scan_assignments, scan_courses
from src.tui.workspace import ConfirmationModal

if TYPE_CHECKING:
    from src.tui.app import AppState


log = logging.getLogger(__name__)

_RATING_VALUES = tuple(rating.value for rating in Rating)

#: Controls frozen while an auto-generation worker is in flight (write races
#: against the same rubric file — MINOR audit 2024-09).
_AUTOGEN_BUSY_SELECTORS = (
    "#rb-edit",
    "#rb-remove",
    "#rb-add",
    "#rb-update",
    "#rb-save",
    "#rb-rename",
    "#rb-delete",
    "#rb-autogen",
    "#rb-filename",
)
_GRADING_VALUES = tuple(grading.value for grading in Grading)


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


class AutoGenModal(ModalScreen[str | None]):
    """Pick an assignment description and generate a rubric from it.

    ``assignments`` is a list of (label, config_path str) pairs; Generate
    dismisses with the config path string, Cancel with None.
    """

    BINDINGS: ClassVar = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, assignments: list[tuple[str, str]]) -> None:
        super().__init__()
        self._assignments = assignments

    def action_cancel(self) -> None:
        self.dismiss(None)

    @override
    def compose(self) -> ComposeResult:
        with Vertical(classes="confirm-modal"):
            yield Static("[b]Auto-generate rubric[/b]", classes="modal-title")
            yield Static("Pick an assignment description to generate from:")
            yield Select(self._assignments, id="ag-assignment", allow_blank=False)
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="cancel")
                yield Button(
                    "Generate",
                    id="ag-generate",
                    variant="primary",
                    disabled=not self._assignments,
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.action_cancel()
        elif event.button.id == "ag-generate":
            self._submit()

    def _submit(self) -> None:
        value = self.query_one("#ag-assignment", Select).value
        if value is not None:
            self.dismiss(str(value))


class RubricsPane(Vertical):
    """Rubric editor: file picker + criteria table + one-criterion form.

    Save validates the whole :class:`RubricDefinition` and writes
    ``data/rubrics/<name>.toml`` as a ``[[criterion]]`` array of tables via
    tomlkit.
    """

    DEFAULT_CSS = load_css("library.tcss")

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self._criteria: list[dict] = []
        #: "rubrics/<file>" name of the file being saved over, or None (new file).
        self._current_file: str | None = None
        #: Index of the criterion loaded into the form (None = Add mode).
        self._editing_idx: int | None = None
        #: True while an auto-generate worker is in flight (re-entrancy guard).
        self._autogen_running = False

    def _rubrics_dir(self) -> Path:
        return self.state.assignments_dir / "rubrics"

    def _file_options(self) -> list[tuple[str, str]]:
        names = sorted(p.name for p in self._rubrics_dir().glob("*.toml"))
        return [(name, name) for name in names] + [("New rubric…", new_value)]

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
                yield Button("Auto-generate", id="rb-autogen")

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
        is_new = value == new_value
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
        name = validate_name(self.query_one("#rb-filename", Input).value, ".toml")
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
        refs = referencing_configs(self.state.assignments_dir, f"rubrics/{name}")
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
        new = validate_name(result, ".toml")
        if new is None:
            self._show_error(f"Invalid rubric name: {result!r}")
            return
        if new == current:
            self._show_error("New name is the same as the current name")
            return
        if (self._rubrics_dir() / new).exists():
            self._show_error(f"A rubric named {new} already exists")
            return
        refs = grading_reference_configs(
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
        changed, failed = rewrite_grading_refs(
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

    # ---------- auto-generate ----------

    def action_autogen(self) -> None:
        """Pick an assignment description and generate a rubric from it."""
        if self._autogen_running:
            self.app.notify("Auto-generation already in progress", severity="warning")
            return
        assignments = self._autogen_assignments()
        if not assignments:
            self.app.notify(
                "No fetched assignment descriptions. Fetch assignments first.",
                severity="warning",
            )
            return
        self.app.push_screen(AutoGenModal(assignments), self._handle_autogen)

    def _handle_autogen(self, config_path: str | None) -> None:
        if config_path is None:
            return
        self._run_autogen(Path(config_path))

    def _autogen_assignments(self) -> list[tuple[str, str]]:
        """(label, config_path) for every assignment with a fetched description."""
        options: list[tuple[str, str]] = []
        for course in scan_courses(self.state.assignments_dir):
            for assignment in scan_assignments(
                self.state.assignments_dir / course.dir_name
            ):
                config = assignment.config_path
                if not (
                    config.is_file() and (config.parent / "assignment.md").is_file()
                ):
                    continue
                name = assignment_display_name(
                    self.state.assignments_dir,
                    course.dir_name,
                    assignment.dir_name,
                    assignment.assignment_id,
                )
                label = f"{name} ({course.dir_name}/{assignment.dir_name})"
                options.append((label, str(config)))
        return options

    def _set_autogen_busy(self, busy: bool) -> None:
        """Freeze (busy=True) or restore (busy=False) every writable control
        while an auto-generation worker is in flight. Restore must be
        followed by the per-state syncs so conditionally-disabled buttons
        (rename/delete, edit/remove/update) land on their true state."""
        for selector in _AUTOGEN_BUSY_SELECTORS:
            self.query_one(selector, (Button, Input)).disabled = busy

    def _restore_autogen_buttons(self) -> None:
        self._set_autogen_busy(False)
        self._sync_file_buttons()
        self._sync_action_buttons()

    def _run_autogen(self, config_path: Path) -> None:
        """Generate (or regenerate) the rubric for an assignment description."""
        out = self._rubrics_dir() / f"{config_path.parent.name}.toml"
        if out.exists():
            self.app.push_screen(
                ConfirmationModal(
                    "Overwrite rubric",
                    f"rubrics/{out.name} already exists "
                    f"(assignment {config_path.parent.parent.name}/"
                    f"{config_path.parent.name}). Overwrite it with a new "
                    "generation?",
                    [("Overwrite", "overwrite")],
                ),
                lambda choice: self._confirm_autogen(choice, config_path, out),
            )
            return
        self._confirm_autogen("continue", config_path, out)

    def _confirm_autogen(
        self, choice: str | None, config_path: Path, out: Path
    ) -> None:
        if choice is None:
            return
        tmp = out.parent / f"{out.name}.tmp"
        with suppress(
            OSError
        ):  # stale tmp from a crashed run; generator reports real errors
            tmp.unlink()
        self._autogen_running = True
        self._set_autogen_busy(True)
        self.app.notify("Generating rubric…", severity="information")
        self.run_worker(
            lambda: self._autogen_worker(config_path, out, tmp),
            thread=True,
            group="rubric-gen",
            exclusive=True,
        )

    def _autogen_worker(self, config_path: Path, out: Path, tmp: Path) -> None:
        try:
            generate_rubric(config_path, tmp)
            # Atomic replace in the same directory: the old rubric survives a
            # failed generation instead of being unlinked up front.
            tmp.replace(out)
        except Exception as exc:
            with suppress(OSError):  # don't leave a half-written tmp behind
                tmp.unlink()
            log.error("rubric auto-generate failed for %s: %s", config_path, exc)
            message = f"Auto-generate failed: {type(exc).__name__}: {exc}"
            ok = False
        else:
            message = f"Generated rubric: {out.name}"
            ok = True
        with suppress(RuntimeError):  # app closed mid-generation
            self.app.call_from_thread(self._autogen_done, ok, message, out)

    def _autogen_done(self, ok: bool, message: str, out: Path) -> None:
        self._autogen_running = False
        self._restore_autogen_buttons()
        self.reload_files()
        if ok:
            select = self.query_one("#rb-file", Select)
            select.value = out.name
            self._on_file_change(str(select.value))
        self.app.notify(message, severity="success" if ok else "error")

    def _select_first_file(self) -> None:
        """Point the Select at the first remaining file, or New when empty."""
        first = next((v for _, v in self._file_options() if v != new_value), new_value)
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
        elif button_id == "rb-autogen":
            self.action_autogen()
