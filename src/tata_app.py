"""TATA Workbench — Textual TUI platform shell (T4a).

Three-tab shell (Dashboard / Plagiarism / Settings) with the S1 three-level
dashboard (Global -> Course -> Assignment placeholder) on top of
:mod:`src.tata_scan`. All UI copy is English.

Run: ``uv run python src/tata_app.py``
"""

from __future__ import annotations

import time
from collections.abc import Callable, MutableMapping
from contextlib import suppress
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import ClassVar, Literal, override

import main as main_mod
import tomlkit
from canvasapi import Canvas
from rich.markup import escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    RadioButton,
    RadioSet,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from src.aliases import assignment_display_name, course_display_name
from src.assignment_config import FetchSection
from src.canvas_fetch import list_assignments, list_courses
from src.cli_options import FetchCliOptions
from src.plagiarism import detect_plagiarism
from src.score_review import ScoreReviewScreen
from src.tata_plagiarism import PlagiarismScreen
from src.tata_scan import AssignmentInfo, CourseInfo, scan_assignments, scan_courses
from src.tata_settings import SettingsScreen
from src.tata_workspace import (
    AssignmentScreen,
    ConfirmationModal,
    _fmt_last_run,
    _fmt_state,
    _is_displayed,
    _state_key,
)


def _env_status(root_dir: Path) -> dict:
    """Probe for ``.env`` (CANVAS_BASE_URL/CANVAS_ACCESS_TOKEN) without exiting.

    Mirrors :func:`src.canvas_fetch.load_env`: walk root_dir then its
    ancestors; a .env missing either key is skipped, keep walking up.
    """
    for d in [root_dir, *root_dir.parents]:
        env_path = d / ".env"
        if not env_path.is_file():
            continue
        vals: dict[str, str] = {}
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                vals[key.strip()] = value.strip()
        if "CANVAS_BASE_URL" in vals and "CANVAS_ACCESS_TOKEN" in vals:
            return {
                "has_env": True,
                "base_url": vals["CANVAS_BASE_URL"],
                "token": vals["CANVAS_ACCESS_TOKEN"],
                "token_set": True,
            }
    return {"has_env": False, "base_url": None, "token": None, "token_set": False}


@dataclass
class AppState:
    """Shared platform state (design 00 §5)."""

    root_dir: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent.parent
    )
    courses: list[CourseInfo] = field(default_factory=list)
    current_course: CourseInfo | None = None
    assignments: list[AssignmentInfo] = field(default_factory=list)
    current_assignment: AssignmentInfo | None = None
    dashboard_level: str = "global"  # global | course | assignment
    env_state: dict = field(default_factory=dict)
    # App-level job mutex (M1): name of the stage currently running, shared by
    # DashboardScreen / AssignmentScreen / PlagiarismScreen (group-exclusive
    # workers are per-DOM-node and never cancel each other across screens).
    active_job: str | None = None

    @property
    def assignments_dir(self) -> Path:
        return self.root_dir / "data"

    def refresh_courses(self) -> None:
        self.courses = scan_courses(self.assignments_dir)

    def load_assignments(self, course: CourseInfo) -> None:
        self.assignments = scan_assignments(self.assignments_dir / course.dir_name)


def _fmt_score(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else "-"


# State filters (design 01 §5: 1=All 2=Done 3=Partial 4=Not run 5=Flagged).
_FILTER_LABELS: dict[str | None, str] = {
    None: "All",
    "done": "Done",
    "partial": "Partial",
    "not_run": "Not run",
    "flagged": "Flagged",
}


def _filter_assignments(
    assignments: list[AssignmentInfo], flt: str | None
) -> list[AssignmentInfo]:
    if flt is None:
        return assignments
    if flt == "flagged":
        return [a for a in assignments if a.flagged_pairs]
    return [a for a in assignments if _state_key(a) == flt]


class _FocusableStatic(Static):
    """Static that keeps focus when the DataTable is hidden (empty/placeholder
    states), so the screen's esc/backspace/r bindings keep working."""

    can_focus = True


class DashboardScreen(Vertical):
    """S1 Dashboard: view stack switching on ``state.dashboard_level``."""

    BINDINGS: ClassVar = [
        Binding("escape", "go_up", "Up one level"),
        Binding("backspace", "go_up", "Up one level"),
        Binding("r", "rescan", "Rescan"),
        Binding("c", "import_item", "Import"),
        Binding("g", "global_config", "Global config"),
        Binding("o", "course_config", "Course config"),
        Binding("F", "fetch_all", "Fetch all"),
        Binding("p", "plagiarism_run", "Plagiarism"),
        Binding("s", "score_review", "Score review"),
        Binding("1", "filter_all", "Filter: All"),
        Binding("2", "filter_done", "Filter: Done"),
        Binding("3", "filter_partial", "Filter: Partial"),
        Binding("4", "filter_not_run", "Filter: Not run"),
        Binding("5", "filter_flagged", "Filter: Flagged"),
    ]

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self._rows: list[object] = []
        # Last selected row per dashboard level (dir_name); DataTable rebuilds
        # in render_level drop the cursor, so we re-seat it after re-render.
        self._last_dir: dict[str, str] = {}
        self._filter: str | None = None  # course-level state filter
        self._job: dict | None = None  # minimal job protocol (see _start_job)
        # Per-target state of the last/current fetch-all (F) run; None = panel
        # hidden. Each entry: {"label", "state", "err", "seconds"}.
        self._fetch_progress: list[dict] | None = None
        self._fetch_done = False  # fetch-all completed; panel stays visible

    @override
    def compose(self) -> ComposeResult:
        yield Static(id="topbar", markup=True)
        yield Static(id="breadcrumb", markup=True)
        yield DataTable(id="dashboard-table", cursor_type="row", zebra_stripes=True)
        yield _FocusableStatic(id="dash-empty", markup=True)
        yield AssignmentScreen(self.state)
        progress: Static = Static(id="dash-progress", markup=True)
        progress.display = False  # fetch-all panel; shown by fetch-all only
        yield progress
        yield Static(id="dash-status", markup=True)

    def on_mount(self) -> None:
        self.state.refresh_courses()
        self.render_level()

    # ---------- rendering ----------

    def render_level(self) -> None:
        state = self.state
        table = self.query_one("#dashboard-table", DataTable)
        breadcrumb = self.query_one("#breadcrumb", Static)
        topbar = self.query_one("#topbar", Static)
        empty = self.query_one("#dash-empty", Static)
        workspace = self.query_one(AssignmentScreen)

        table.clear(columns=True)
        self._rows = []
        canvas = (
            "Canvas: OK"
            if state.env_state.get("has_env")
            else "Canvas: ? (.env missing)"
        )

        if state.dashboard_level == "global":
            topbar.update(
                f"[b]TATA[/b] · Dashboard [Global]   {canvas}   "
                f"Courses: {len(state.courses)}"
            )
            breadcrumb.update("Global")
            table.add_columns(
                "Course",
                "Assignments",
                "Raw",
                "Proc",
                "Grad",
                "Avg score",
                ">80% pairs",
                "Last run",
            )
            for i, c in enumerate(state.courses):
                table.add_row(
                    escape(course_display_name(state.assignments_dir, c.dir_name, c.course_id)),
                    str(c.assignment_count),
                    str(c.counts.raw),
                    str(c.counts.processed),
                    str(c.counts.graded),
                    _fmt_score(c.score_mean),
                    str(c.flagged_pairs),
                    _fmt_last_run(c.last_run),
                    key=str(i),
                )
                self._rows.append(c)
            self._show_empty(
                empty,
                "No courses yet. Press `c` to import (configure .env first).",
                table,
                workspace,
            )
        elif state.dashboard_level == "course":
            course = state.current_course
            assert course is not None  # course level implies a selected course
            course_name = course_display_name(
                state.assignments_dir, course.dir_name, course.course_id
            )
            topbar.update(
                f"[b]TATA[/b] · Dashboard [Course: {escape(course_name)}]   {canvas}"
                + (
                    f"   Filter: {_FILTER_LABELS[self._filter]}"
                    if self._filter is not None
                    else ""
                )
            )
            breadcrumb.update(f"Global / [b]{escape(course_name)}[/b]")
            table.add_columns(
                "Assignment",
                "ID",
                "Raw",
                "Proc",
                "Grad",
                "Avg",
                "State",
                "Last run",
            )
            shown = _filter_assignments(state.assignments, self._filter)
            for i, a in enumerate(shown):
                table.add_row(
                    escape(
                        assignment_display_name(
                            state.assignments_dir,
                            course.dir_name,
                            a.dir_name,
                            a.assignment_id,
                        )
                    ),
                    str(a.assignment_id or "-"),
                    str(a.counts.raw),
                    str(a.counts.processed),
                    str(a.counts.graded),
                    _fmt_score(a.score_summary),
                    _fmt_state(a),
                    _fmt_last_run(a.last_run),
                    key=str(i),
                )
                self._rows.append(a)
            self._show_empty(
                empty,
                "No assignments match the filter."
                if self._filter is not None
                else "No assignments in this course yet.",
                table,
                workspace,
            )
        else:  # assignment — T4b workspace
            a = state.current_assignment
            assert a is not None  # assignment level implies a selected assignment
            course = state.current_course
            course_dir_name = course.dir_name if course is not None else ""
            a_name = assignment_display_name(
                state.assignments_dir, course_dir_name, a.dir_name, a.assignment_id
            )
            course_name = (
                course_display_name(
                    state.assignments_dir, course.dir_name, course.course_id
                )
                if course is not None
                else ""
            )
            topbar.update(
                f"[b]TATA[/b] · Dashboard [Assignment: {escape(a_name)}]   {canvas}"
            )
            breadcrumb.update(
                f"Global / {escape(course_name)} / [b]{escape(a_name)}[/b]"
            )
            table.display = False
            empty.display = False
            workspace.display = True
            workspace.open_assignment()

        # Fetch-all progress panel: live during the run and after completion;
        # hidden on any level/navigation change (course level only). A new
        # non-fetch-all job also clears _fetch_progress in _start_job.
        self.query_one("#dash-progress", Static).display = (
            state.dashboard_level == "course"
            and self._fetch_progress is not None
            and (self.state.active_job == "fetch-all" or self._fetch_done)
        )
        self._restore_cursor(table)
        self._refocus()

    def _remember_selection(self) -> None:
        """Remember this level's selected row (dir_name) before navigation or
        rescan rebuilds the table — render_level re-seats the cursor with it."""
        table = self.query_one("#dashboard-table", DataTable)
        if not table.display:
            return
        sel = self._selected()
        if sel is not None and hasattr(sel, "dir_name"):
            self._last_dir[self.state.dashboard_level] = sel.dir_name  # type: ignore[attr-defined]

    def _restore_cursor(self, table: DataTable) -> None:
        """Re-seat the cursor on the row matching this level's last selection."""
        target = self._last_dir.get(self.state.dashboard_level)
        if target is None:
            return
        for i, row in enumerate(self._rows):
            if getattr(row, "dir_name", None) == target:
                table.move_cursor(row=i)
                return

    def _refocus(self) -> None:
        """Keep focus on a visible descendant so bindings keep firing."""
        if not _is_displayed(self):
            return  # dashboard tab hidden — never steal focus (F2)
        table = self.query_one("#dashboard-table", DataTable)
        if _is_displayed(table):
            table.focus()
        elif self.state.dashboard_level == "assignment":
            self.query_one(AssignmentScreen).focus_stage()
        else:
            self.query_one("#dash-empty").focus()

    def _show_empty(
        self, empty: Static, text: str, table: DataTable, workspace: AssignmentScreen
    ) -> None:
        workspace.display = False
        has_rows = bool(self._rows)
        table.display = has_rows
        empty.display = not has_rows
        empty.update(text)

    # ---------- navigation ----------

    def _selected(self) -> object | None:
        idx = self.query_one("#dashboard-table", DataTable).cursor_row
        if 0 <= idx < len(self._rows):
            return self._rows[idx]
        return None

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        self.action_go_down()

    def action_go_down(self) -> None:
        state = self.state
        if state.dashboard_level == "assignment":
            return  # T4b: assignment workspace comes later
        item = self._selected()
        if item is None:
            return
        self._remember_selection()
        if state.dashboard_level == "global":
            state.current_course = item  # type: ignore[assignment]
            state.dashboard_level = "course"
            state.load_assignments(state.current_course)
            state.current_assignment = None
        else:
            state.current_assignment = item  # type: ignore[assignment]
            state.dashboard_level = "assignment"
        self.render_level()

    def action_go_up(self) -> None:
        state = self.state
        self._remember_selection()
        if state.dashboard_level == "assignment":
            state.dashboard_level = "course"
            state.current_assignment = None
        elif state.dashboard_level == "course":
            state.dashboard_level = "global"  # current_course retained
        else:
            return
        self.render_level()

    def action_rescan(self) -> None:
        state = self.state
        self._remember_selection()
        if state.dashboard_level == "global":
            state.refresh_courses()
        else:
            state.load_assignments(state.current_course)
        self.render_level()
        self.app.notify("Rescan complete", severity="information")

    # ---------- actions: import / config / fetch / plagiarism / review / filter ----------

    def action_import_item(self) -> None:
        if self._job is not None:
            self.app.notify("A job is already running", severity="warning")
            return
        if self.state.dashboard_level == "global":
            self._import_course()
        elif self.state.dashboard_level == "course":
            self._import_assignment()

    def _import_course(self) -> None:
        if not self.state.env_state.get("has_env"):
            self.app.notify(
                "Canvas environment missing — set CANVAS_BASE_URL/"
                "CANVAS_ACCESS_TOKEN in .env",
                severity="error",
            )
            return
        self.app.push_screen(
            ImportCourseModal(self.state), callback=self._on_course_imported
        )

    def _on_course_imported(self, value: object) -> None:
        if value:
            self.render_level()

    def _import_assignment(self) -> None:
        if not self.state.env_state.get("has_env"):
            self.app.notify(
                "Canvas environment missing — set CANVAS_BASE_URL/"
                "CANVAS_ACCESS_TOKEN in .env",
                severity="error",
            )
            return
        course = self.state.current_course
        if course is None or course.course_id is None:
            self.app.notify("Current course has no course_id", severity="error")
            return
        self.app.push_screen(
            ImportAssignmentModal(self.state), callback=self._on_assignment_imported
        )

    def _on_assignment_imported(self, value: object) -> None:
        if not isinstance(value, tuple):
            return
        try:
            aid, out, mode = value  # type: ignore[misc]
        except (TypeError, ValueError):
            return
        if not isinstance(aid, int) or not isinstance(out, str) or not isinstance(mode, str):
            return
        course = self.state.current_course
        if course is None or course.course_id is None:
            return
        mode_val: Literal["attach", "text", "auto"] = (
            mode if mode in {"attach", "text", "auto"} else "auto"
        )  # type: ignore[assignment]
        self._start_job(
            "fetch",
            partial(self._fetch_one, course, aid, out, mode_val),
            after=self._rescan_course,
        )

    @staticmethod
    def _fetch_one(
        course: CourseInfo, aid: int, out: str, mode: Literal["attach", "text", "auto"]
    ) -> None:
        main_mod._run_fetch(
            FetchCliOptions(
                course=course.course_id,
                assignment=aid,
                out=f"{out}/raw",
                config=course.config_path,
                mode=mode,
            )
        )
        # M3: record the assignment in the course config's [[fetch.assignments]]
        # so fetch-all (F) picks it up later. CLI's _remember does not maintain
        # that list; a plain append lands in [fetch] (TOML table headers are
        # absolute). Dedup on assignment_id; skip when the config is unreadable
        # — the fetch itself already succeeded.
        cfg_path = course.config_path
        try:
            doc = tomlkit.parse(cfg_path.read_text(encoding="utf-8"))
        except (OSError, tomlkit.exceptions.ParseError):
            return
        fetch = doc.get("fetch")
        entries = (
            fetch.get("assignments") if isinstance(fetch, MutableMapping) else None
        )
        if isinstance(entries, list) and any(
            isinstance(e, dict) and e.get("assignment_id") == aid for e in entries
        ):
            return
        if isinstance(fetch, MutableMapping):
            if "assignments" not in fetch:
                fetch["assignments"] = tomlkit.aot()
            fetch["assignments"].append({"assignment_id": aid, "out": f"{out}/raw"})
        else:
            doc["fetch"] = {"assignments": tomlkit.aot()}
            doc["fetch"]["assignments"].append({
                "assignment_id": aid,
                "out": f"{out}/raw",
            })
        cfg_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

    def _rescan_course(self) -> None:
        if self.state.current_course is not None:
            self.state.load_assignments(self.state.current_course)
            self.render_level()

    def action_fetch_all(self) -> None:
        if self.state.dashboard_level != "course" or self.state.current_course is None:
            return
        if self._job is not None:
            self.app.notify("A job is already running", severity="warning")
            return
        course = self.state.current_course
        cfg = self._fetch_all_section(course)
        if cfg is None:
            self.app.notify(
                "No assignments configured for fetch — add [[fetch.assignments]] "
                "to the course config",
                severity="warning",
            )
            return
        self.app.push_screen(
            ConfirmationModal(
                "Fetch all",
                f"Fetch {len(cfg.assignments)} assignment(s) in this course?",
                [("Fetch", "run")],
            ),
            callback=self._on_fetch_all_confirmed,
        )

    @staticmethod
    def _fetch_all_section(course: CourseInfo) -> FetchSection | None:
        """Course [fetch] section via main.py's loader (empty list -> None).

        Mirrors the CLI's root-config model exactly: ``[[fetch.assignments]]``
        entries with ``out`` (already '<aid>/raw') and ``mode`` falling back to
        the root [fetch] mode.
        """
        try:
            cfg = main_mod._root_fetch(course.config_path)
        except ValueError:
            return None
        if cfg is None or cfg.course_id is None or not cfg.assignments:
            return None
        return cfg

    def _on_fetch_all_confirmed(self, value: object) -> None:
        if value != "run" or self.state.current_course is None:
            return
        course = self.state.current_course
        cfg = self._fetch_all_section(course)
        if cfg is None:
            return
        targets: list[dict] = []
        for entry in cfg.assignments:
            # Alias-aware label; the out path's parent dir names the
            # assignment (e.g. '2978557') — raw paths are never shown.
            label = assignment_display_name(
                self.state.assignments_dir,
                course.dir_name,
                Path(entry.out).parent.name or str(entry.assignment_id),
                entry.assignment_id,
            )
            targets.append(
                {"label": label, "state": "pending", "err": "", "seconds": 0.0}
            )
        self._fetch_progress = targets
        self._fetch_done = False
        self._render_fetch_progress()
        entries = list(cfg.assignments)
        course_id = cfg.course_id
        config_path = course.config_path

        def job() -> None:  # worker thread
            for i, entry in enumerate(entries):
                self._mark_fetch(i, "running")
                t0 = time.monotonic()
                try:
                    main_mod._run_fetch(
                        FetchCliOptions(
                            course=course_id,
                            assignment=entry.assignment_id,
                            out=entry.out,  # entry.out already ends in '/raw'
                            config=config_path,
                            mode=entry.mode or cfg.mode,
                        )
                    )
                    self._mark_fetch(i, "done", seconds=time.monotonic() - t0)
                except BaseException as exc:  # per-target failure: keep going
                    self._mark_fetch(
                        i, "failed", err=str(exc), seconds=time.monotonic() - t0
                    )

        self._start_job("fetch-all", job, after=self._rescan_course)

    def _mark_fetch(
        self,
        index: int,
        state: str,
        err: str | None = None,
        seconds: float | None = None,
    ) -> None:  # worker thread (mutates state, renders on the main thread)
        if self._fetch_progress is None:
            return
        target = self._fetch_progress[index]
        target["state"] = state
        if err is not None:
            target["err"] = err
        if seconds is not None:
            target["seconds"] = seconds
        self.app.call_from_thread(self._render_fetch_progress)

    def _render_fetch_progress(self) -> None:  # main thread
        if self._fetch_progress is None:
            return
        lines = []
        for target in self._fetch_progress:
            label = escape(target["label"])
            if target["state"] == "running":
                lines.append(f"[yellow]▶ {label}[/yellow]")
            elif target["state"] == "done":
                lines.append(
                    f"[green]✓ {label} ({target['seconds']:.1f}s)[/green]"
                )
            elif target["state"] == "failed":
                # Escape markup brackets; keep the line short (no paths).
                err = (target["err"] or "").replace("[", r"\[")[:60]
                lines.append(f"[red]✗ {label} — {err}[/red]")
            else:
                lines.append(f"[dim]○ {label}[/dim]")
        panel = self.query_one("#dash-progress", Static)
        panel.display = True
        panel.update("\n".join(lines))
        if self.state.active_job == "fetch-all":
            done = sum(
                1 for t in self._fetch_progress if t["state"] != "pending"
            )
            self.query_one("#dash-status", Static).update(
                f"Fetching {done}/{len(self._fetch_progress)}…"
            )

    def action_plagiarism_run(self) -> None:
        if self.state.dashboard_level != "course" or self.state.current_course is None:
            return
        if self._job is not None:
            self.app.notify("A job is already running", severity="warning")
            return
        self.app.push_screen(
            ConfirmationModal(
                "Plagiarism",
                "Run plagiarism + aggregate for all assignments in this course?",
                [("Run", "run")],
            ),
            callback=self._on_plagiarism_confirmed,
        )

    def _on_plagiarism_confirmed(self, value: object) -> None:
        if value != "run" or self.state.current_course is None:
            return
        course = self.state.current_course

        def job() -> None:
            detect_plagiarism(course.config_path, aggregate=True)

        def after() -> None:
            self._rescan_course()
            self.app.switch_tab("tab-plagiarism")

        self._start_job("plagiarism", job, after=after)

    def action_global_config(self) -> None:
        if self.state.dashboard_level != "global":
            return
        self._open_settings("global")

    def action_course_config(self) -> None:
        if self.state.dashboard_level != "course":
            return
        self._open_settings("course")

    def _open_settings(self, ctx: str) -> None:
        settings = self.app.query_one(SettingsScreen)
        with suppress(Exception):
            settings.set_context(ctx)  # may run before SettingsScreen.on_mount
        self.app.switch_tab("tab-settings")

    def action_score_review(self) -> None:
        if self.state.dashboard_level != "course":
            return  # 's' belongs to the assignment workspace below course
        item = self._selected()
        if item is None:
            return
        graded_dir = item.config_path.parent / "graded"  # type: ignore[attr-defined]
        if not list(graded_dir.glob("*.json")):
            self.app.notify("No graded files — run grade first", severity="warning")
            return
        self.app.push_screen(ScoreReviewScreen(graded_dir, pop_on_escape=True))

    def action_filter_all(self) -> None:
        self._set_filter(None)

    def action_filter_done(self) -> None:
        self._set_filter("done")

    def action_filter_partial(self) -> None:
        self._set_filter("partial")

    def action_filter_not_run(self) -> None:
        self._set_filter("not_run")

    def action_filter_flagged(self) -> None:
        self._set_filter("flagged")

    def _set_filter(self, value: str | None) -> None:
        if self.state.dashboard_level != "course":
            return
        self._filter = value
        self.render_level()
        self.app.notify(f"Filter: {_FILTER_LABELS[value]}", severity="information")

    # ---------- minimal job protocol (ponytail: status text only, no queue) ----------

    def _start_job(
        self, stage: str, fn: Callable[[], None], after: Callable[[], None] | None = None
    ) -> None:
        """One exclusive worker thread; progress is #dash-status text (plus the
        #dash-progress panel for fetch-all)."""
        if self.state.active_job is not None:
            self.app.notify(
                f"'{self.state.active_job}' is running — finish or cancel it first",
                severity="warning",
            )
            return
        if stage != "fetch-all":
            self._fetch_progress = None  # hide any stale fetch-all panel
            self._fetch_done = False
        self.state.active_job = stage
        self._job = {"stage": stage}
        self.query_one("#dash-status", Static).update(f"Running {stage}…")
        self.run_worker(
            partial(self._run_job, fn=fn, after=after),
            thread=True,
            group="stage",  # one job at a time across workspace/plagiarism too
            exclusive=True,
        )

    def _run_job(
        self, fn: Callable[[], None], after: Callable[[], None] | None
    ) -> None:  # worker thread
        start = time.monotonic()
        error: BaseException | None = None
        try:
            fn()
        except BaseException as exc:  # incl. SystemExit from main's interactive fallback
            error = exc
        self.app.call_from_thread(self._job_done, start, error, after)

    def _job_done(
        self,
        start: float,
        error: BaseException | None,
        after: Callable[[], None] | None,
    ) -> None:  # main thread
        stage = self._job["stage"] if self._job else None
        self.state.active_job = None
        self._job = None
        if error is not None:
            self.query_one("#dash-status", Static).update("Job failed")
            self.app.notify(f"Job failed: {error}", severity="error")
            return
        progress = self._fetch_progress
        fetch_all = stage == "fetch-all" and progress is not None
        if fetch_all:
            self._fetch_done = True  # before after(): rescan re-renders the panel
        if after is not None:
            after()
        if fetch_all and progress is not None:
            done = sum(1 for t in progress if t["state"] == "done")
            fails = len(progress) - done
            summary = f"Fetch complete: {done}/{len(progress)} ok"
            if fails:
                summary += f", {fails} failed"
            self.query_one("#dash-status", Static).update(summary)
            if fails:
                first_err = next(
                    (t["err"] for t in progress if t["state"] == "failed"),
                    "",
                )
                self.app.notify(
                    f"Fetch complete: {fails} failed — {first_err}",
                    severity="warning",
                )
            else:
                self.app.notify("Fetch complete", severity="information")
            return
        self.query_one("#dash-status", Static).update(
            f"Done in {time.monotonic() - start:.1f}s"
        )
        self.app.notify("Job complete", severity="information")


# ---------- import modals ----------


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
        return Canvas(self.state.env_state["base_url"], self.state.env_state["token"])

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
        self.query_one("#modal-canvas-course", Select).set_options(
            [(f"Error: {message}", -1)]
        )

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
            self.app.notify(
                f"Directory already exists: {dir_name}", severity="error"
            )
            return
        dest.mkdir(parents=True)
        (dest / "config.toml").write_text(
            f'[fetch]\ncourse_id = {course_id}\nmode = "attach"\n',
            encoding="utf-8",
        )
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
    """Import an assignment: Canvas assignment + out dir + fetch mode; fetch job after."""

    @override
    def compose(self) -> ComposeResult:
        with Vertical(classes="confirm-modal"):
            yield Static("[b]Import assignment from Canvas[/b]")
            yield Select(
                [("Loading…", -1)], id="modal-assignment", allow_blank=False
            )
            yield Input(placeholder="Output dir (default: assignment id)", id="modal-out")
            yield RadioSet(
                RadioButton("attach", id="mode-attach"),
                RadioButton("text", id="mode-text"),
                RadioButton("auto", value=True, id="mode-auto"),
                id="modal-mode",
            )
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
        self.query_one("#modal-assignment", Select).set_options(
            [(f"Error: {message}", -1)]
        )

    def _populate(self) -> None:
        select = self.query_one("#modal-assignment", Select)
        if not self._items:
            select.set_options([("No assignments found — check course", -1)])
            return
        select.set_options([(f"{aid} — {name}", aid) for aid, name in self._items])
        select.value = self._items[0][0]

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "modal-assignment" and event.value not in {None, -1}:
            self.query_one("#modal-out", Input).value = str(event.value)

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
        out = self.query_one("#modal-out", Input).value.strip() or str(aid)
        mode_set: RadioSet = self.query_one("#modal-mode", RadioSet)
        pressed = mode_set.pressed_button
        mode: Literal["attach", "text", "auto"] = (
            pressed.id.removeprefix("mode-")  # type: ignore[union-attr]
            if pressed is not None and pressed.id is not None
            else "auto"
        )
        course = self.state.current_course
        if course is None:
            self.app.notify("No course selected", severity="error")
            return
        course_dir = self.state.assignments_dir / course.dir_name
        if (course_dir / out).exists():
            self.app.notify(f"Directory already exists: {out}", severity="error")
            return
        self.dismiss((aid, out, mode))


class TataApp(App[None]):
    """TATA Workbench shell: Header + 3 work tabs + Footer."""

    TITLE = "TATA Workbench"
    CSS_PATH = "tata_app.tcss"
    BINDINGS: ClassVar = [
        Binding("q", "quit", "Quit"),
        Binding("?", "show_help_panel", "Keys"),
    ]

    def __init__(self, root_dir: Path | None = None) -> None:
        super().__init__()
        self.state = AppState(root_dir=root_dir or AppState().root_dir)
        self.state.env_state = _env_status(self.state.root_dir)

    @override
    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="shell-tabs"):
            with TabPane("Dashboard", id="tab-dashboard"):
                yield DashboardScreen(self.state)
            with TabPane("Plagiarism", id="tab-plagiarism"):
                yield PlagiarismScreen(self.state)
            with TabPane("Settings", id="tab-settings"):
                yield SettingsScreen(self.state)
        yield Footer()

    def on_mount(self) -> None:
        # SettingsScreen.on_mount focuses its ctx-select, which makes the
        # TabbedContent activate the hidden settings pane (and drop focus).
        # After mount settles, re-activate the Dashboard tab and give the
        # table focus so the dashboard keys work immediately.
        def _restore() -> None:
            with suppress(Exception):
                self.switch_tab("tab-dashboard")
                self.query_one(DashboardScreen)._refocus()

        self.call_after_refresh(_restore)

    def switch_tab(self, name: str) -> None:
        """Activate a TabPane by id (tab-dashboard / tab-plagiarism / tab-settings)."""
        # Blur the current pane's focused widget BEFORE activating: Textual's
        # TabbedContent._on_tab_pane_focused re-activates the old pane when a
        # Focus event from a widget inside it lands after .active is set —
        # that made the global-layer `g` key a silent no-op.
        self.set_focus(None)
        self.query_one("#shell-tabs", TabbedContent).active = name
        # Seat focus inside the pane that is now visible; helpers are no-ops
        # when their pane is hidden (is_displayed guard).
        if name == "tab-dashboard":
            self.query_one(DashboardScreen)._refocus()
        elif name == "tab-plagiarism":
            self.query_one(PlagiarismScreen)._focus_active_table()
        elif name == "tab-settings":
            settings = self.query_one(SettingsScreen)
            with suppress(Exception):
                settings.query_one("#ctx-select", Select).focus()

    def _derive_ctx(self) -> str:
        """Settings context matching the current dashboard level."""
        state = self.state
        if state.dashboard_level == "assignment" and state.current_assignment is not None:
            return "assignment"
        if state.dashboard_level == "course" and state.current_course is not None:
            return "course"
        return "global"

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        """Refresh the pane that just became visible. Fires on mount too —
        guard so the initial activation of tab-dashboard is a no-op."""
        pane_id = event.pane.id
        if pane_id == "tab-settings":
            settings = self.query_one(SettingsScreen)
            with suppress(Exception):
                settings.set_context(self._derive_ctx())  # may fire before mount
        elif pane_id == "tab-plagiarism":
            plag = self.query_one(PlagiarismScreen)
            with suppress(Exception):
                plag.reload_all()  # may fire before mount


def run() -> None:
    TataApp().run()


if __name__ == "__main__":
    run()
