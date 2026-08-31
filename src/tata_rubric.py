"""Rubric builder screen (Settings v2): edit/create rubric TOML files.

Pushed as its own Screen (mirroring :class:`src.score_review.ScoreReviewScreen`);
the Settings Grading tab opens it via ``push_screen`` and reloads its
rubric/prompt lists when the builder returns. Criteria are edited one at a
time with a form; Save validates the whole :class:`RubricDefinition` and
writes ``data/rubrics/<name>.toml`` as a ``[[criterion]]`` array of tables
via tomlkit. All UI copy is English.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, override

import tomlkit
from pydantic import ValidationError
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Input, Label, Select, Static, TextArea

from src.rubric import Grading, Rating, RubricDefinition, get_rubric_definition

if TYPE_CHECKING:
    from src.tata_app import AppState

#: Select value for the "New rubric…" file option (never a real file name).
_NEW_VALUE = "__new__"

_RATING_VALUES = tuple(rating.value for rating in Rating)
_GRADING_VALUES = tuple(grading.value for grading in Grading)


class RubricBuilderScreen(Screen[None]):
    """Standalone rubric editor: file picker + criteria table + one-criterion form.

    ``push_screen(RubricBuilderScreen(state))``; Esc pops without saving, Save
    validates and writes ``data/rubrics/<name>.toml`` then pops.
    """

    BINDINGS: ClassVar = [
        Binding("escape", "close", "Back"),
    ]

    CSS = """
#rb-title {
    height: auto;
    padding: 0 0 1 0;
}
#rb-file-row {
    height: auto;
    padding: 0 0 1 0;
}
#rb-file {
    width: 40;
}
#rb-filename {
    width: 40;
    margin: 0 0 0 2;
    display: none;
}
#rb-error {
    height: auto;
    padding: 0 0 1 0;
    color: $error;
}
#rb-criteria {
    height: 12;
    margin: 0 0 1 0;
}
#rb-form {
    height: auto;
}
#rb-form Label {
    color: $text-muted;
    text-style: bold;
}
.rb-field {
    height: auto;
    padding: 0 0 1 0;
}
#rb-desc {
    height: 5;
}
#rb-scale:disabled {
    text-style: dim;
}
#rb-actions {
    height: auto;
    padding: 0 0 1 0;
}
#rb-actions Button {
    margin: 0 1 0 0;
}
"""

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
        yield Static("[b]Rubric builder[/b]", id="rb-title")
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
                    [(v, v) for v in _RATING_VALUES], id="rb-rating", allow_blank=False
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
        yield Static("[dim]Esc closes without saving.[/dim]", id="rb-hint")

    @override
    def on_mount(self) -> None:
        self._sync_scale_enabled()
        self._on_file_change(str(self.query_one("#rb-file", Select).value))

    # ---------- file selection ----------

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "rb-file":
            self._on_file_change(str(event.value))
        elif event.select.id == "rb-grading":
            self._sync_scale_enabled()

    def _on_file_change(self, value: str) -> None:
        is_new = value == _NEW_VALUE
        self.query_one("#rb-filename", Input).display = "block" if is_new else "none"
        self._current_file = None if is_new else value
        if is_new:
            # In-memory criteria are kept (Save writes them to the new file).
            return
        self._load_file(value)

    def _load_file(self, name: str) -> None:
        path = self._rubrics_dir() / name
        try:
            definition = get_rubric_definition(path)
        except Exception as exc:  # unreadable/invalid rubric — keep the screen alive
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
        return "# schema: ../../config/rubric.schema.json\n" + tomlkit.dumps(doc)

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
        name = self.query_one("#rb-filename", Input).value.strip()
        if not name:
            self._show_error("Enter a filename for the new rubric")
            return None
        if not name.endswith(".toml"):
            name += ".toml"
        if Path(name).name != name:
            self._show_error(f"Invalid filename: {name!r}")
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
        self.app.notify(f"Saved rubric: {filename}", severity="success")
        self.dismiss(None)

    # ---------- key actions ----------

    def action_close(self) -> None:
        """Dismiss without saving (no-op when the builder is the root screen)."""
        if len(self.app.screen_stack) > 1:
            self.dismiss(None)

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
