"""Dashboard modals: Canvas course/assignment import, quick setup and the
alias-name editor. Pushed from :mod:`src.tui.app`'s DashboardScreen.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, override

import tomlkit
from canvasapi import Canvas
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Select, Static

from src.shared.aliases import load_alias_file, seed_course_alias, set_alias
from src.shared.canvas_fetch import list_assignments, list_courses, make_canvas_client
from src.shared.provider import get_providers
from src.tui.scan import CourseInfo

if TYPE_CHECKING:
    from src.tui.app import AppState


class _ImportBase(ModalScreen[object | None]):
    """Shared bits: esc-to-close + background Canvas option loading."""

    BINDINGS: ClassVar = [Binding("escape", "close", "Close", show=False)]

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self._items: list[tuple[int, str]] = []

    def action_close(self) -> None:
        self.dismiss(None)

    def _canvas(self) -> Canvas:
        return make_canvas_client(
            self.state.env_state["base_url"], self.state.env_state["token"]
        )

    def _safe_post(self, fn: Callable[..., None], *args: object) -> None:
        with suppress(Exception):
            self.app.call_from_thread(fn, *args)  # modal may be dismissed already


class ImportCourseModal(_ImportBase):
    """Import a course: pick a Canvas course -> create data/<dir>/config.toml."""

    @override
    def compose(self) -> ComposeResult:
        with Vertical(classes="confirm-modal"):
            yield Static("[b]Import course from Canvas[/b]")
            yield Select(
                [("Loading…", -1)], id="modal-canvas-course", allow_blank=False
            )
            yield Input(placeholder="Course dir (default: course id)", id="modal-dir")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Import", id="import", variant="primary")

    def on_mount(self) -> None:
        self.run_worker(self._load_worker, thread=True, group="stage", exclusive=True)

    def _load_worker(self) -> None:  # worker thread
        try:
            self._items = list_courses(self._canvas())
        except Exception as exc:
            self._safe_post(self._load_failed, str(exc))
            return
        self._safe_post(self._populate)

    def _load_failed(self, message: str) -> None:
        self.query_one("#modal-canvas-course", Select).set_options([
            (f"Error: {message}", -1)
        ])

    def _populate(self) -> None:
        select = self.query_one("#modal-canvas-course", Select)
        if not self._items:
            select.set_options([("No courses found — check .env", -1)])
            return
        select.set_options([(f"{cid} — {name}", cid) for cid, name in self._items])
        select.value = self._items[0][0]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "import":
            self._do_import()

    def _do_import(self) -> None:
        select = self.query_one("#modal-canvas-course", Select)
        if select.value is None or select.value == -1:
            self.app.notify("No course selected", severity="error")
            return
        course_id = select.value
        dir_name = self.query_one("#modal-dir", Input).value.strip() or str(course_id)
        dest = self.state.assignments_dir / dir_name
        if dest.exists():
            self.app.notify(f"Directory already exists: {dir_name}", severity="error")
            return
        dest.mkdir(parents=True)
        (dest / "config.toml").write_text(
            tomlkit.dumps(tomlkit.item({"fetch": {"course_id": course_id}})),
            encoding="utf-8",
        )
        name = next((n for cid, n in self._items if cid == course_id), None)
        if name:
            seed_course_alias(self.state.assignments_dir, course_id, name)
        state = self.state
        state.refresh_courses()
        state.current_course = next(
            (c for c in state.courses if c.dir_name == dir_name), None
        )
        if state.current_course is None:
            # scan_courses skips an empty course (is_course_config needs child
            # config.toml dirs); enter the Course view straight from the config
            # we just wrote so the empty state shows (design 01 §6.1).
            state.current_course = CourseInfo(
                dir_name=dir_name,
                config_path=dest / "config.toml",
                course_id=course_id,
            )
            state.assignments = []
        else:
            state.load_assignments(state.current_course)
        state.dashboard_level = "course"
        state.current_assignment = None
        self.dismiss(True)


class ImportAssignmentModal(_ImportBase):
    """Import an assignment: pick a Canvas assignment; fetch job after."""

    @override
    def compose(self) -> ComposeResult:
        with Vertical(classes="confirm-modal"):
            yield Static("[b]Import assignment from Canvas[/b]")
            yield Select([("Loading…", -1)], id="modal-assignment", allow_blank=False)
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Import", id="import", variant="primary")

    def on_mount(self) -> None:
        self.run_worker(self._load_worker, thread=True, group="stage", exclusive=True)

    def _load_worker(self) -> None:  # worker thread
        course_id = (
            self.state.current_course.course_id if self.state.current_course else None
        )
        if course_id is None:
            self._safe_post(self._load_failed, "No course_id")
            return
        try:
            self._items = list_assignments(self._canvas(), course_id)
        except Exception as exc:
            self._safe_post(self._load_failed, str(exc))
            return
        self._safe_post(self._populate)

    def _load_failed(self, message: str) -> None:
        self.query_one("#modal-assignment", Select).set_options([
            (f"Error: {message}", -1)
        ])

    def _populate(self) -> None:
        select = self.query_one("#modal-assignment", Select)
        if not self._items:
            select.set_options([("No assignments found — check course", -1)])
            return
        select.set_options([(f"{aid} — {name}", aid) for aid, name in self._items])
        select.value = self._items[0][0]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "import":
            self._do_import()

    def _do_import(self) -> None:
        select = self.query_one("#modal-assignment", Select)
        if select.value is None or select.value == -1:
            self.app.notify("No assignment selected", severity="error")
            return
        aid = select.value
        course = self.state.current_course
        if course is None:
            self.app.notify("No course selected", severity="error")
            return
        course_dir = self.state.assignments_dir / course.dir_name
        if (course_dir / str(aid)).exists():
            self.app.notify(f"Already imported: {aid}", severity="error")
            return
        name = next((n for id_, n in self._items if id_ == aid), None)
        self.dismiss((aid, name))


class AssignmentSetupModal(_ImportBase):
    """Quick setup after picking an assignment: rubric / prompt(s) / provider.

    Reads the local libraries (data/rubrics/*.toml, data/prompt/*.md) and the
    provider registry (data/providers/) synchronously. Import dismisses
    with ``{"rubric": "rubrics/<file>", "system_prompt": ["prompt/<file>",
    ...], "provider": "<name>"}`` (the Dashboard writes config.toml + aliases);
    Cancel dismisses None. Import stays disabled while no prompt is checked
    or any library is empty.
    """

    def __init__(self, state: AppState) -> None:
        super().__init__(state)
        data_dir = state.assignments_dir
        self._rubrics = sorted(p.name for p in (data_dir / "rubrics").glob("*.toml"))
        self._prompts = sorted(p.name for p in (data_dir / "prompt").glob("*.md"))
        try:
            providers = get_providers().providers
        except Exception:
            providers = {}
        self._providers = sorted(providers)
        errors = []
        if not self._rubrics:
            errors.append(
                "No rubrics found in data/rubrics — build one in the Library "
                "tab (Rubrics)."
            )
        if not self._prompts:
            errors.append("No prompt files found in data/prompt.")
        if not self._providers:
            errors.append(
                "No providers configured — add provider files to data/providers/."
            )
        self._error = " ".join(errors) or None

    @override
    def compose(self) -> ComposeResult:
        rubric_options = [(n, n) for n in self._rubrics] or [("No rubrics found", -1)]
        provider_options = [(n, n) for n in self._providers] or [
            ("No providers found", -1)
        ]
        with Vertical(classes="confirm-modal"):
            yield Static("[b]Assignment quick setup[/b]")
            if self._error:
                yield Static(self._error, id="setup-error")
            yield Static("Rubric", classes="setup-label")
            yield Select(rubric_options, id="setup-rubric", allow_blank=False)
            yield Static("Prompt(s) (multi-select)", classes="setup-label")
            with Vertical(id="setup-prompts"):
                for i, name in enumerate(self._prompts):
                    yield Checkbox(name, value=True, id=f"prompt-{i}")
            yield Static("Provider", classes="setup-label")
            yield Select(provider_options, id="setup-provider", allow_blank=False)
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="cancel")
                yield Button(
                    "Import",
                    id="import",
                    variant="primary",
                    disabled=self._error is not None,
                )

    def on_mount(self) -> None:
        self._update_import_enabled()

    def _selected_prompts(self) -> list[str]:
        container = self.query_one("#setup-prompts", Vertical)
        return [
            str(checkbox.label)
            for checkbox in container.query(Checkbox)
            if checkbox.value
        ]

    def _update_import_enabled(self) -> None:
        self.query_one("#import", Button).disabled = (
            self._error is not None or not self._selected_prompts()
        )

    def on_checkbox_changed(self, _event: Checkbox.Changed) -> None:
        self._update_import_enabled()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "import":
            self._do_import()

    def _do_import(self) -> None:
        rubric = self.query_one("#setup-rubric", Select).value
        provider = self.query_one("#setup-provider", Select).value
        prompts = self._selected_prompts()
        if not isinstance(rubric, str) or not rubric:
            self.app.notify("No rubric selected", severity="error")
            return
        if not isinstance(provider, str) or not provider:
            self.app.notify("No provider selected", severity="error")
            return
        if not prompts:
            self.app.notify("Select at least one prompt", severity="error")
            return
        self.dismiss({
            "rubric": f"rubrics/{rubric}",
            "system_prompt": [f"prompt/{p}" for p in prompts],
            "provider": provider,
        })


class AliasEditorModal(ModalScreen[bool | None]):
    """Edit one alias.toml entry: read-only key + editable name Input
    (initial value = the current alias name, empty if absent). Save writes
    the entry through :func:`src.shared.aliases.set_alias` (an empty name
    deletes the key); esc cancels without writing. Dismisses True on save,
    None on cancel.

    Callers pass the alias path/section/key of the selected item: course
    entries live in ``data/alias.toml`` ``[course]``, assignment entries in
    ``<course>/alias.toml`` ``[assignment]``.
    """

    BINDINGS: ClassVar = [Binding("escape", "close", "Close", show=False)]

    def __init__(self, alias_path: Path, section: str, key: str, title: str) -> None:
        super().__init__()
        self.alias_path = alias_path
        self.section = section
        self._key = key
        self._title = title

    @override
    def compose(self) -> ComposeResult:
        with Vertical(classes="confirm-modal"):
            yield Static(f"[b]{self._title}[/b]")
            with Horizontal(classes="alias-row"):
                yield Static(self._key, classes="alias-key")
                yield Input(
                    value=load_alias_file(self.alias_path)
                    .get(self.section, {})
                    .get(self._key, ""),
                    id="alias-name",
                )
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="primary")

    def action_close(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "save":
            self._do_save()

    def _do_save(self) -> None:
        name = self.query_one("#alias-name", Input).value.strip()
        try:
            set_alias(self.alias_path, self.section, self._key, name)
        except ValueError as exc:
            self.app.notify(str(exc), severity="error")
            return
        self.app.notify("Alias saved", severity="information")
        self.dismiss(True)
