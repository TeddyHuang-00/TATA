"""TATA Settings screen (S5, T6b): three-layer config editing.

The screen edits ``config.toml`` at three layers — global
(``data/config.toml``), course (``data/<course>/config.toml``)
and assignment (``data/<course>/<name>/config.toml``) — selected by a
context ``Select``. The read path reuses :mod:`src.shared.assignment_config`
(layered merge via :func:`load_assignment_file`); writes merge **only the
edited keys** into the target file and validate the result with the same
pydantic models before persisting (design 05 §4). All UI copy is English.

Hosted by :mod:`src.tui.app` (T6c) inside the Settings TabPane; this module
deliberately does not import or modify that file (the ``AppState`` type is
imported under TYPE_CHECKING only, breaking the circular import).

v1 scope limits (design 05): the provider registry is read-only (edit
``data/providers/*.toml`` with e=$EDITOR), ``.env`` is display-only, and
full hook-model editing are not implemented.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, override

from canvasapi import Canvas
from dotenv import dotenv_values, set_key
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from src.shared.aliases import (
    assignment_display_name,
    course_display_name,
)
from src.shared.assignment_config import (
    FetchSection,
    PlagiarismSection,
    load_assignment_file,
)
from src.shared.canvas_fetch import list_courses, load_env
from src.shared.config_edit import edit_config, read_config, validate_config_edits
from src.shared.provider import ProviderInfo, get_providers

if TYPE_CHECKING:
    from src.tui.app import AppState

# (fqid, kind) for every text field; ``section.key`` is both the TOML path and
# the widget id suffix. ``prompt`` is a str-or-list-of-str field (system_prompt);
# ``list`` is a comma-separated extensions-style field.
_FIELD_SPECS: tuple[tuple[str, str], ...] = (
    ("grading.system_prompt", "prompt"),
    ("grading.max_parallel_tasks", "int"),
    ("fetch.course_id", "int"),
    ("plagiarism.copydetect_weight", "float"),
    ("plagiarism.embedding_weight", "float"),
    ("plagiarism.display_threshold", "float"),
    ("plagiarism.pairwise_alpha", "float"),
    ("plagiarism.individual_alpha", "float"),
    ("plagiarism.score_floor", "float"),
    ("plagiarism.score_cap", "float"),
    ("plagiarism.embedding_model", "str"),
    ("plagiarism.extensions", "list"),
    ("plagiarism.template_file", "str"),
    ("assignment.raw_dir", "str"),
    ("assignment.processed_dir", "str"),
    ("assignment.graded_dir", "str"),
    ("assignment.logs_dir", "str"),
    ("assignment.reference_file", "str"),
)

# (fqid) Select fields: provider (from data/providers/) and rubric (from
# data/rubrics), both dynamic lists.
_SELECT_SPECS: tuple[str, ...] = ("grading.provider", "grading.rubric")

# (fqid, label) Checkbox fields (design 05 §④ — the six common switches).
_CHECKBOX_SPECS: tuple[tuple[str, str], ...] = (
    ("processing.remove_base64_images", "remove_base64_images"),
    ("processing.clean_filenames", "clean_filenames"),
    ("processing.strip_canvas_suffix", "strip_canvas_suffix"),
    ("processing.strip_html_callouts", "strip_html_callouts"),
    ("processing.strip_html_escaped_backslashes", "strip_html_escaped_backslashes"),
    ("processing.strip_html_div_tags", "strip_html_div_tags"),
)

# Context -> writable TOML sections (design 05 §2.5).
_EDITABLE: dict[str, frozenset[str]] = {
    "global": frozenset({"plagiarism"}),
    "course": frozenset({"fetch", "plagiarism"}),
    "assignment": frozenset({"grading", "plagiarism", "assignment", "processing"}),
}


def _field_widget_id(fqid: str) -> str:
    return f"f-{fqid.replace('.', '-')}"


def _read_registry() -> dict[str, ProviderInfo]:
    """Provider registry from ``data/providers/*.toml`` ({} on any failure)."""
    try:
        return get_providers().providers
    except Exception:  # display-only; the screen must not crash
        return {}


def mask_secret(value: str) -> str:
    """``abcd********efgh`` preview; never the full value.

    Fixed rule: first 4 chars + 8 stars + last 4 chars; a value of 8 chars
    or fewer is 8 stars; the empty string stays empty.
    """
    if not value:
        return ""
    if len(value) <= 2 * _SECRET_SHOW_LEN:
        return "*" * _SECRET_STARS
    return f"{value[:_SECRET_SHOW_LEN]}{'*' * _SECRET_STARS}{value[-_SECRET_SHOW_LEN:]}"


_SECRET_SHOW_LEN = 4  # fixed head/tail shown around the mask
_SECRET_STARS = 8  # fixed star count covering the middle


class _SecretInput(Input):
    """Token field: plaintext while focused, masked preview otherwise.

    ``value`` always holds the plaintext (save writes it to .env); the mask
    is display-only via ``_value``, which drives Input's ``render_line``.
    """

    @property
    def _value(self) -> Text:
        if self.has_focus:
            return super()._value
        return Text(
            mask_secret(str(self.value)), no_wrap=True, overflow="ignore", end=""
        )


class _PromptCheckList(Vertical):
    """Checkbox list for ``grading.system_prompt`` (one row per prompt file).

    Each row is a Checkbox plus up/down buttons; row order IS the value
    order (the ``system_prompt`` list order is semantic). Exposes
    ``value: list[str]`` ("prompt/<file>"), ``set_value`` and the standard
    ``disabled`` attribute so the settings plumbing treats it like any
    other field widget. Rows are rebuilt by :meth:`_refresh_files` whenever
    the set of ``data/prompt/*.md`` files changes and by :meth:`set_value`
    when the config order differs from the current row order.
    """

    def __init__(
        self, assignments_dir: Path, label: str, reset: Button | None = None
    ) -> None:
        super().__init__(classes="settings-field")
        self._assignments_dir = assignments_dir
        self._base_label = label
        self._files: list[str] = []
        self._checked: dict[str, bool] = {}
        self._reset = reset

    @override
    def compose(self) -> ComposeResult:
        with Horizontal(classes="field-heading"):
            yield Label(self._base_label, id="label-grading-system-prompt")
            if self._reset is not None:
                yield self._reset
        yield Vertical(id="prompt-list")

    @override
    def on_mount(self) -> None:
        self._refresh_files()

    def watch_disabled(self, disabled: bool) -> None:
        for widget in self.walk_children():
            widget.disabled = disabled

    def _rebuild_rows(self) -> None:
        """Rebuild all rows in ``self._files`` order.

        ``value`` derives from the ``_files``/``_checked`` mirror, so the
        rows are pure display. Textual defers remove/mount, and a query in
        the same frame would still see the old boxes, so the mirror never
        depends on DOM state.
        """
        container = self.query_one("#prompt-list", Vertical)
        container.remove_children()
        for index, name in enumerate(self._files):
            checkbox = Checkbox(
                name, value=self._checked.get(name, False), id=f"cb-prompt-{index}"
            )
            up = Button("▲", id=f"up-prompt-{index}", classes="prompt-arrow")
            down = Button("▼", id=f"down-prompt-{index}", classes="prompt-arrow")
            container.mount(Horizontal(checkbox, up, down, classes="prompt-row"))
        if not self._files:
            container.mount(
                Static("No prompt files found in data/prompt.", id="prompt-empty")
            )
        for widget in container.walk_children():
            widget.disabled = self.disabled

    def _refresh_files(self) -> None:
        """(Re)build the rows from disk; preserve checked state and row order.

        No-op when the file set is unchanged, so existing checked states and
        the user's ordering survive ``_load_context`` re-runs.
        """
        files = sorted(p.name for p in (self._assignments_dir / "prompt").glob("*.md"))
        container = self.query_one("#prompt-list", Vertical)
        if files == self._files and container.children:
            return
        self._checked = {name: self._checked.get(name, False) for name in files}
        self._files = files
        self._rebuild_rows()

    def _move_row(self, index: int, delta: int) -> None:
        """Swap row ``index`` with its up/down neighbour (button action).

        The buttons move any row, checked or not; the value order is the
        row order, so reordering before/after checking both work.
        """
        target = index + delta
        if not (0 <= target < len(self._files)):
            return
        self._files[index], self._files[target] = (
            self._files[target],
            self._files[index],
        )
        self._rebuild_rows()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("up-prompt-"):
            self._move_row(int(button_id.split("-")[-1]), -1)
        elif button_id.startswith("down-prompt-"):
            self._move_row(int(button_id.split("-")[-1]), 1)

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        """Keep the ``_checked`` mirror in step with user toggles."""
        if event.checkbox.id and str(event.checkbox.id).startswith("cb-prompt-"):
            self._checked[str(event.checkbox.label)] = bool(event.checkbox.value)

    @property
    def value(self) -> list[str]:
        """Checked prompt paths in row order, e.g. ``["prompt/system.md"]``."""
        return [f"prompt/{name}" for name in self._files if self._checked.get(name)]

    def set_value(self, value: object) -> None:
        """Check the boxes matching ``value`` (str or list of paths).

        Rows are also reordered to match a list value's order, so saving an
        unrelated key cannot silently reorder ``system_prompt``.
        """
        if isinstance(value, str):
            items = [value]
        elif isinstance(value, (list, tuple)):
            items = [str(item) for item in value]
        else:
            items = []
        selected = set(items)
        wanted = [
            item.removeprefix("prompt/")
            for item in items
            if item.startswith("prompt/")
            and item.removeprefix("prompt/") in self._files
        ]
        order = wanted + [name for name in sorted(self._files) if name not in wanted]
        self._checked = {name: f"prompt/{name}" in selected for name in self._files}
        container = self.query_one("#prompt-list", Vertical)
        if order != self._files or not container.children:
            self._files = order
            self._rebuild_rows()
            return
        for checkbox in container.query(Checkbox):
            checkbox.value = f"prompt/{checkbox.label!s}" in selected


class _LField(Vertical):
    """Label + Input/Select/Checkbox pair (small compose helper)."""

    def __init__(
        self, label: str, widget: Input | Select | Checkbox, reset: Button | None = None
    ) -> None:
        super().__init__(classes="settings-field")
        self._label = label
        self._widget = widget
        self._reset = reset

    @override
    def compose(self) -> ComposeResult:
        with Horizontal(classes="field-heading"):
            yield Label(self._label)
            if self._reset is not None:
                yield self._reset
        yield self._widget


def _context_label(dir_name: str, display_name: str, ident: int | None) -> str:
    """``Alias (id)`` when aliased; ``dir_name`` otherwise (no double ID)."""
    if display_name != dir_name:
        id_str = dir_name if ident is None else str(ident)
        return f"{display_name} ({id_str})"
    return dir_name


class SettingsScreen(Vertical):
    """S5 settings: context selector + four tab panes + layering-aware save.

    Exposes :meth:`set_context` / :meth:`action_save` / :meth:`action_reset`
    so host wiring (T6c) and headless checks can drive it without key events.
    """

    can_focus = True  # holds the key bindings when no inner widget is focused

    BINDINGS: ClassVar = [
        Binding("ctrl+s", "save", "Save"),
        Binding("r", "reset", "Reset"),
        Binding("e", "edit_config", "Edit config"),
        Binding("t", "test_canvas", "Test Canvas"),
        Binding("1", "tab_grading", "Grading"),
        Binding("2", "tab_canvas", "Canvas"),
        Binding("3", "tab_plagiarism", "Plagiarism"),
        Binding("4", "tab_paths", "Paths"),
    ]

    DEFAULT_CSS = (Path(__file__).parent / "styles" / "settings.tcss").read_text()

    def __init__(self, state: AppState) -> None:
        super().__init__(id="settings-screen")
        self.state = state
        self._ctx: str = ""  # unset until on_mount sets the initial context
        self._widgets: dict[str, Input | Select | Checkbox | _PromptCheckList] = {}
        self._reset_buttons: dict[str, Button] = {}
        self._reset_fqids: dict[str, str] = {}
        self._loaded: dict[str, str] = {}
        self._base_labels: dict[str, str] = {}
        self._result = ""
        self._registry: dict[str, ProviderInfo] = {}
        self._global_exists = False
        self._specs: dict[str, tuple[str, str, str]] = {}
        spec_list = list(_FIELD_SPECS)
        spec_list.extend((fqid, "select") for fqid in _SELECT_SPECS)
        spec_list.extend((fqid, "bool") for fqid, _label in _CHECKBOX_SPECS)
        for fqid, kind in spec_list:
            section, key = fqid.split(".", 1)
            self._specs[fqid] = (section, key, kind)

    # ---------- composition ----------

    @override
    def compose(self) -> ComposeResult:
        with Horizontal(id="settings-top"):
            yield Static("Settings · Context:", id="settings-title")
            yield Select([("Global", "global")], id="ctx-select", allow_blank=False)
        yield Static("", id="ctx-hint")
        with TabbedContent(id="settings-tabs"):
            with TabPane("Grading", id="tab-grading"), ScrollableContainer():
                yield _LField(
                    "provider (from data/providers/)",
                    self._select("grading.provider"),
                    reset=self._reset_button("grading.provider"),
                )
                yield _LField(
                    "rubric (from data/rubrics)",
                    self._select("grading.rubric"),
                    reset=self._reset_button("grading.rubric"),
                )
                yield self._prompt_checklist("grading.system_prompt")
                yield _LField(
                    "max_parallel_tasks (1..10)",
                    self._input("grading.max_parallel_tasks"),
                    reset=self._reset_button("grading.max_parallel_tasks"),
                )
            with TabPane("Canvas", id="tab-canvas"), ScrollableContainer():
                yield Static("", id="canvas-env")
                yield _LField(
                    "Canvas URL",
                    Input(
                        id="canvas-url",
                        placeholder="https://canvas.instructure.com",
                    ),
                )
                yield _LField(
                    "Canvas token (visible only while editing)",
                    _SecretInput(id="canvas-token"),
                )
                with Horizontal():
                    yield Button("Save .env", id="btn-save-env", variant="primary")
                    yield Button("Reload .env", id="btn-reload-env")
                yield Static("", id="canvas-fetch-list")
                yield _LField(
                    "course_id (Canvas course, numeric)",
                    self._input("fetch.course_id"),
                    reset=self._reset_button("fetch.course_id"),
                )
                yield Button("Test Canvas connection", id="btn-test-canvas")
            with TabPane("Plagiarism", id="tab-plagiarism"), ScrollableContainer():
                yield _LField(
                    "copydetect_weight (0..1)",
                    self._input("plagiarism.copydetect_weight"),
                    reset=self._reset_button("plagiarism.copydetect_weight"),
                )
                yield _LField(
                    "embedding_weight (0..1)",
                    self._input("plagiarism.embedding_weight"),
                    reset=self._reset_button("plagiarism.embedding_weight"),
                )
                yield _LField(
                    "display_threshold (0..1)",
                    self._input("plagiarism.display_threshold"),
                    reset=self._reset_button("plagiarism.display_threshold"),
                )
                yield _LField(
                    "pairwise_alpha (>0, <=1)",
                    self._input("plagiarism.pairwise_alpha"),
                    reset=self._reset_button("plagiarism.pairwise_alpha"),
                )
                yield _LField(
                    "individual_alpha (>0, <=1)",
                    self._input("plagiarism.individual_alpha"),
                    reset=self._reset_button("plagiarism.individual_alpha"),
                )
                yield _LField(
                    "score_floor (0..0.5)",
                    self._input("plagiarism.score_floor"),
                    reset=self._reset_button("plagiarism.score_floor"),
                )
                yield _LField(
                    "score_cap (0.5..1)",
                    self._input("plagiarism.score_cap"),
                    reset=self._reset_button("plagiarism.score_cap"),
                )
                yield _LField(
                    "embedding_model",
                    self._input("plagiarism.embedding_model"),
                    reset=self._reset_button("plagiarism.embedding_model"),
                )
                yield _LField(
                    "extensions (comma-separated, e.g. .py, .ipynb)",
                    self._input("plagiarism.extensions"),
                    reset=self._reset_button("plagiarism.extensions"),
                )
            with TabPane("Paths / Advanced", id="tab-paths"), ScrollableContainer():
                yield _LField(
                    "raw_dir",
                    self._input("assignment.raw_dir"),
                    reset=self._reset_button("assignment.raw_dir"),
                )
                yield _LField(
                    "processed_dir",
                    self._input("assignment.processed_dir"),
                    reset=self._reset_button("assignment.processed_dir"),
                )
                yield _LField(
                    "graded_dir",
                    self._input("assignment.graded_dir"),
                    reset=self._reset_button("assignment.graded_dir"),
                )
                yield _LField(
                    "logs_dir",
                    self._input("assignment.logs_dir"),
                    reset=self._reset_button("assignment.logs_dir"),
                )
                yield _LField(
                    "reference_file (optional)",
                    self._input("assignment.reference_file"),
                    reset=self._reset_button("assignment.reference_file"),
                )
                yield _LField(
                    "template_file ([plagiarism])",
                    self._input("plagiarism.template_file"),
                    reset=self._reset_button("plagiarism.template_file"),
                )
                for fqid, label in _CHECKBOX_SPECS:
                    yield _LField(
                        label,
                        self._checkbox(fqid),
                        reset=self._reset_button(fqid),
                    )
        with Horizontal(id="settings-actions"):
            yield Button("Save (ctrl+s)", id="btn-save", variant="primary")
            yield Button("Reset (r)", id="btn-reset")
        yield Static("", id="settings-status")

    def _reset_button(self, fqid: str) -> Button:
        """Small per-field reset button (deletes the key at this layer)."""
        button = Button("↺", id=f"reset-{fqid.replace('.', '-')}")
        self._reset_buttons[fqid] = button
        self._reset_fqids[str(button.id)] = fqid
        return button

    def _input(self, fqid: str) -> Input:
        widget = Input(id=_field_widget_id(fqid))
        self._widgets[fqid] = widget
        return widget

    def _select(self, fqid: str) -> Select:
        widget = Select([("", "")], id=_field_widget_id(fqid), allow_blank=False)
        self._widgets[fqid] = widget
        return widget

    def _checkbox(self, fqid: str) -> Checkbox:
        widget = Checkbox("", id=_field_widget_id(fqid))
        self._widgets[fqid] = widget
        return widget

    def _prompt_checklist(self, fqid: str) -> _PromptCheckList:
        widget = _PromptCheckList(
            self.state.assignments_dir,
            "system_prompt (multi-select)",
            reset=self._reset_button(fqid),
        )
        widget.id = _field_widget_id(fqid)
        self._widgets[fqid] = widget
        return widget

    @override
    def on_mount(self) -> None:
        self._registry = _read_registry()
        self._load_env_fields()
        self.set_context(self._initial_ctx())
        self.query_one("#ctx-select", Select).focus()

    # ---------- context API ----------

    @property
    def current_context(self) -> str:
        return self._ctx

    def available_contexts(self) -> list[str]:
        options = ["global"]
        if self.state.current_course is not None:
            options.append("course")
        if self.state.current_assignment is not None:
            options.append("assignment")
        return options

    def context_options(self) -> list[tuple[str, str]]:
        """(value, label) pairs for ``#ctx-select``.

        Labels show the alias + id when an alias.toml entry exists (e.g.
        ``Data Structures (271218)``); plain ``dir_name`` otherwise. Values
        stay ``global``/``course``/``assignment`` — display only.
        """
        options: list[tuple[str, str]] = [("global", "Global")]
        course = self.state.current_course
        if course is not None:
            name = course_display_name(
                self.state.assignments_dir, course.dir_name, course.course_id
            )
            options.append((
                "course",
                _context_label(course.dir_name, name, course.course_id),
            ))
        assignment = self.state.current_assignment
        if assignment is not None:
            name = assignment_display_name(
                self.state.assignments_dir,
                course.dir_name if course is not None else "",
                assignment.dir_name,
                assignment.assignment_id,
            )
            options.append((
                "assignment",
                _context_label(assignment.dir_name, name, assignment.assignment_id),
            ))
        return options

    def set_context(self, ctx: str) -> None:
        if ctx == self._ctx or ctx not in self.available_contexts():
            return
        self._ctx = ctx
        self._load_context()

    def _initial_ctx(self) -> str:
        if self.state.current_assignment is not None:
            return "assignment"
        if self.state.current_course is not None:
            return "course"
        return "global"

    # ---------- loading ----------

    def _global_path(self) -> Path:
        return self.state.root_dir / "data" / "config.toml"

    @staticmethod
    def _read_raw(path: Path | None) -> dict:
        """Tolerant raw read: {} when missing/unparseable (see config_edit)."""
        if path is None:
            return {}
        return read_config(path)

    def _assignment_view(self) -> dict | None:
        """Layered merged view of the current assignment (None when broken)."""
        assignment = self.state.current_assignment
        if assignment is None:
            return None
        try:
            return load_assignment_file(assignment.config_path).model_dump()
        except Exception:  # fields show empty, save still validates
            return None

    def _layer_view(self) -> dict:
        """Values to display for the current context.

        Assignment context shows the fully layered view (existing merge code);
        course/global contexts show the layer's own raw values over the schema
        defaults (that is what the target file holds, and what gets written).
        """
        if self._ctx == "assignment":
            return self._assignment_view() or {}
        dirty: dict[str, dict] = {
            "fetch": FetchSection().model_dump(),
            "plagiarism": PlagiarismSection().model_dump(),
        }
        path: Path | None = None
        if self._ctx == "course":
            course = self.state.current_course
            path = course.config_path if course is not None else None
        if self._ctx == "global":
            path = self._global_path()
        raw = self._read_raw(path)
        view: dict[str, dict] = {}
        for section, defaults in dirty.items():
            merged = dict(defaults)
            source = raw.get(section) if isinstance(raw, dict) else None
            if isinstance(source, dict):
                merged.update(source)
            view[section] = merged
        return view

    def _load_context(self) -> None:
        view = self._layer_view()
        self._global_exists = self._global_path().is_file()
        contexts = self.available_contexts()
        if self._ctx not in contexts:
            self._ctx = "global"

        ctx_select = self.query_one("#ctx-select", Select)
        options = [(label, value) for value, label in self.context_options()]
        current_values = {
            value for _, value in ctx_select._options if value is not Select.NULL
        }
        if current_values != {value for _, value in options}:
            ctx_select.set_options(options)
        if ctx_select.value != self._ctx:
            ctx_select.value = self._ctx

        hint = ""
        if self.state.current_course is None:
            hint = (
                "No course selected — only the Global context is editable. "
                "Enter a course from the Dashboard (Global view) to unlock the "
                "Course/Assignment contexts."
            )
        elif self._ctx == "global" and not self._global_exists:
            hint = (
                "No global config file (data/config.toml) — press e to "
                "create it, or save here once the file exists."
            )
        self.query_one("#ctx-hint", Static).update(hint)

        self._loaded = {}
        for fqid, (section, key, kind) in self._specs.items():
            widget = self._widgets[fqid]
            value = (
                (view.get(section) or {}).get(key) if isinstance(view, dict) else None
            )
            if isinstance(widget, Select):
                self._set_select_options(
                    widget, fqid, str(value) if value is not None else ""
                )
            if isinstance(widget, _PromptCheckList):
                widget._refresh_files()
            self._set_widget_value(widget, kind, value)
            self._loaded[fqid] = self._value_str(widget)
            writable = self._writable(section)
            widget.disabled = not writable
            if fqid in self._reset_buttons:
                self._reset_buttons[fqid].disabled = not writable
        self._apply_badges()
        self._render_statics()
        self._update_status()

    # ---------- inherited badges ----------

    def _local_keys(self) -> frozenset[str]:
        """fqids present (non-None) in the raw LOCAL assignment config.

        Read via :func:`read_config` on the assignment's own config.toml —
        NOT the layered merged view: a global/course value for a key the
        assignment never sets is what the "(inherited)" badge means.
        """
        assignment = self.state.current_assignment
        if assignment is None:
            return frozenset()
        raw = read_config(assignment.config_path)
        local: set[str] = set()
        for fqid, (section, key, _kind) in self._specs.items():
            section_data = raw.get(section)
            if isinstance(section_data, dict) and section_data.get(key) is not None:
                local.add(fqid)
        return frozenset(local)

    def _field_label(self, fqid: str) -> Label:
        widget = self._widgets[fqid]
        owner = widget if isinstance(widget, _PromptCheckList) else widget.parent
        return owner.query_one(Label)

    def _inherit_source(self, section: str, key: str) -> str | None:
        """Layer that actually provides a field's value: course, global, None.

        None means the value is a pydantic schema default, not an inherited
        layer value. Only called for keys absent from the local assignment
        config.
        """
        course = self.state.current_course
        if course is not None:
            raw = self._read_raw(course.config_path)
            if self._raw_key(raw, section, key):
                return "course"
        global_raw = self._read_raw(self._global_path())
        if self._raw_key(global_raw, section, key):
            return "global"
        return None

    @staticmethod
    def _raw_key(raw: object, section: str, key: str) -> bool:
        data = raw.get(section) if isinstance(raw, dict) else None
        return isinstance(data, dict) and data.get(key) is not None

    def _apply_badges(self) -> None:
        """Append an inheritance badge to the label in the assignment context.

        Assignment context shows the layered effective values; a key that is
        not set in the local config gets a badge naming the source layer and
        the effective value (or the schema default). Other contexts show
        plain labels.
        """
        local = self._local_keys() if self._ctx == "assignment" else None
        for fqid, (section, key, _kind) in self._specs.items():
            label = self._field_label(fqid)
            base = self._base_labels.get(fqid)
            if base is None:
                base = str(label.content)
                self._base_labels[fqid] = base
            if local is not None and fqid not in local:
                display = self._value_str(self._widgets[fqid])
                source = self._inherit_source(section, key)
                if source is not None:
                    suffix = f" (inherited from {source}: {display})"
                elif display:
                    suffix = f" (default: {display})"
                else:
                    suffix = " (not set)"
                label.update(f"{base} [dim]{suffix}[/dim]")
            else:
                label.update(base)

    def _set_select_options(self, widget: Select, fqid: str, current: str) -> None:
        if fqid == "grading.provider":
            names = sorted(self._registry)
            options = [(name, name) for name in names]
            empty_label = "(no available provider)"
        elif fqid == "grading.rubric":
            names = sorted(
                p.name for p in (self.state.assignments_dir / "rubrics").glob("*.toml")
            )
            options = [(f"rubrics/{name}", f"rubrics/{name}") for name in names]
            empty_label = "No rubrics found — create one in the Library tab"
        else:
            return
        if current and current not in {value for _, value in options}:
            options.append((f"{current} (not in list)", current))
        if not options:
            options = [(empty_label, "")]
        widget.set_options(options)
        values = {value for _, value in widget._options if value is not Select.NULL}
        if current in values:
            widget.value = current

    @staticmethod
    def _set_widget_value(
        widget: Input | Select | Checkbox | _PromptCheckList, kind: str, value: object
    ) -> None:
        if isinstance(widget, _PromptCheckList):
            widget.set_value(value)
        elif isinstance(widget, Checkbox):
            widget.value = bool(value)
        elif not isinstance(widget, Select):
            widget.value = SettingsScreen._format_value(kind, value)

    def _writable(self, section: str) -> bool:
        global_blocked = (
            section == "plagiarism"
            and self._ctx == "global"
            and not self._global_exists
        )
        return section in _EDITABLE[self._ctx] and not global_blocked

    # ---------- rendering ----------

    @staticmethod
    def _format_value(kind: str, value: object) -> str:
        if value is None:
            return ""
        if kind == "float":
            return f"{float(value):g}"
        if kind == "int":
            return str(int(value))
        if kind == "list":
            return ", ".join(str(item) for item in value)
        if kind == "prompt":
            return ", ".join(value) if isinstance(value, list) else str(value)
        return str(value)

    @staticmethod
    def _value_str(widget: Input | Select | Checkbox | _PromptCheckList) -> str:
        if isinstance(widget, _PromptCheckList):
            return ",".join(widget.value)
        if isinstance(widget, Checkbox):
            return "true" if widget.value else "false"
        if isinstance(widget, Select):
            return "" if widget.value is Select.NULL else str(widget.value)
        return str(widget.value or "")

    def _render_statics(self) -> None:
        env = self.state.env_state or {}
        if env.get("has_env"):
            env_text = (
                f"[green]Canvas .env: found[/green] — {env.get('base_url')}, "
                f"token: {mask_secret(env.get('token') or '')}. [dim]Edit URL and "
                "token in the fields above, then Save .env.[/dim]"
            )
        else:
            env_text = (
                "[yellow]Canvas .env: not found[/yellow] — set CANVAS_BASE_URL / "
                "CANVAS_ACCESS_TOKEN in the fields above and Save .env "
                "(gitignored), then press t to test."
            )
        self.query_one("#canvas-env", Static).update(env_text)

        course = self.state.current_course
        if course is None:
            self.query_one("#canvas-fetch-list", Static).update(
                "[dim][[fetch.assignments]]: select a course to view the "
                "assignment list.[/dim]"
            )
        else:
            raw = self._read_raw(course.config_path)
            assigns = (raw.get("fetch") or {}).get("assignments")
            if isinstance(assigns, list) and assigns:
                entries = [
                    f"  {entry.get('id') or entry.get('assignment_id')}"
                    for entry in assigns
                    if isinstance(entry, dict)
                ]
                text = (
                    "[dim][[fetch.assignments]] (read-only; managed from the "
                    "Course view):[/dim]\n" + "\n".join(entries)
                )
            else:
                text = "[dim][[fetch.assignments]]: none yet.[/dim]"
            self.query_one("#canvas-fetch-list", Static).update(text)

    # ---------- .env (Canvas tab) ----------

    def _read_env_state(self) -> dict:
        """Read ``<root_dir>/.env``; shape mirrors :func:`src.tui.app._env_status`.

        Deliberately not reused from app.py — that module imports this one
        (settings), so the read lives here (dotenv_values is tolerant).
        """
        env_path = self.state.root_dir / ".env"
        if env_path.is_file():
            try:
                vals = dotenv_values(env_path, interpolate=False)
            except UnicodeDecodeError:
                vals = {}
            if "CANVAS_BASE_URL" in vals and "CANVAS_ACCESS_TOKEN" in vals:
                return {
                    "has_env": True,
                    "base_url": vals["CANVAS_BASE_URL"],
                    "token": vals["CANVAS_ACCESS_TOKEN"],
                    "token_set": True,
                }
        return {"has_env": False, "base_url": None, "token": None, "token_set": False}

    def _load_env_fields(self) -> None:
        """Refresh env fields + ``state.env_state`` from disk, re-render statics."""
        env = self._read_env_state()
        self.state.env_state = env
        self.query_one("#canvas-url", Input).value = env.get("base_url") or ""
        self.query_one("#canvas-token", _SecretInput).value = env.get("token") or ""
        self._render_statics()

    def action_save_env(self) -> None:
        """Write CANVAS_BASE_URL/CANVAS_ACCESS_TOKEN into ``<root_dir>/.env``.

        ``set_key`` preserves the file's other keys and comments (and creates
        the file when missing); both keys are always written as-is, even empty.
        """
        env_path = self.state.root_dir / ".env"
        url = str(self.query_one("#canvas-url", Input).value).strip()
        token = str(self.query_one("#canvas-token", _SecretInput).value)
        try:
            set_key(env_path, "CANVAS_BASE_URL", url)
            set_key(env_path, "CANVAS_ACCESS_TOKEN", token)
        except (OSError, UnicodeEncodeError) as exc:
            self._fail("Save .env", str(exc))
            return
        self._load_env_fields()
        self._set_result(f"[green]Saved .env to {env_path}[/green]")
        self.app.notify(f"Saved .env to {env_path}", severity="success")

    def action_reload_env(self) -> None:
        """Discard the field edits and re-read ``<root_dir>/.env`` from disk."""
        self._load_env_fields()
        self._set_result("[dim]Reloaded .env from disk[/dim]")

    # ---------- status ----------

    def _target_path(self) -> Path | None:
        if self._ctx == "assignment":
            assignment = self.state.current_assignment
            return assignment.config_path if assignment is not None else None
        if self._ctx == "course":
            course = self.state.current_course
            return course.config_path if course is not None else None
        return self._global_path()

    def _set_result(self, text: str) -> None:
        self._result = text
        self._update_status()

    def _update_status(self) -> None:
        target = self._target_path()
        if target is None:
            target_text = "n/a (no context selected)"
        elif self._ctx == "global" and not target.is_file():
            target_text = f"{target} (does not exist yet)"
        else:
            target_text = str(target)
        body = f"[b]Will write:[/b] {target_text}"
        if self._result:
            body += f"\n{self._result}"
        self.query_one("#settings-status", Static).update(body)

    def _fail(self, title: str, detail: str) -> None:
        self._set_result(f"[red]{title}:[/red] {detail}")
        self.app.notify(f"{title}: {detail}", severity="error")

    # ---------- save / validation ----------

    @staticmethod
    def _parse(  # ruff: ignore[too-many-return-statements]
        kind: str, text: str
    ) -> object:
        """Coerce widget text to a TOML value (raises ValueError on bad input)."""
        text = text.strip()
        if kind == "float":
            return float(text)
        if kind == "int":
            return int(text)
        if kind == "bool":
            low = text.lower()
            if low not in {"true", "false"}:
                msg = f"not a boolean: {text!r}"
                raise ValueError(msg)
            return low == "true"
        if kind == "list":
            return [part.strip() for part in text.split(",") if part.strip()]
        if kind == "prompt":
            if "," in text:
                return [part.strip() for part in text.split(",") if part.strip()]
            return text
        return text

    def _collect_edits(self) -> tuple[dict[str, dict[str, object]], list[str]]:
        """Delta of edited keys only: ``{section: {key: value}}``."""
        edits: dict[str, dict[str, object]] = {}
        errors: list[str] = []
        for fqid, (section, key, kind) in self._specs.items():
            widget = self._widgets[fqid]
            if widget.disabled:
                continue
            current = self._value_str(widget)
            if current == self._loaded.get(fqid):
                continue
            if isinstance(widget, _PromptCheckList):
                # Always a list; validated (non-empty) downstream.
                value: object = widget.value
            else:
                try:
                    value = self._parse(kind, current)
                except ValueError:
                    errors.append(f"Invalid value for '{section}.{key}': {current!r}")
                    continue
            edits.setdefault(section, {})[key] = value
        return edits, errors

    def _validate(self, edits: dict[str, dict[str, object]]) -> list[str]:
        """Validate edits before writing (rules live in :mod:`src.shared.config_edit`).

        Assignment context validates the final layered merged view — identical
        to what ``load_assignment_file`` will parse after the write (per-key
        overlay is layer-commutative). Course/global edits validate their own
        edited sections plus the weight-sum rule (design 05 §③).
        """
        target = self._target_path()
        if target is None:
            return ["no target config for the current context"]
        try:
            validate_config_edits(target, edits)
        except ValueError as exc:
            return [str(exc)]
        return []

    def action_save(self) -> None:
        edits, errors = self._collect_edits()
        if errors:
            self._fail("Invalid input", "; ".join(errors))
            return
        if not edits:
            self._set_result("[dim]No changes[/dim]")
            return
        target = self._target_path()
        if target is None:
            self._fail("Save", "no target config for the current context")
            return
        if self._ctx == "global" and not target.is_file():
            self._fail(
                "No global config",
                "data/config.toml does not exist — create it with $EDITOR (e) first",
            )
            return
        problems = self._validate(edits)
        if problems:
            self._fail("Validation failed", "; ".join(problems))
            return
        try:
            edit_config(target, edits)
        except OSError as exc:
            self._fail("Write failed", str(exc))
            return
        self._result = ""
        self._load_context()  # re-baseline the dirty snapshot from disk
        self._set_result(f"[green]Saved to {target}[/green]")
        self.app.notify(f"Saved to {target}", severity="success")

    # ---------- key actions ----------

    def action_reset(self) -> None:
        """Reload the current context's values from disk (design 05 §5 'r')."""
        self._load_context()
        self._set_result("[dim]Reloaded from disk[/dim]")

    def action_reset_field(self, fqid: str) -> None:
        """Delete ``section.key`` from the current layer config (F5).

        The key disappears from the target TOML (not nulled), so the merge
        view falls back to the upper layer (assignment context) or the schema
        default (course/global context), and the inherited badge reappears.
        """
        target = self._target_path()
        if target is None:
            self._fail("Reset field", "no target config for the current context")
            return
        section, key, _kind = self._specs[fqid]
        raw = self._read_raw(target)
        if not self._raw_key(raw, section, key):
            self._set_result(f"[dim]Reset {section}.{key}: no local value[/dim]")
            return
        try:
            edit_config(target, {}, deletes={section: [key]})
        except OSError as exc:
            self._fail("Reset field", str(exc))
            return
        self._load_context()
        self._set_result(
            f"[dim]Reset {section}.{key} — value removed at this layer[/dim]"
        )

    def action_edit_config(self) -> None:
        """Open the current context's config file in ``$EDITOR`` (design §5 'e')."""
        target = self._target_path()
        if target is None:
            self.app.notify("No config file for this context", severity="warning")
            return
        editor = os.environ.get("EDITOR")
        if not editor or shutil.which(editor.split()[0]) is None:
            self.app.notify(
                "$EDITOR not set or not found; set EDITOR to open config",
                severity="warning",
            )
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(f"{editor} {shlex.quote(str(target))}", shell=True, check=False)
        self._load_context()
        self._set_result("[dim]Config reloaded from disk[/dim]")
        self.app.notify("Config reloaded", severity="information")

    def action_test_canvas(self) -> None:
        """Probe Canvas in a background thread (design 05 §4; Canvas tab only)."""
        tabs = self.query_one("#settings-tabs", TabbedContent)
        if tabs.active != "tab-canvas":
            self.app.notify(
                "Test Canvas: switch to the Canvas tab first (2)", severity="warning"
            )
            return
        if not (self.state.env_state or {}).get("has_env"):
            self._set_result(
                "[red]Canvas: .env missing — configure CANVAS_BASE_URL / "
                "CANVAS_ACCESS_TOKEN first[/red]"
            )
            self.app.notify("Canvas .env missing", severity="warning")
            return
        self._set_result("[yellow]Testing Canvas connection…[/yellow]")

        def probe() -> None:
            try:
                base_url, token = load_env()
                courses = list_courses(Canvas(base_url=base_url, api_key=token))
                message = f"Canvas: OK — {len(courses)} course(s)"
                ok = True
            except BaseException as exc:  # load_env exits via SystemExit
                message = f"Canvas test failed: {type(exc).__name__}: {exc}"
                ok = False
            with contextlib.suppress(RuntimeError):  # app closed mid-probe
                self.call_from_thread(self._canvas_done, ok, message)

        self.run_worker(probe, thread=True, group="settings-test")

    def _canvas_done(self, ok: bool, message: str) -> None:
        color = "green" if ok else "red"
        self._set_result(f"[{color}]{message}[/{color}]")
        self.app.notify(message, severity="success" if ok else "error")

    def _set_tab(self, name: str) -> None:
        self.query_one("#settings-tabs", TabbedContent).active = name

    def action_tab_grading(self) -> None:
        self._set_tab("tab-grading")

    def action_tab_canvas(self) -> None:
        self._set_tab("tab-canvas")

    def action_tab_plagiarism(self) -> None:
        self._set_tab("tab-plagiarism")

    def action_tab_paths(self) -> None:
        self._set_tab("tab-paths")

    # ---------- messages ----------

    def on_select_changed(self, event: Select.Changed) -> None:
        """Apply only user-consistent context changes.

        Programmatic ``set_options``/``value`` writes post stale Changed
        messages (the selected option gets reset first); applying those as if
        the user had picked them would ping-pong the context forever. A
        message whose value no longer matches the widget's live value is
        stale; a live value equal to the current context is a repair no-op.
        """
        if event.select.id != "ctx-select":
            return
        new_ctx = str(event.value)
        if new_ctx == self._ctx:
            return
        if str(event.select.value) != new_ctx:
            return  # stale programmatic message — ignore
        self.set_context(new_ctx)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "btn-save":
            self.action_save()
        elif button_id == "btn-reset":
            self.action_reset()
        elif button_id == "btn-test-canvas":
            self.action_test_canvas()
        elif button_id == "btn-save-env":
            self.action_save_env()
        elif button_id == "btn-reload-env":
            self.action_reload_env()
        elif button_id in self._reset_fqids:
            self.action_reset_field(self._reset_fqids[button_id])
