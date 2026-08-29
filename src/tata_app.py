"""TATA Workbench — Textual TUI platform shell (T4a).

Three-tab shell (Dashboard / Plagiarism / Settings) with the S1 three-level
dashboard (Global -> Course -> Assignment placeholder) on top of
:mod:`src.tata_scan`. All UI copy is English.

Run: ``uv run python src/tata_app.py``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, override

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane

from src.tata_scan import AssignmentInfo, CourseInfo, scan_assignments, scan_courses


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
            return {"has_env": True, "base_url": vals["CANVAS_BASE_URL"], "token_set": True}
    return {"has_env": False, "base_url": None, "token_set": False}


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
    jobs: dict = field(default_factory=dict)
    env_state: dict = field(default_factory=dict)

    @property
    def assignments_dir(self) -> Path:
        return self.root_dir / "assignments"

    def refresh_courses(self) -> None:
        self.courses = scan_courses(self.assignments_dir)

    def load_assignments(self, course: CourseInfo) -> None:
        self.assignments = scan_assignments(self.assignments_dir / course.dir_name)


def _fmt_score(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else "-"


def _fmt_last_run(ts: float | None) -> str:
    if ts is None:
        return "Never"
    dt = datetime.fromtimestamp(ts, tz=UTC).astimezone()
    now = datetime.now(tz=UTC).astimezone()
    if dt.date() == now.date():
        return f"Today {dt:%H:%M}"
    return f"{dt:%Y-%m-%d %H:%M}"


# State vocabulary (design 99 §2). "Flagged" is a display-level marker: it
# fires on flagged_pairs (max_similarity_pct >= DISPLAY_THRESHOLD_PCT), NOT on
# aggregate z-score flags (S4) — the TUI does not consume the aggregate report.
_STATE_LABELS = {
    "not_run": "Not run",
    "partial": "Partial",
    "done": "Done",
    "flagged": "Flagged",
    "error": "Error",
    "unknown": "? Unknown",
}


def _fmt_state(a: AssignmentInfo) -> str:
    """Counts-based pipeline state; all outputs come from ``_STATE_LABELS``."""
    if a.flagged_pairs:
        return f"{_STATE_LABELS['flagged']} ({a.flagged_pairs})"
    if a.counts.raw == 0:
        return _STATE_LABELS["not_run"]
    if (
        a.counts.processed < a.counts.raw
        or a.counts.graded < a.counts.processed
        or a.counts.scored == 0
    ):
        return _STATE_LABELS["partial"]
    return _STATE_LABELS["done"]


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
    ]

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self._rows: list[object] = []
        # Last selected row per dashboard level (dir_name); DataTable rebuilds
        # in render_level drop the cursor, so we re-seat it after re-render.
        self._last_dir: dict[str, str] = {}

    @override
    def compose(self) -> ComposeResult:
        yield Static(id="topbar", markup=True)
        yield Static(id="breadcrumb", markup=True)
        yield DataTable(id="dashboard-table", cursor_type="row", zebra_stripes=True)
        yield _FocusableStatic(id="dash-empty", markup=True)
        yield _FocusableStatic(id="workspace", markup=True)
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
        workspace = self.query_one("#workspace", Static)
        status = self.query_one("#dash-status", Static)

        table.clear(columns=True)
        self._rows = []
        canvas = (
            "Canvas: OK" if state.env_state.get("has_env") else "Canvas: ? (.env missing)"
        )

        if state.dashboard_level == "global":
            topbar.update(
                f"[b]TATA[/b] · Dashboard [Global]   {canvas}   "
                f"Courses: {len(state.courses)}   "
                "enter=Course view  r=Rescan  q=Quit"
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
                    c.dir_name,
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
            )
        elif state.dashboard_level == "course":
            topbar.update(
                f"[b]TATA[/b] · Dashboard [Course: {state.current_course.dir_name}] "
                f"   {canvas}   enter=Assignment  esc=Global  r=Rescan  q=Quit"
            )
            breadcrumb.update(
                f"Global / [b]{state.current_course.dir_name}[/b]"
            )
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
            for i, a in enumerate(state.assignments):
                table.add_row(
                    a.dir_name,
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
            self._show_empty(empty, "No assignments in this course yet.", table)
        else:  # assignment — T4b placeholder
            topbar.update(
                f"[b]TATA[/b] · Dashboard [Assignment: {state.current_assignment.dir_name}]"
                f"   {canvas}   esc=Course view  q=Quit"
            )
            breadcrumb.update(
                f"Global / {state.current_course.dir_name} / "
                f"[b]{state.current_assignment.dir_name}[/b]"
            )
            table.display = False
            empty.display = False
            workspace.display = True
            workspace.update("Assignment workspace (T4b)")

        status.update(
            "enter=Drill down   esc/backspace=Up one level   r=Rescan   q=Quit"
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
        table = self.query_one("#dashboard-table", DataTable)
        if table.display:
            table.focus()
        elif self.state.dashboard_level == "assignment":
            self.query_one("#workspace").focus()
        else:
            self.query_one("#dash-empty").focus()

    def _show_empty(self, empty: Static, text: str, table: DataTable) -> None:
        workspace = self.query_one("#workspace", Static)
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


class TataApp(App[None]):
    """TATA Workbench shell: Header + 3 work tabs + Footer."""

    TITLE = "TATA Workbench"
    CSS_PATH = "tata_app.tcss"
    BINDINGS: ClassVar = [Binding("q", "quit", "Quit")]

    def __init__(self, root_dir: Path | None = None) -> None:
        super().__init__()
        self.state = AppState(root_dir=root_dir or AppState().root_dir)
        self.state.env_state = _env_status(self.state.root_dir)

    @override
    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent():
            with TabPane("Dashboard"):
                yield DashboardScreen(self.state)
            with TabPane("Plagiarism"):
                yield Static(
                    "Plagiarism workspace (coming soon)", id="plagiarism-placeholder"
                )
            with TabPane("Settings"):
                yield Static("Settings (coming soon)", id="settings-placeholder")
        yield Footer()


def run() -> None:
    TataApp().run()


if __name__ == "__main__":
    run()
