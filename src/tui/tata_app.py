"""TATA Workbench — Textual TUI platform shell (T4a).

Four-tab shell (Dashboard / Plagiarism / Library / Settings) with the S1
three-level
dashboard (Global -> Course -> Assignment placeholder) on top of
:mod:`src.tui.tata_scan`. All UI copy is English.

Run: ``uv run python src/tata_app.py``
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, MutableMapping
from contextlib import suppress
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import ClassVar, override

import dotenv
import tomlkit
from canvasapi import Canvas
from rich.markup import escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.dom import NoMatches
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    HelpPanel,
    Input,
    Select,
    Static,
    TabbedContent,
    TabPane,
)
from textual_serve.server import Server

from src import cli as main_mod
from src.shared.aliases import (
    assignment_display_name,
    course_display_name,
    load_alias_file,
    seed_assignment_alias,
    seed_course_alias,
    set_alias,
)
from src.shared.assignment_config import FetchSection
from src.shared.canvas_fetch import list_assignments, list_courses
from src.shared.cli_options import FetchCliOptions
from src.shared.config_edit import edit_config
from src.shared.provider import get_providers
from src.tui.score_review import open_score_review
from src.tui.tata_library import LibraryScreen
from src.tui.tata_plagiarism import PlagiarismScreen, run_aggregate_job
from src.tui.tata_scan import (
    AssignmentInfo,
    CourseInfo,
    _plagiarism_threshold_pct,
    scan_assignments,
    scan_courses,
)
from src.tui.tata_settings import SettingsScreen
from src.tui.tata_workspace import (
    AssignmentScreen,
    ConfirmationModal,
    fmt_last_run,
    fmt_state,
    is_displayed,
    state_key,
)


def _env_status(root_dir: Path) -> dict:
    """Probe for ``.env`` (CANVAS_BASE_URL/CANVAS_ACCESS_TOKEN) without exiting.

    Mirrors :func:`src.shared.canvas_fetch.load_env`: walk root_dir then its
    ancestors; a .env missing either key is skipped, keep walking up.
    """
    for d in [root_dir, *root_dir.parents]:
        env_path = d / ".env"
        if not env_path.is_file():
            continue
        try:
            vals = dotenv.dotenv_values(env_path, interpolate=False)
        except UnicodeDecodeError:
            continue
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
        # Single display-threshold source: the course's [plagiarism] section
        # (default 0.8 -> 80%), shared with the Plagiarism pane — the
        # dashboard flags must never disagree with the pane. Tolerant helper
        # (M1): malformed course config falls back to the default, never
        # crashes TUI startup.
        threshold_pct = _plagiarism_threshold_pct(course.config_path)
        self.assignments = scan_assignments(
            self.assignments_dir / course.dir_name,
            threshold_pct=threshold_pct,
        )


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
    return [a for a in assignments if state_key(a) == flt]


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
        # Course level: `a` = Aliases. No priority — at assignment level the
        # workspace (AssignmentScreen) owns `a` = Analyze (T4b design); the
        # assignment alias editor opens from the workspace's Aliases button.
        Binding("a", "edit_aliases", "Aliases"),
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
            # ponytail: one shared header cannot reflect per-course
            # thresholds when they differ — show the first (sorted) course's;
            # per-course labels would need the threshold in the row instead.
            thr = _plagiarism_threshold_pct(
                state.courses[0].config_path if state.courses else None
            )
            table.add_columns(
                "Course",
                "Assignments",
                "Raw",
                "Proc",
                "Grad",
                "Avg score",
                f">{thr:g}% pairs",
                "Last run",
            )
            for i, c in enumerate(state.courses):
                table.add_row(
                    escape(
                        course_display_name(
                            state.assignments_dir, c.dir_name, c.course_id
                        )
                    ),
                    str(c.assignment_count),
                    str(c.counts.raw),
                    str(c.counts.processed),
                    str(c.counts.graded),
                    _fmt_score(c.score_mean),
                    str(c.flagged_pairs),
                    fmt_last_run(c.last_run),
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
                    fmt_state(a),
                    fmt_last_run(a.last_run),
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
        if not is_displayed(self):
            return  # dashboard tab hidden — never steal focus (F2)
        table = self.query_one("#dashboard-table", DataTable)
        if is_displayed(table):
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
        match value:
            case (aid, name) if isinstance(aid, int):
                pass
            case _:
                return
        course = self.state.current_course
        if course is None or course.course_id is None:
            return
        self.app.push_screen(
            AssignmentSetupModal(self.state),
            callback=partial(self._on_assignment_setup, aid, name),
        )

    def _on_assignment_setup(self, aid: int, name: str | None, value: object) -> None:
        if not isinstance(value, dict):
            return  # setup cancelled
        course = self.state.current_course
        if course is None or course.course_id is None:
            return
        config_dir = self.state.assignments_dir / course.dir_name / str(aid)
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.toml"
        config_path.write_text(
            "# schema: ../../config/assignment.schema.json\n", encoding="utf-8"
        )
        edit_config(config_path, {"grading": value})
        if name:
            seed_assignment_alias(
                self.state.assignments_dir / course.dir_name, aid, name
            )
        self._start_job(
            "fetch",
            partial(self._fetch_one, course, aid),
            after=self._rescan_course,
        )

    # ---------- actions: aliases ----------

    def action_edit_aliases(self) -> None:
        """Open AliasEditorModal for the course alias table (course-level `a`).

        Edits the global ``data/alias.toml`` ``[course]`` table — the same
        file ``seed_course_alias`` writes. Assignment-level aliases are
        edited from the workspace's Aliases button (which passes
        ``<course>/alias.toml`` + ``[assignment]``).
        """
        state = self.state
        if state.dashboard_level != "course":
            self.app.notify("Select a course above to edit aliases", severity="warning")
            return
        course = state.current_course
        if course is None or course.course_id is None:
            self.app.notify("Current course has no course_id", severity="error")
            return
        modal = AliasEditorModal(
            state.assignments_dir / "alias.toml",
            "course",
            "Course aliases",
        )
        self.app.push_screen(modal, callback=self._on_aliases_saved)

    def _on_aliases_saved(self, value: object) -> None:
        if value:
            self.render_level()  # display names may have changed

    @staticmethod
    def _fetch_one(course: CourseInfo, aid: int) -> None:
        main_mod._run_fetch(
            FetchCliOptions(
                course=course.course_id,
                assignment=aid,
                config=course.config_path,
            )
        )
        # M3: record the assignment in the course config's [[fetch.assignments]]
        # so fetch-all (F) picks it up later. CLI's _remember does not maintain
        # that list; a plain append lands in [fetch] (TOML table headers are
        # absolute). Dedup on id (legacy assignment_id key also accepted);
        # skip when the config is unreadable — the fetch already succeeded.
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
            isinstance(e, dict)
            and (e.get("id") == aid or e.get("assignment_id") == aid)
            for e in entries
        ):
            return
        entry: dict[str, object] = {"id": aid}
        if isinstance(fetch, MutableMapping):
            if "assignments" not in fetch:
                fetch["assignments"] = tomlkit.aot()
            fetch["assignments"].append(entry)
        else:
            doc["fetch"] = {"assignments": tomlkit.aot()}
            doc["fetch"]["assignments"].append(entry)
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
        """Course [fetch] section via ``src/cli.py``'s loader (empty list -> None).

        Mirrors the CLI's root-config model exactly: ``[[fetch.assignments]]``
        entries carry ``id`` only (fetch auto-collects all submission types);
        the fetch output dir is derived (``<course dir>/<id>/raw``), not
        stored.
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
            # Alias-aware label; the assignment id names the dir (e.g.
            # '2978557') — raw paths are never shown.
            label = assignment_display_name(
                self.state.assignments_dir,
                course.dir_name,
                str(entry.id),
                entry.id,
            )
            targets.append({
                "label": label,
                "state": "pending",
                "err": "",
                "seconds": 0.0,
            })
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
                            assignment=entry.id,
                            config=config_path,
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
                lines.append(f"[green]✓ {label} ({target['seconds']:.1f}s)[/green]")
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
            done = sum(1 for t in self._fetch_progress if t["state"] != "pending")
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
            run_aggregate_job(course.config_path)

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
        open_score_review(self.app, item.config_path.parent)  # type: ignore[attr-defined]

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
        self,
        stage: str,
        fn: Callable[[], None],
        after: Callable[[], None] | None = None,
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
        except (
            BaseException
        ) as exc:  # incl. SystemExit from main's interactive fallback
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
    provider registry (config/provider.toml) synchronously. Import dismisses
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
                "No providers configured — add [providers] to config/provider.toml."
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
    """Edit one alias.toml table: rows of key (read-only) -> name (editable
    Input), plus one add row. Save writes every row through
    :func:`src.shared.aliases.set_alias` (an empty name deletes the key); esc
    cancels without writing. Dismisses True on save, None on cancel.

    Course level passes the global ``data/alias.toml`` + ``[course]``;
    assignment level ``<course>/alias.toml`` + ``[assignment]`` — the same
    files the seed functions write.
    """

    BINDINGS: ClassVar = [Binding("escape", "close", "Close", show=False)]

    def __init__(self, alias_path: Path, section: str, title: str) -> None:
        super().__init__()
        self.alias_path = alias_path
        self.section = section
        self._title = title
        self._rows: list[tuple[str, Input]] = []

    @override
    def compose(self) -> ComposeResult:
        with Vertical(classes="confirm-modal"):
            yield Static(f"[b]{self._title}[/b]")
            yield Static(f"No {self.section} aliases yet.", id="alias-empty")
            with Vertical(id="alias-rows"):
                for key, name in sorted(
                    load_alias_file(self.alias_path).get(self.section, {}).items()
                ):
                    with Horizontal(classes="alias-row"):
                        yield Static(key, classes="alias-key")
                        entry = Input(value=name)
                        self._rows.append((key, entry))
                        yield entry
            with Horizontal(classes="alias-row"):  # add row
                yield Input(placeholder="Add key (id)", id="alias-new-key")
                yield Input(placeholder="Add name", id="alias-new-name")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="primary")

    @override
    def on_mount(self) -> None:
        self.query_one("#alias-empty", Static).display = not self._rows

    def action_close(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "save":
            self._do_save()

    def _do_save(self) -> None:
        entries = [(key, name.value.strip()) for key, name in self._rows]
        new_key = self.query_one("#alias-new-key", Input).value.strip()
        if new_key:
            entries.append((
                new_key,
                self.query_one("#alias-new-name", Input).value.strip(),
            ))
        try:
            for key, name in entries:
                set_alias(self.alias_path, self.section, key, name)
        except ValueError as exc:
            self.app.notify(str(exc), severity="error")
            return
        self.app.notify("Aliases saved", severity="information")
        self.dismiss(True)


class TataApp(App[None]):
    """TATA Workbench shell: Header + 4 work tabs + Footer."""

    TITLE = "TATA Workbench"
    CSS_PATH = "tata_app.tcss"
    BINDINGS: ClassVar = [
        Binding("q", "quit", "Quit"),
        Binding("?", "toggle_help", "Keys"),
    ]

    def __init__(self, root_dir: Path | None = None) -> None:
        super().__init__()
        self.state = AppState(root_dir=root_dir or AppState().root_dir)
        self.state.env_state = _env_status(self.state.root_dir)

    def action_toggle_help(self) -> None:
        """Toggle the native keys panel ('?': built-in show/hide wrapped)."""
        try:
            self.screen.query_one(HelpPanel)
        except NoMatches:
            self.action_show_help_panel()
        else:
            self.action_hide_help_panel()

    @override
    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="shell-tabs"):
            with TabPane("Dashboard", id="tab-dashboard"):
                yield DashboardScreen(self.state)
            with TabPane("Plagiarism", id="tab-plagiarism"):
                yield PlagiarismScreen(self.state)
            with TabPane("Library", id="tab-library"):
                yield LibraryScreen(self.state)
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
        """Activate a TabPane by id (tab-dashboard / tab-plagiarism / tab-library / tab-settings)."""
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
        elif name == "tab-library":
            self.query_one(LibraryScreen)._focus_default()
        elif name == "tab-settings":
            settings = self.query_one(SettingsScreen)
            with suppress(Exception):
                settings.query_one("#ctx-select", Select).focus()

    def _derive_ctx(self) -> str:
        """Settings context matching the current dashboard level."""
        state = self.state
        if (
            state.dashboard_level == "assignment"
            and state.current_assignment is not None
        ):
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
        elif pane_id == "tab-library":
            library = self.query_one(LibraryScreen)
            with suppress(Exception):
                library.reload_files()  # may fire before mount


def run() -> None:
    """Entry for the ``tui`` script; ``--web`` serves it over HTTP (textual-serve)."""
    if "--web" in sys.argv[1:]:
        Server("uv run tui").serve()
        return
    TataApp().run()


if __name__ == "__main__":
    run()
