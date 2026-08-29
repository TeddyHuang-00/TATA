"""TATA Settings screen (S5, T6b): three-layer config editing.

The screen edits ``config.toml`` at three layers — global
(``assignments/config.toml``), course (``assignments/<course>/config.toml``)
and assignment (``assignments/<course>/<name>/config.toml``) — selected by a
context ``Select``. The read path reuses :mod:`src.assignment_config`
(layered merge via :func:`load_assignment_file`); writes merge **only the
edited keys** into the target file and validate the result with the same
pydantic models before persisting (design 05 §4). All UI copy is English.

Hosted by :mod:`src.tata_app` (T6c) inside the Settings TabPane; this module
deliberately does not import or modify that file (the ``AppState`` type is
imported under TYPE_CHECKING only, breaking the circular import).

v1 scope limits (design 05): the provider registry is read-only (edit
``config/provider.toml`` with e=$EDITOR), ``.env`` is display-only, and
schema generation / full hook-model editing are not implemented.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, override

from canvasapi import Canvas
from pydantic import ValidationError
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
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

from src.assignment_config import (
    AssignmentFileConfig,
    AssignmentSection,
    FetchSection,
    GradingSection,
    PlagiarismSection,
    ProcessingSection,
    load_assignment_file,
)
from src.canvas_fetch import list_courses, load_env
from src.provider import ProviderInfo, get_providers

if TYPE_CHECKING:
    from src.tata_app import AppState

# (fqid, kind) for every text field; ``section.key`` is both the TOML path and
# the widget id suffix. ``prompt`` is a str-or-list-of-str field (system_prompt,
# rubric); ``list`` is a comma-separated extensions-style field.
_FIELD_SPECS: tuple[tuple[str, str], ...] = (
    ("grading.rubric", "prompt"),
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

# (fqid) Select fields: provider (assignment context) and fetch mode (course).
_SELECT_SPECS: tuple[str, ...] = ("grading.provider", "fetch.mode")

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

_SECTION_MODELS: dict[str, type] = {
    "assignment": AssignmentSection,
    "fetch": FetchSection,
    "grading": GradingSection,
    "processing": ProcessingSection,
    "plagiarism": PlagiarismSection,
}

_SAFE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_WEIGHT_EPS = 1e-6  # tolerance for the copydetect+embedding sum check


def _dump_toml(data: dict) -> str:
    """Serialize ``data`` back to TOML.

    ponytail: stdlib ``tomllib`` is read-only and the ``toml`` PyPI package is
    not a dependency, so this mini writer covers the shapes these configs
    actually contain (scalars, lists of scalars, ``[table]`` and
    ``[[table]]``/``[a.b]`` nesting). Keys are emitted bare when they match
    ``[A-Za-z0-9_-]+`` (true for every schema key), quoted otherwise; strings
    use JSON escaping, a subset of TOML basic-string escapes.
    """

    def scalar(value: object) -> str:
        if isinstance(value, str):
            return json.dumps(value)
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            return repr(value)
        if isinstance(value, list):
            return "[" + ", ".join(scalar(item) for item in value) + "]"
        message = f"unsupported TOML value: {type(value).__name__}"
        raise TypeError(message)

    def table(prefix: str, section: dict) -> list[str]:
        lines: list[str] = []
        for key, value in section.items():
            if value is None or isinstance(value, (dict, list)):
                continue
            lines.append(f"{_key(key)} = {scalar(value)}")
        for key, value in section.items():
            if value is None:
                continue
            name = f"{prefix}.{key}" if prefix else key
            if isinstance(value, list) and all(
                isinstance(item, dict) for item in value
            ):
                for item in value:
                    lines.append(f"[[{name}]]")
                    lines.extend(table(name, item))
            elif isinstance(value, dict):
                lines.append(f"[{name}]")
                lines.extend(table(name, value))
            elif isinstance(value, list):
                lines.append(f"{_key(key)} = {scalar(value)}")
        return lines

    def _key(key: str) -> str:
        return key if _SAFE_KEY.fullmatch(key) else json.dumps(key)

    return "\n".join(table("", data)) + "\n"


def merge_edits(original: dict, edits: dict) -> dict:
    """Overlay ``edits`` onto ``original`` per section-per-key."""
    merged = dict(original)
    for section, values in edits.items():
        if isinstance(values, dict) and isinstance(merged.get(section), dict):
            merged[section] = {**merged[section], **values}
        else:
            merged[section] = values
    return merged


def _field_widget_id(fqid: str) -> str:
    return f"f-{fqid.replace('.', '-')}"


def _read_registry() -> dict[str, ProviderInfo]:
    """Provider registry from ``config/provider.toml`` ({} on any failure)."""
    try:
        return get_providers().providers
    except Exception:  # display-only; the screen must not crash
        return {}


class _LField(Vertical):
    """Label + Input/Select/Checkbox pair (small compose helper)."""

    def __init__(self, label: str, widget: Input | Select | Checkbox) -> None:
        super().__init__(classes="settings-field")
        self._label = label
        self._widget = widget

    @override
    def compose(self) -> ComposeResult:
        yield Label(self._label)
        yield self._widget


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

    CSS = """
#settings-screen {
    height: 1fr;
    padding: 0 1;
}
#settings-top {
    height: auto;
    padding: 1 0;
}
#settings-title {
    width: auto;
    padding: 0 1 0 0;
}
#ctx-select {
    width: 52;
}
#ctx-hint {
    height: auto;
    padding: 0 0 1 0;
    color: $warning;
}
#settings-tabs {
    height: 1fr;
}
#settings-tabs TabPane {
    padding: 1 2;
}
#grading-registry {
    height: auto;
    padding: 0 0 1 0;
    color: $text-muted;
}
#canvas-env,
#canvas-fetch-list {
    height: auto;
    padding: 0 0 1 0;
}
#btn-test-canvas {
    width: auto;
    margin: 0 0 1 0;
}
#settings-actions {
    height: auto;
    padding: 1 0 0 0;
}
#settings-actions Button {
    margin: 0 1 0 0;
}
#settings-status {
    height: auto;
    padding: 0 1 1 1;
    border-top: solid $primary;
    color: $text-muted;
}
.settings-field {
    height: auto;
    padding: 0 0 1 0;
}
.settings-field Label {
    color: $text-muted;
    text-style: bold;
}
.settings-field Input,
.settings-field Select,
.settings-field Checkbox {
    width: 1fr;
}
"""

    def __init__(self, state: AppState) -> None:
        super().__init__(id="settings-screen")
        self.state = state
        self._ctx: str = ""  # unset until on_mount sets the initial context
        self._widgets: dict[str, Input | Select | Checkbox] = {}
        self._loaded: dict[str, str] = {}
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
            with TabPane("Grading", id="tab-grading"):
                yield Static("", id="grading-registry")
                yield _LField(
                    "provider (from config/provider.toml, read-only registry)",
                    self._select("grading.provider"),
                )
                yield _LField("rubric", self._input("grading.rubric"))
                yield _LField(
                    "system_prompt (comma-separated file paths)",
                    self._input("grading.system_prompt"),
                )
                yield _LField(
                    "max_parallel_tasks (1..10)",
                    self._input("grading.max_parallel_tasks"),
                )
            with TabPane("Canvas", id="tab-canvas"):
                yield Static("", id="canvas-env")
                yield Static("", id="canvas-fetch-list")
                yield _LField(
                    "course_id (Canvas course, numeric)",
                    self._input("fetch.course_id"),
                )
                yield _LField("fetch mode", self._select("fetch.mode"))
                yield Button("Test Canvas connection", id="btn-test-canvas")
            with TabPane("Plagiarism", id="tab-plagiarism"):
                yield _LField(
                    "copydetect_weight (0..1)",
                    self._input("plagiarism.copydetect_weight"),
                )
                yield _LField(
                    "embedding_weight (0..1)",
                    self._input("plagiarism.embedding_weight"),
                )
                yield _LField(
                    "display_threshold (0..1)",
                    self._input("plagiarism.display_threshold"),
                )
                yield _LField(
                    "pairwise_alpha (>0, <=1)", self._input("plagiarism.pairwise_alpha")
                )
                yield _LField(
                    "individual_alpha (>0, <=1)",
                    self._input("plagiarism.individual_alpha"),
                )
                yield _LField(
                    "score_floor (0..0.5)", self._input("plagiarism.score_floor")
                )
                yield _LField("score_cap (0.5..1)", self._input("plagiarism.score_cap"))
                yield _LField(
                    "embedding_model", self._input("plagiarism.embedding_model")
                )
                yield _LField(
                    "extensions (comma-separated, e.g. .py, .ipynb)",
                    self._input("plagiarism.extensions"),
                )
            with TabPane("Paths / Advanced", id="tab-paths"):
                yield _LField("raw_dir", self._input("assignment.raw_dir"))
                yield _LField("processed_dir", self._input("assignment.processed_dir"))
                yield _LField("graded_dir", self._input("assignment.graded_dir"))
                yield _LField("logs_dir", self._input("assignment.logs_dir"))
                yield _LField(
                    "reference_file (optional)",
                    self._input("assignment.reference_file"),
                )
                yield _LField(
                    "template_file ([plagiarism])",
                    self._input("plagiarism.template_file"),
                )
                for fqid, label in _CHECKBOX_SPECS:
                    yield _LField(label, self._checkbox(fqid))
        with Horizontal(id="settings-actions"):
            yield Button("Save (ctrl+s)", id="btn-save", variant="primary")
            yield Button("Reset (r)", id="btn-reset")
        yield Static("", id="settings-status")

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

    @override
    def on_mount(self) -> None:
        self._registry = _read_registry()
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
        """(value, label) pairs for ``#ctx-select``."""
        options: list[tuple[str, str]] = [("global", "Global")]
        if self.state.current_course is not None:
            options.append(("course", f"Course: {self.state.current_course.dir_name}"))
        if self.state.current_assignment is not None:
            options.append((
                "assignment",
                f"Assignment: {self.state.current_assignment.dir_name}",
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
        return self.state.root_dir / "assignments" / "config.toml"

    @staticmethod
    def _read_raw(path: Path | None) -> dict:
        if path is None or not path.is_file():
            return {}
        try:
            return tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return {}

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
                "No global config file (assignments/config.toml) — press e to "
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
            self._set_widget_value(widget, kind, value)
            self._loaded[fqid] = self._value_str(widget)
            widget.disabled = not self._writable(section)
        self._render_statics()
        self._update_status()

    def _set_select_options(self, widget: Select, fqid: str, current: str) -> None:
        if fqid == "grading.provider":
            names = sorted(self._registry)
            options = [(name, name) for name in names]
            if current and current not in names:
                options.append((f"{current} (not in registry)", current))
            if not options:
                options = [("(no available provider)", "")]
        else:  # fetch.mode
            modes = {"attach", "text", "auto"}
            options = [(mode, mode) for mode in sorted(modes)]
            if current and current not in modes:
                options.append((current, current))
        widget.set_options(options)
        values = {value for _, value in widget._options if value is not Select.NULL}
        if current in values:
            widget.value = current

    @staticmethod
    def _set_widget_value(
        widget: Input | Select | Checkbox, kind: str, value: object
    ) -> None:
        if isinstance(widget, Checkbox):
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
    def _value_str(widget: Input | Select | Checkbox) -> str:
        if isinstance(widget, Checkbox):
            return "true" if widget.value else "false"
        if isinstance(widget, Select):
            return "" if widget.value is Select.NULL else str(widget.value)
        return str(widget.value or "")

    def _render_statics(self) -> None:
        registry = self.query_one("#grading-registry", Static)
        if self._registry:
            lines = [
                f"[b]{name}[/b] · {info.base_url} · {info.model}"
                for name, info in sorted(self._registry.items())
            ]
            registry.update(
                "\n".join(lines) + "\n[dim]Provider registry is read-only here — edit "
                "config/provider.toml with $EDITOR (e).[/dim]"
            )
        else:
            registry.update(
                "[red]Provider registry unavailable[/red] — "
                "config/provider.toml missing or invalid.\n"
                "[dim]Providers are read-only here — edit config/provider.toml "
                "with $EDITOR (e).[/dim]"
            )

        env = self.state.env_state or {}
        if env.get("has_env"):
            env_text = (
                f"[green]Canvas .env: found[/green] — {env.get('base_url')}, "
                "token set. [dim].env is read-only here; edit it directly.[/dim]"
            )
        else:
            env_text = (
                "[yellow]Canvas .env: not found[/yellow] — set CANVAS_BASE_URL / "
                "CANVAS_ACCESS_TOKEN in .env (gitignored), then press t to test."
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
                    f"  {entry.get('assignment_id')} -> {entry.get('out')}"
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
    def _parse(kind: str, text: str) -> object:
        """Coerce widget text to a TOML value (raises ValueError on bad input)."""
        text = text.strip()
        if kind == "float":
            return float(text)
        if kind == "int":
            return int(text)
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
            try:
                value = self._parse(kind, current)
            except ValueError:
                errors.append(f"Invalid value for '{section}.{key}': {current!r}")
                continue
            edits.setdefault(section, {})[key] = value
        return edits, errors

    @staticmethod
    def _fmt_errors(exc: ValidationError) -> list[str]:
        return [
            f"{'.'.join(str(part) for part in e.get('loc', []))}: {e.get('msg')}"
            for e in exc.errors()
        ]

    def _validate(self, edits: dict[str, dict[str, object]]) -> list[str]:
        """Validate edits against the pydantic models before writing.

        Assignment context validates the final layered merged view — identical
        to what ``load_assignment_file`` will parse after the write (per-key
        overlay is layer-commutative). Course/global edits validate their own
        edited sections plus the weight-sum rule (design 05 §③).
        """
        if self._ctx == "assignment":
            errors = self._validate_assignment_edit(edits)
            plag = self._effective_plagiarism(edits)
        else:
            errors = self._validate_sections(edits)
            # Edits only carry changed keys, so a one-key weight edit would
            # dodge the sum rule; validate the effective layer (schema
            # defaults + raw file + edits) like assignment context does.
            plag = dict(self._layer_view().get("plagiarism") or {})
            plag.update(edits.get("plagiarism") or {})
        weight_error = self._weight_sum_error(plag)
        if weight_error is not None:
            errors.append(weight_error)
        return errors

    def _validate_assignment_edit(
        self, edits: dict[str, dict[str, object]]
    ) -> list[str]:
        base = self._assignment_view()
        if base is None:
            return self._validate_sections(edits)
        try:
            AssignmentFileConfig.model_validate(merge_edits(base, edits))
        except ValidationError as exc:
            return self._fmt_errors(exc)
        return []

    def _validate_sections(self, edits: dict[str, dict[str, object]]) -> list[str]:
        errors: list[str] = []
        for section, values in edits.items():
            model = _SECTION_MODELS.get(section)
            if model is None:
                continue
            try:
                model.model_validate(values)
            except ValidationError as exc:
                errors.extend(self._fmt_errors(exc))
        return errors

    def _effective_plagiarism(self, edits: dict[str, dict[str, object]]) -> dict:
        base = self._assignment_view()
        if base is None:
            return edits.get("plagiarism") or {}
        merged = merge_edits(base, edits)
        return merged.get("plagiarism") or {}

    @staticmethod
    def _weight_sum_error(plag: dict) -> str | None:
        w1, w2 = plag.get("copydetect_weight"), plag.get("embedding_weight")
        if not isinstance(w1, (int, float)) or not isinstance(w2, (int, float)):
            return None
        total = w1 + w2
        if abs(total - 1.0) > _WEIGHT_EPS:
            return f"plagiarism weights: sum {total:.2f} ≠ 1.00"
        return None

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
                "assignments/config.toml does not exist — create it with "
                "$EDITOR (e) first",
            )
            return
        problems = self._validate(edits)
        if problems:
            self._fail("Validation failed", "; ".join(problems))
            return
        original = self._read_raw(target)
        content = _dump_toml(merge_edits(original, edits))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
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
