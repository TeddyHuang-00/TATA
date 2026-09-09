"""TATA Workbench — Textual TUI platform shell (T4a).

Three-tab shell (Dashboard / Library / Settings); the S4 plagiarism workspace
is embedded in the course-level dashboard (lower half of the screen). All UI
copy is English.

Run: ``uv run python src/tui/app.py``
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, MutableMapping
from contextlib import suppress
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import partial
from pathlib import Path
from typing import ClassVar, cast, override

import tomlkit
from rich.markup import escape
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.dom import NoMatches
from textual.widgets import (
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

from src import REPO_ROOT
from src.shared.aliases import (
    assignment_display_name,
    course_display_name,
    seed_assignment_alias,
)
from src.shared.assignment_config import FetchSection
from src.shared.canvas_fetch import read_env_state
from src.shared.cli_options import FetchCliOptions
from src.shared.config_edit import edit_config
from src.shared.fetch_pipeline import root_fetch, run_fetch
from src.tui.library import LibraryScreen
from src.tui.modals import (
    AliasEditorModal,
    AssignmentSetupModal,
    ImportAssignmentModal,
    ImportCourseModal,
)
from src.tui.plagiarism import PlagiarismScreen, run_aggregate_job
from src.tui.scan import (
    AssignmentInfo,
    CourseInfo,
    plagiarism_threshold_pct,
    scan_assignments,
    scan_courses,
)
from src.tui.score_review import open_score_review
from src.tui.settings import SettingsScreen
from src.tui.workspace import (
    AssignmentScreen,
    ConfirmationModal,
    fmt_last_run,
    fmt_state,
    is_displayed,
    state_key,
)


@dataclass
class AppState:
    """Shared platform state (design 00 §5)."""

    root_dir: Path = field(default_factory=lambda: REPO_ROOT)
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
        threshold_pct = plagiarism_threshold_pct(course.config_path)
        self.assignments = scan_assignments(
            self.assignments_dir / course.dir_name,
            threshold_pct=threshold_pct,
        )


def _fmt_score(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else "-"


# State filters (design 01 §5: 1=All 2=Done 3=Partial 4=Not run). The
# 'flagged' filter was removed (feedback 5): plagiarism flags live in the
# plagiarism pane only.
_FILTER_LABELS: dict[str | None, str] = {
    None: "All",
    "done": "Done",
    "partial": "Partial",
    "not_run": "Not run",
}


def _filter_assignments(
    assignments: list[AssignmentInfo], flt: str | None
) -> list[AssignmentInfo]:
    if flt is None:
        return assignments
    return [a for a in assignments if state_key(a) == flt]


_FUZZY_RATIO = 0.55  # fuzzy search threshold (difflib) for whole-ratio fallback


def fuzzy_match(query: str, text: str) -> bool:
    """Search-match: case-insensitive substring or close ratio (stdlib)."""
    q, s = query.lower().strip(), text.lower()
    if not q:
        return True
    if q in s:
        return True
    return SequenceMatcher(None, q, s).ratio() >= _FUZZY_RATIO


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
        # `a` = Aliases for the selected item: global level edits the
        # course's alias, course level the assignment's. No priority — at
        # assignment level the workspace (AssignmentScreen) owns `a` = Analyze.
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
    ]

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self._rows: list[object] = []
        # Last selected row per dashboard level (dir_name); DataTable rebuilds
        # in render_level drop the cursor, so we re-seat it after re-render.
        self._last_dir: dict[str, str] = {}
        self._filter: str | None = None  # course-level state filter
        self._search = ""  # live search text (#search-input)
        # Last header-click (column index, desc); None = default name asc.
        self._sort: tuple[int, bool] | None = None
        self._job: dict | None = None  # minimal job protocol (see _start_job)
        # Per-target state of the last/current fetch-all (F) run; None = panel
        # hidden. Each entry: {"label", "state", "err", "seconds"}.
        self._fetch_progress: list[dict] | None = None
        self._fetch_done = False  # fetch-all completed; panel stays visible

    @override
    def compose(self) -> ComposeResult:
        yield Static(id="topbar", markup=True)
        yield Static(id="breadcrumb", markup=True)
        yield Input(placeholder="Search…", id="search-input")
        yield DataTable(id="dashboard-table", cursor_type="row", zebra_stripes=True)
        yield _FocusableStatic(id="dash-empty", markup=True)
        yield AssignmentScreen(self.state)
        # S4 embed: the plagiarism workspace lives at course level, under the
        # assignment table; visible there only (see render_level).
        self._plag = PlagiarismScreen(self.state)
        yield self._plag
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
        # Embed split: at course level the table becomes the upper half and
        # the plagiarism pane takes the rest; elsewhere the table fills.
        table.styles.height = "40%" if state.dashboard_level == "course" else "1fr"

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
                "Last run",
            )
            items = self._visible_courses()
            for i, c in enumerate(items):
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
                    fmt_last_run(c.last_run),
                    key=str(i),
                )
                self._rows.append(c)
            self._show_empty(
                empty,
                "No courses match the search."
                if self._search
                else "No courses yet. Press `c` to import (configure .env first).",
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
            shown = self._visible_assignments(course)
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
                "No assignments match the filter/search."
                if self._filter is not None or self._search
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

        # Search strip lives at the list levels (global/course) only.
        self.query_one("#search-input", Input).display = (
            state.dashboard_level != "assignment"
        )
        # Fetch-all progress panel: live during the run and after completion;
        # hidden on any level/navigation change (course level only). A new
        # non-fetch-all job also clears _fetch_progress in _start_job.
        self.query_one("#dash-progress", Static).display = (
            state.dashboard_level == "course"
            and self._fetch_progress is not None
            and (self.state.active_job == "fetch-all" or self._fetch_done)
        )
        # Embedded plagiarism pane: course level only. reload_all() re-reads
        # the course's pairs/aggregate JSON — navigation entry, rescan and
        # the p-key job's after() all funnel through this single call.
        self._plag.display = state.dashboard_level == "course"
        if self._plag.display:
            self._plag.reload_all()
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
        search = self.query_one("#search-input", Input)
        if search.display and search.has_focus:
            return  # user is typing in live search — never steal focus (MAJOR-D)
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

    # ---------- search + sort (feedback 5 Item 4) ----------

    def _visible_courses(self) -> list[CourseInfo]:
        """Courses filtered by the live search, in default/header sort order."""
        state = self.state
        items = [
            c
            for c in state.courses
            if fuzzy_match(
                self._search,
                course_display_name(state.assignments_dir, c.dir_name, c.course_id),
            )
        ]
        self._sort_rows(items)
        return items

    def _visible_assignments(self, course: CourseInfo) -> list[AssignmentInfo]:
        """Assignments for the current state filter + search, in sort order."""
        state = self.state
        shown = _filter_assignments(state.assignments, self._filter)
        shown = [
            a
            for a in shown
            if fuzzy_match(
                self._search,
                assignment_display_name(
                    state.assignments_dir,
                    course.dir_name,
                    a.dir_name,
                    a.assignment_id,
                ),
            )
        ]
        self._sort_rows(shown)
        return shown

    def _sort_rows(self, items: list[CourseInfo] | list[AssignmentInfo]) -> None:
        """Default display-name asc; header-clicked sort (col, desc) overrides."""
        level = self.state.dashboard_level
        if self._sort is None:
            items.sort(key=lambda item: self._sort_name(item, level).lower())
        else:
            col, desc = self._sort
            items.sort(
                key=lambda item: self._sort_value(item, level, col), reverse=desc
            )

    def _sort_name(self, item: CourseInfo | AssignmentInfo, level: str) -> str:
        state = self.state
        if level == "global":
            c = cast(CourseInfo, item)
            return course_display_name(state.assignments_dir, c.dir_name, c.course_id)
        a = cast(AssignmentInfo, item)
        course_name = state.current_course.dir_name if state.current_course else ""
        return assignment_display_name(
            state.assignments_dir,
            course_name,
            a.dir_name,
            a.assignment_id,
        )

    def _sort_value(
        self, item: CourseInfo | AssignmentInfo, level: str, col: int
    ) -> str | int | float:
        if col == 0:  # name column: use the display name itself
            return self._sort_name(item, level)
        if level == "global":
            c = cast(CourseInfo, item)
            values: dict[int, str | int | float] = {
                1: c.assignment_count,
                2: c.counts.raw,
                3: c.counts.processed,
                4: c.counts.graded,
                5: c.score_mean if c.score_mean is not None else -1.0,
                6: c.last_run if c.last_run is not None else -1.0,
            }
            return values.get(col, 0)
        a = cast(AssignmentInfo, item)
        values: dict[int, str | int | float] = {
            1: str(a.assignment_id or ""),
            2: a.counts.raw,
            3: a.counts.processed,
            4: a.counts.graded,
            5: a.score_summary if a.score_summary is not None else -1.0,
            6: state_key(a),
            7: a.last_run if a.last_run is not None else -1.0,
        }
        return values.get(col, 0)

    @on(Input.Changed)
    def _on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "search-input":
            return
        self._search = event.value
        self.render_level()

    @on(DataTable.HeaderSelected)
    def _on_header_selected(self, event: DataTable.HeaderSelected) -> None:
        if event.data_table.id != "dashboard-table":
            return
        col = event.column_index
        self._sort = (
            (col, not self._sort[1])
            if self._sort is not None and self._sort[0] == col
            else (col, False)
        )
        self.render_level()

    # ---------- navigation ----------

    def _selected(self) -> object | None:
        idx = self.query_one("#dashboard-table", DataTable).cursor_row
        if 0 <= idx < len(self._rows):
            return self._rows[idx]
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "dashboard-table":
            return  # plagiarism pane tables handle their own selection
        self.action_go_down()

    def action_go_down(self) -> None:
        state = self.state
        if state.dashboard_level == "assignment":
            return  # T4b: assignment workspace comes later
        item = self._selected()
        if item is None:
            return
        self._remember_selection()
        self._sort = None  # level change resets sort to default name asc
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
        self._sort = None  # level change resets sort to default name asc
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
        """Open AliasEditorModal for the selected item's single alias.

        Global level edits the selected course's ``[course]`` entry in
        ``data/alias.toml``; course level the selected assignment's
        ``[assignment]`` entry in ``<course>/alias.toml``. At the assignment
        level ``a`` belongs to the workspace (Analyze) and does nothing here.
        """
        state = self.state
        item = self._selected()
        if item is None:
            return
        if state.dashboard_level == "global":
            assert isinstance(item, CourseInfo)  # global rows are courses
            if item.course_id is None:
                self.app.notify("Selected course has no course_id", severity="error")
                return
            modal = AliasEditorModal(
                state.assignments_dir / "alias.toml",
                "course",
                str(item.course_id),
                "Course alias",
            )
        elif state.dashboard_level == "course":
            assert isinstance(item, AssignmentInfo)  # course rows are assignments
            key = (
                str(item.assignment_id)
                if item.assignment_id is not None
                else item.dir_name
            )
            course = state.current_course
            if course is None:
                return
            modal = AliasEditorModal(
                state.assignments_dir / course.dir_name / "alias.toml",
                "assignment",
                key,
                "Assignment alias",
            )
        else:
            return  # assignment level: the workspace owns `a` (Analyze)
        self.app.push_screen(modal, callback=self._on_aliases_saved)

    def _on_aliases_saved(self, value: object) -> None:
        if value:
            self.render_level()  # display names may have changed

    @staticmethod
    def _fetch_one(course: CourseInfo, aid: int) -> None:
        run_fetch(
            FetchCliOptions(
                course=course.course_id,
                assignment=aid,
                config=course.config_path,
            )
        )
        # M3: record the assignment in the course config's [[fetch.assignments]]
        # so fetch-all (F) picks it up later. fetch_pipeline.remember does not
        # maintain
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
            cfg = root_fetch(course.config_path)
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
            # '987654') — raw paths are never shown.
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
                    run_fetch(
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
            # render_level (via _rescan_course) reloads the embedded pane
            self._rescan_course()

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


class TataApp(App[None]):
    """TATA Workbench shell: Header + 3 work tabs + Footer."""

    TITLE = "TATA Workbench"
    CSS_PATH = "styles/app.tcss"
    BINDINGS: ClassVar = [
        Binding("q", "quit", "Quit"),
        Binding("?", "toggle_help", "Keys"),
    ]

    def __init__(self, root_dir: Path | None = None) -> None:
        super().__init__()
        self.state = AppState(root_dir=root_dir or AppState().root_dir)
        self.state.env_state = read_env_state(self.state.root_dir)

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
            with TabPane("Library", id="tab-library"):
                yield LibraryScreen(self.state)
            with TabPane("Settings", id="tab-settings"):
                yield SettingsScreen(self.state)
        yield Footer()

    def on_mount(self) -> None:
        # SettingsScreen.on_mount focuses its ctx-select, which makes the
        # TabbedContent activate the hidden settings pane (and drop focus).
        # After mount settles, re-activate the Dashboard tab and give the
        # table focus so the dashboard keys work immediately. Only switch
        # when needed: switch_tab blurs first (set_focus(None)) to dodge
        # Textual's TabPane.Focused re-activation, and an unconditional
        # blur mid-startup would eat keys a user/tests press right after
        # mount (settings seed is now display-guarded, so normally the
        # dashboard tab never left).
        def _restore() -> None:
            with suppress(Exception):
                tabs = self.query_one("#shell-tabs", TabbedContent)
                if tabs.active != "tab-dashboard":
                    self.switch_tab("tab-dashboard")
                self.query_one(DashboardScreen)._refocus()

        self.call_after_refresh(_restore)

    def switch_tab(self, name: str) -> None:
        """Activate a TabPane by id (tab-dashboard / tab-library / tab-settings)."""
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
        the dashboard branch is idempotent (DashboardScreen.on_mount renders
        again after mount); the settings/library branches are guarded against
        pre-mount exceptions."""
        pane_id = event.pane.id
        if pane_id == "tab-dashboard":
            dashboard = self.query_one(DashboardScreen)
            with suppress(Exception):
                state = self.state
                if state.current_course is not None:
                    state.load_assignments(state.current_course)
                    # load_assignments replaces the list: keep
                    # current_assignment pointing at the fresh object (same
                    # pattern as workspace._rescan_after_job) so the
                    # workspace renders current counts/config.
                    current = state.current_assignment
                    if current is not None:
                        fresh = {a.dir_name: a for a in state.assignments}
                        state.current_assignment = fresh.get(current.dir_name, current)
                dashboard.render_level()
        elif pane_id == "tab-settings":
            settings = self.query_one(SettingsScreen)
            with suppress(Exception):
                # ponytail: force reload on tab activation; unsaved Settings
                # edits are dropped (matches Ctrl+S workflow)
                if (
                    settings._ctx_manual
                    and settings._ctx in settings.available_contexts()
                ):
                    # keep the user's manual context pick (re-armed so it
                    # survives repeated switch-away-and-back)
                    settings.set_context(settings._ctx, force=True, manual=True)
                else:
                    # manual pick went stale (e.g. assignment level left for
                    # course): clear the flag so set_context won't swallow it
                    # again, then fall back to the dashboard-derived context
                    settings._ctx_manual = False
                    settings.set_context(self._derive_ctx(), force=True)
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
