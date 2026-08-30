"""S4 Plagiarism screen (T2): course-scoped tabs + embedded compare pane.

Four tabs (``TabbedContent``, aggregate first): ``#pane-aggregate`` —
course-level z-score table; ``#pane-assignments`` — per-assignment ranking;
``#pane-students`` — per-student ranking; ``#pane-pairs`` — per-pair ranking
with an embedded ``#cmp-pane`` side-by-side compare (no pushed modal; the
pane updates live on row highlight).  All UI copy is English (design 04
v1.1).

Jobs reuse the S2 JobHandle protocol (design 99 §3.1), shared with the
workspace via :class:`src.tata_jobs.JobHost`: the worker thread runs
:func:`src.plagiarism.detect_plagiarism` (quiet=True — the panes read
the JSON, not the text report) with stdout redirected into a log queue; the
main thread drains it into the RichLog.  ``[p]`` detects the current
assignment, ``[a]`` runs the course aggregate.

Data sources (course-scoped; no dependence on ``state.current_assignment``):
- pairs: ``<assignment>/plagiarism/all_pairs.json`` per assignment in
  ``state.assignments`` (values may be strings; every float conversion is
  tolerant)
- aggregate: ``<course>/plagiarism/aggregate.json`` — ``detect_plagiarism``
  only prints the text report, so the [a] worker additionally writes this
  JSON; the pane reads it.  Rows are built with ``aggregate_pair_rows`` —
  the same public entry point the CLI report assembly uses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, override

from rich.markup import escape
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    DataTable,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from src.aliases import (
    assignment_display_name,
    course_display_name,
    course_student_display_name,
    student_display_name,
)
from src.plagiarism import detect_plagiarism, root_plagiarism_section
from src.plagiarism_aggregate import aggregate_pair_rows
from src.score_review import base_uid, find_raw_file, preview_content
from src.tata_jobs import JobHost
from src.tata_scan import (
    DISPLAY_THRESHOLD_PCT as DEFAULT_DISPLAY_THRESHOLD_PCT,
    AssignmentInfo,
    _pair_pct,
    _plagiarism_threshold_pct,
)
from src.tata_workspace import is_displayed

if TYPE_CHECKING:
    from src.tata_app import AppState

PAGE_ROWS = 20
SIDE_MAX_LINES = 300
AGG_ALPHA_FALLBACK = 0.01
AGG_JSON_NAME = "aggregate.json"
Z_WATCH_THRESHOLD = 3.0

_PANE_ORDER = ["pane-aggregate", "pane-assignments", "pane-students", "pane-pairs"]

_FLAG_TEXT = Text("FLAG", style="red bold")
_WATCH_TEXT = Text("? watch", style="yellow bold")
_DASH_TEXT = Text("-", style="dim")


# ---------- data loading ----------


def _pairs_file(assignment_dir: Path) -> Path:
    return assignment_dir / "plagiarism" / "all_pairs.json"


def _aggregate_file(course_dir: Path) -> Path:
    return course_dir / "plagiarism" / AGG_JSON_NAME


def _load_payload(file_: Path) -> tuple[object | None, str | None]:
    """``(payload, error)`` for a pairs/aggregate JSON file.

    ``None`` payload = file absent; parse failures carry the exact error
    string. Shape checks (and their exact error strings) stay in the callers.
    """
    if not file_.is_file():
        return None, None
    try:
        data = json.loads(file_.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return data, None


def _load_pairs(assignment_dir: Path) -> tuple[list[dict] | None, str | None]:
    """(pairs, error).  ``None`` pairs = file absent (detection not run)."""
    data, err = _load_payload(_pairs_file(assignment_dir))
    if err is not None:
        return None, err
    if data is None:
        return None, None
    if not isinstance(data, dict) or not isinstance(data.get("pairs"), list):
        return None, "malformed payload (missing list 'pairs')"
    return data["pairs"], None


def _load_course_pairs(
    state: AppState,
) -> tuple[list[tuple[AssignmentInfo, dict]], list[str]]:
    """Course-scoped pairs: ``(assignment_info, pair)`` for every assignment
    in ``state.assignments`` that has pair data.

    Returns ``(loaded, errors)``; absent files (detection not run) are
    skipped silently, corrupt/malformed payloads are recorded as per-
    assignment errors.
    """
    loaded: list[tuple[AssignmentInfo, dict]] = []
    errors: list[str] = []
    for info in state.assignments:
        pairs, err = _load_pairs(info.config_path.parent)
        if err is not None:
            errors.append(f"{info.dir_name}: {err}")
            continue
        if pairs is None:
            continue
        loaded.extend((info, pair) for pair in pairs)
    return loaded, errors


def _load_aggregate(course_dir: Path) -> tuple[dict | None, str | None]:
    """(aggregate payload, error).  ``None`` payload = file absent."""
    data, err = _load_payload(_aggregate_file(course_dir))
    if err is not None:
        return None, err
    if data is None:
        return None, None
    if not isinstance(data, dict):
        return None, "malformed payload (not an object)"
    if not isinstance(data.get("pairs"), list):
        return None, "malformed payload (missing list 'pairs')"
    return data, None


def _overlap_display(pair: dict) -> str:
    """token_overlap cell text (int count, or line-set length when a list)."""
    overlap = pair.get("token_overlap")
    if isinstance(overlap, list):
        return str(len(overlap))
    if isinstance(overlap, float):
        return str(int(overlap))
    return str(overlap)  # int or missing


def pair_side_name(
    assignments_dir: Path,
    course_dir_name: str,
    assignment_info: AssignmentInfo,
    file_name: str | None,
) -> str:
    """Display name for one pair side: the file stem is the student uid."""
    stem = Path(str(file_name)).stem
    return student_display_name(
        assignments_dir,
        course_dir_name,
        assignment_info.dir_name,
        base_uid(stem),
    )


# ---------- aggregate JSON writer (used by the [a] job) ----------


def _aggregate_rows(
    course_dir: Path, alpha: float, floor: float, cap: float
) -> list[dict]:
    """Full combined pair ranking (shared core in plagiarism_aggregate)."""
    return aggregate_pair_rows(course_dir, alpha, floor, cap)


def _write_aggregate_json(course_config: Path) -> Path:
    """Write ``<course>/plagiarism/aggregate.json`` for the pane."""
    course_dir = course_config.parent
    # Root-section read: course configs carry no [grading], so the layered
    # load_assignment_file path raises on real data.  Same section source
    # the CLI aggregate report uses (root_plagiarism_section).
    plag = root_plagiarism_section(course_config)
    rows = sorted(
        _aggregate_rows(
            course_dir,
            plag.pairwise_alpha,
            plag.score_floor,
            plag.score_cap,
        ),
        key=lambda row: (-row["z_score"], row["one_sided_p_value"]),
    )
    payload = {
        "assignments_root": str(course_dir),
        "alpha": plag.pairwise_alpha,
        "tested_pairs": len(rows),
        "flagged_pairs": sum(
            1 for row in rows if row["one_sided_p_value"] <= plag.pairwise_alpha
        ),
        "pairs": rows,
    }
    out = _aggregate_file(course_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[plagiarism] aggregate json -> {out}")
    return out


# ---------- job worker functions (called on worker threads) ----------


def _run_detect_job(config_path: Path) -> dict | None:
    """Single-assignment copydetect run (no aggregate); quiet: the pane
    reads all_pairs.json, not the text report."""
    return detect_plagiarism(config_path, aggregate=False, quiet=True)


def run_aggregate_job(config_path: Path) -> dict | None:
    """Course aggregate: detect_plagiarism (quiet) + JSON for the pane."""
    summary = detect_plagiarism(config_path, aggregate=True, quiet=True)
    try:
        _write_aggregate_json(config_path)
    except Exception as exc:
        print(f"[error] aggregate json write failed: {exc}")
    return summary


# ---------- side resolution (shared by the compare pane) ----------


def _resolve_side(
    assignment_dir: Path, file_name: str
) -> tuple[Path | None, Path | None]:
    """(raw, processed) for one compare side; code submissions carry a
    ``<assignment>__<stem>`` prefix handled by stripping segments."""
    stem = Path(file_name).stem
    candidates = [stem, *(part for part in stem.split("__") if part)]
    processed_dir = assignment_dir / "processed"
    for candidate in candidates:
        processed = processed_dir / f"{candidate}.md"
        raw = find_raw_file(processed_dir, candidate)
        if raw is not None or processed.is_file():
            return raw, processed if processed.is_file() else None
    return None, None


def _side_lines(assignment_dir: Path, file_name: str, overlap_lines: set[int]) -> str:
    """Numbered file lines; lines in ``overlap_lines`` rendered red."""
    raw, processed = _resolve_side(assignment_dir, file_name)
    result = preview_content(raw, processed)
    if result is None:
        return f"[dim]{escape(file_name)}: file not found[/dim]"
    lines = result[1].splitlines()[:SIDE_MAX_LINES]
    out: list[str] = []
    for number, line in enumerate(lines, 1):
        escaped = f"{number:>4}  {escape(line)}"
        if number in overlap_lines:
            out.append(f"[red]{escaped}[/red]")
        else:
            out.append(escaped)
    return "\n".join(out)


# ---------- embedded compare pane ----------


def _cmp_pane() -> ComposeResult:
    """Side-by-side compare below the pairs table (hidden by default)."""
    with Horizontal(id="cmp-pane"), Vertical():
        yield Static("", id="cmp-title", markup=True)
        with Grid(id="cmp-grid"):
            with VerticalScroll():
                yield Static("", id="cmp-left", markup=True)
            with VerticalScroll():
                yield Static("", id="cmp-right", markup=True)


# ---------- the screen ----------


class PlagiarismScreen(JobHost):
    """S4 plagiarism tab: course-scoped tabs with p/a job buttons."""

    log_widget_id = "#plag-log"
    cancel_button_id = "#plag-cancel"
    progress_text_id = "#plag-progress-text"
    protect_message = "Job '{stage}' is running — use the Cancel button"
    cancelled_log = "Job cancelled — progress saved"
    cancelled_notify = "Cancelled"

    can_focus = True  # keeps screen bindings alive while a job runs

    BINDINGS: ClassVar = [
        Binding("tab", "next_pane", "Switch pane", priority=True),
        Binding("up", "cursor_up", "Cursor up"),
        Binding("down", "cursor_down", "Cursor down"),
        Binding("k", "cursor_up", "Cursor up"),
        Binding("j", "cursor_down", "Cursor down"),
        Binding("p", "run_detect", "Run detection"),
        Binding("a", "run_aggregate", "Run aggregation"),
        Binding("r", "reload", "Reload"),
        Binding("escape", "go_dashboard", "Dashboard"),
    ]

    def __init__(self, state: AppState) -> None:
        super().__init__(id="plagiarism")
        self.state = state
        self._job: dict | None = None
        self._course_pairs: list[tuple[AssignmentInfo, dict]] = []
        self._course_errors: list[str] = []
        self._visible_rows: list[tuple[AssignmentInfo, dict]] = []
        self._agg: dict | None = None
        self._agg_error: str | None = None
        self._threshold_pct = DEFAULT_DISPLAY_THRESHOLD_PCT
        self._last_error: str | None = None

    # ---------- composition ----------

    @override
    def compose(self) -> ComposeResult:
        yield Static(id="plag-topbar", markup=True)
        with Horizontal(id="plag-buttons"):
            yield Button("Run detection", id="plag-run")
            yield Button("Run aggregate", id="plag-aggregate")
        with TabbedContent(id="plag-tabs", initial="pane-aggregate"):
            with TabPane("Aggregate", id="pane-aggregate"):
                yield DataTable(id="agg-table", cursor_type="row", zebra_stripes=True)
                yield Static("", id="agg-empty", markup=True)
            with TabPane("Assignments", id="pane-assignments"):
                yield DataTable(
                    id="assign-table", cursor_type="row", zebra_stripes=True
                )
                yield Static("", id="assign-empty", markup=True)
            with TabPane("Students", id="pane-students"):
                yield DataTable(
                    id="students-table", cursor_type="row", zebra_stripes=True
                )
                yield Static("", id="students-empty", markup=True)
            with TabPane("Pairs", id="pane-pairs"):
                yield DataTable(id="pairs-table", cursor_type="row", zebra_stripes=True)
                yield Static("", id="pairs-empty", markup=True)
                yield from _cmp_pane()
        with Horizontal(id="plag-progress"):
            yield Static("", id="plag-progress-text", markup=True)
            yield Button("Cancel", id="plag-cancel", variant="warning")
        yield RichLog(
            markup=True,
            wrap=True,
            max_lines=2000,
            auto_scroll=True,
            id="plag-log",
        )
        yield Static(id="plag-status", markup=True)
        yield Static(id="plag-empty", markup=True)

    def on_mount(self) -> None:
        self.styles.height = "1fr"
        self.query_one("#plag-tabs", TabbedContent).styles.height = "1fr"
        self.query_one("#plag-log", RichLog).styles.height = 8
        self.query_one("#plag-progress", Horizontal).display = False
        for table_id in (
            "pairs-table",
            "assign-table",
            "students-table",
            "agg-table",
        ):
            self.query_one(f"#{table_id}", DataTable).styles.height = "1fr"
        for empty_id in (
            "pairs-empty",
            "assign-empty",
            "students-empty",
            "agg-empty",
        ):
            empty = self.query_one(f"#{empty_id}", Static)
            empty.styles.height = "1fr"
            empty.styles.content_align = ("center", "middle")
            empty.styles.opacity = 0.6
        # embedded compare pane: ~40% of the pairs pane, table takes the rest
        cmp_pane = self.query_one("#cmp-pane", Horizontal)
        cmp_pane.display = False
        cmp_pane.styles.height = "40%"
        self.query_one("#cmp-pane > Vertical", Vertical).styles.width = "1fr"
        grid = self.query_one("#cmp-grid", Grid)
        grid.styles.grid_size_columns = 2
        grid.styles.grid_size_rows = 1
        grid.styles.grid_columns = "1fr 1fr"
        grid.styles.grid_rows = "1fr"
        for scroll in self.query("#cmp-pane VerticalScroll"):
            scroll.styles.height = "1fr"
        self.set_interval(0.1, self._tick)
        # Reload here AND on tab activation (tata_app.TabActivated): the
        # activation handler covers the shell's tab pane; this covers
        # directly pushed instances (check scripts) and startup state.
        self.reload_all()

    # ---------- public API (called by the platform shell on tab switch) ----------

    def reload_all(self) -> None:
        """Re-read state/JSON files and re-render everything."""
        state = self.state
        empty = self.query_one("#plag-empty", Static)
        if state.current_course is None:
            self.query_one("#plag-topbar", Static).display = False
            self.query_one("#plag-buttons", Horizontal).display = False
            self.query_one("#plag-tabs", TabbedContent).display = False
            self.query_one("#plag-status", Static).display = False
            empty.styles.height = "1fr"
            empty.styles.content_align = ("center", "middle")
            empty.styles.opacity = 0.6
            empty.update("No course selected. Open Dashboard and enter a course first.")
            empty.display = True
            return

        # pre-existing errors may have cleared on a later load
        self._last_error = None
        self._agg_error = None
        self._course_errors = []
        for widget_id in (
            "#plag-topbar",
            "#plag-buttons",
            "#plag-tabs",
            "#plag-status",
        ):
            self.query_one(widget_id).display = True
        empty.display = False

        course = state.current_course
        # the panes are course-scoped: the knob is the course-level threshold
        # (shared tolerant helper with scan_courses — malformed configs
        # fall back to the default instead of blanking the pane)
        self._threshold_pct = _plagiarism_threshold_pct(course.config_path)
        self._course_pairs, self._course_errors = _load_course_pairs(state)
        self._agg, self._agg_error = _load_aggregate(course.config_path.parent)
        self._notify_load_errors()
        self._render_topbar()
        self._render_assignments()
        self._render_students()
        self._render_pairs()
        self._render_aggregate()
        self._render_status()

    def _notify_load_errors(self) -> None:
        errors = list(self._course_errors)
        if self._agg_error is not None:
            errors.append(self._agg_error)
        joined = " / ".join(errors) if errors else None
        if joined is not None and joined != self._last_error:
            self._last_error = joined
            self.app.notify(f"Load failed: {joined}", severity="error")

    # ---------- rendering ----------

    def _course_dir_name(self) -> str:
        course = self.state.current_course
        return course.dir_name if course is not None else ""

    def _render_topbar(self) -> None:
        state = self.state
        course = state.current_course
        context = (
            course_display_name(
                state.assignments_dir, course.dir_name, course.course_id
            )
            if course is not None
            else "-"
        )
        n_pairs = len(self._course_pairs)
        cap = f" (top {PAGE_ROWS})" if n_pairs > PAGE_ROWS else ""
        top = (
            f"Plagiarism · [b]{escape(context)}[/b]  ·  pairs {n_pairs}{cap}"
            f"  ·  display threshold {self._threshold_pct:.0f}%"
        )
        self.query_one("#plag-topbar", Static).update(top)

    def _render_assignments(self) -> None:
        table = self.query_one("#assign-table", DataTable)
        empty = self.query_one("#assign-empty", Static)
        table.clear(columns=True)
        by_assignment: dict[str, list[dict]] = {}
        for a, pair in self._course_pairs:
            by_assignment.setdefault(a.dir_name, []).append(pair)
        state = self.state
        course_dir_name = self._course_dir_name()
        rows: list[tuple[AssignmentInfo, int, int, float]] = []
        for a in state.assignments:
            pairs = by_assignment.get(a.dir_name, [])
            max_sim = max((_pair_pct(p) for p in pairs), default=0.0)
            flagged = sum(1 for p in pairs if _pair_pct(p) >= self._threshold_pct)
            rows.append((a, len(pairs), flagged, max_sim))
        rows.sort(key=lambda r: (-r[3], r[0].dir_name))
        if not rows:
            self._show_pane_empty(empty, table, "No assignments in this course.")
            return
        table.add_columns("Assignment", "Pairs", "Flagged", "Max sim %")
        for index, (a, count, flagged, max_sim) in enumerate(rows):
            table.add_row(
                escape(
                    assignment_display_name(
                        state.assignments_dir,
                        course_dir_name,
                        a.dir_name,
                        a.assignment_id,
                    )
                ),
                str(count),
                str(flagged),
                f"{max_sim:.1f}",
                key=str(index),
            )
        empty.display = False
        table.display = True
        if self._course_errors:
            # per-assignment load failures: note under the table (rows that
            # did load stay visible)
            empty.update(
                "[dim]Load failed: " + escape("; ".join(self._course_errors)) + "[/dim]"
            )
            empty.styles.height = "auto"
            empty.display = True

    def _render_students(self) -> None:
        table = self.query_one("#students-table", DataTable)
        empty = self.query_one("#students-empty", Static)
        table.clear(columns=True)
        state = self.state
        course_dir_name = self._course_dir_name()
        # Students aggregated over all course pairs by base uid.  A pair
        # counts once per student per row (a self-pair still counts); the
        # display name comes from the first pair/assignment the student
        # appears in (state.assignments order) — alias chains are
        # per-student, so the choice is cosmetic but deterministic.
        students: dict[str, list] = {}
        for a, pair in self._course_pairs:
            sim = _pair_pct(pair)
            flagged = sim >= self._threshold_pct
            seen: set[str] = set()
            for key in ("test_file", "reference_file"):
                stem = Path(str(pair.get(key) or "")).stem
                uid = base_uid(stem)
                if uid in seen:
                    continue
                seen.add(uid)
                rec = students.get(uid)
                if rec is None:
                    rec = [
                        pair_side_name(
                            state.assignments_dir,
                            course_dir_name,
                            a,
                            str(pair.get(key)),
                        ),
                        0,
                        0,
                        0.0,
                    ]
                    students[uid] = rec
                rec[1] += 1
                if flagged:
                    rec[2] += 1
                rec[3] = max(rec[3], sim)
        rows = sorted(students.values(), key=lambda r: (-r[3], str(r[0])))
        if not rows:
            self._show_pane_empty(
                empty,
                table,
                "No pairs yet. Run detection (p) or aggregation (a).",
            )
            return
        table.add_columns("Student", "Pairs", "Flagged", "Max sim %")
        for index, (name, count, flagged, max_sim) in enumerate(rows):
            table.add_row(
                escape(name),
                str(count),
                str(flagged),
                f"{max_sim:.1f}",
                key=str(index),
            )
        empty.display = False
        table.display = True

    def _render_pairs(self) -> None:
        table = self.query_one("#pairs-table", DataTable)
        empty = self.query_one("#pairs-empty", Static)
        table.clear(columns=True)
        if not self._course_pairs:
            self._show_pane_empty(
                empty,
                table,
                "No pairs yet. Run detection (p) or aggregation (a).",
            )
            self._update_compare()
            return
        state = self.state
        course_dir_name = self._course_dir_name()
        rows = sorted(
            self._course_pairs,
            key=lambda r: (
                -_pair_pct(r[1]),
                r[0].dir_name,
                str(r[1].get("test_file") or ""),
            ),
        )[:PAGE_ROWS]
        self._visible_rows = rows
        table.add_columns(
            "Assignment", "Student A", "Student B", "sim %", "overlap", "Flag"
        )
        for index, (a, pair) in enumerate(rows):
            sim = _pair_pct(pair)
            table.add_row(
                escape(
                    assignment_display_name(
                        state.assignments_dir,
                        course_dir_name,
                        a.dir_name,
                        a.assignment_id,
                    )
                ),
                escape(
                    pair_side_name(
                        state.assignments_dir,
                        course_dir_name,
                        a,
                        str(pair.get("test_file")),
                    )
                ),
                escape(
                    pair_side_name(
                        state.assignments_dir,
                        course_dir_name,
                        a,
                        str(pair.get("reference_file")),
                    )
                ),
                f"{sim:.1f}",
                _overlap_display(pair),
                _FLAG_TEXT if sim >= self._threshold_pct else _DASH_TEXT,
                key=str(index),
            )
        empty.display = False
        table.display = True
        self._update_compare()

    def _render_aggregate(self) -> None:
        table = self.query_one("#agg-table", DataTable)
        empty = self.query_one("#agg-empty", Static)
        table.clear(columns=True)
        if self._agg_error is not None:
            self._show_pane_empty(empty, table, f"Load failed: {self._agg_error}")
            return
        if self._agg is None:
            self._show_pane_empty(empty, table, "No aggregate report yet. Run (a).")
            return
        rows = self._agg.get("pairs") or []
        if not rows:
            self._show_pane_empty(empty, table, "Aggregation done, 0 tested pairs.")
            return
        alpha = float(self._agg.get("alpha") or AGG_ALPHA_FALLBACK)
        table.add_columns("Student A", "Student B", "raw sim", "z", "p", "Flag")
        course_dir_name = self._course_dir_name()
        ranking = sorted(
            rows,
            key=lambda row: (-float(row.get("z_score") or 0.0),),
        )[:PAGE_ROWS]
        for index, row in enumerate(ranking):
            p = float(row.get("one_sided_p_value") or 1.0)
            z = float(row.get("z_score") or 0.0)
            if p < alpha:
                flag = _FLAG_TEXT
            elif z >= Z_WATCH_THRESHOLD and p >= alpha:
                flag = _WATCH_TEXT
            else:
                flag = _DASH_TEXT
            table.add_row(
                escape(
                    course_student_display_name(
                        self.state.assignments_dir,
                        course_dir_name,
                        str(row.get("student_a") or "-"),
                    )
                ),
                escape(
                    course_student_display_name(
                        self.state.assignments_dir,
                        course_dir_name,
                        str(row.get("student_b") or "-"),
                    )
                ),
                f"{float(row.get('raw_similarity_pct') or 0.0):.1f}",
                f"{z:.2f}",
                f"{p:.3g}",
                flag,
                key=str(index),
            )
        empty.display = False
        table.display = True

    def _render_status(self) -> None:
        flagged = sum(
            1
            for _a, pair in self._course_pairs
            if _pair_pct(pair) >= self._threshold_pct
        )
        self.query_one("#plag-status", Static).update(
            f"{len(self._course_pairs)} pairs total · {flagged} flagged"
            f" (display threshold {self._threshold_pct:.0f}%)"
        )

    # ---------- pane/table helpers ----------

    @staticmethod
    def _show_pane_empty(empty: Static, table: DataTable, message: str) -> None:
        empty.update(message)
        empty.styles.height = "1fr"
        empty.display = True
        table.display = False

    def _active_table(self) -> DataTable | None:
        tabs = self.query_one("#plag-tabs", TabbedContent)
        pane = tabs.active_pane
        if pane is None:
            return None
        return pane.query(DataTable).first() if pane.query(DataTable) else None

    def _focus_active_table(self) -> None:
        if not is_displayed(self):
            return  # plagiarism tab hidden — never steal focus (F2)
        table = self._active_table()
        if table is not None and table.display and table.row_count > 0:
            table.focus()
        else:
            self.focus()

    # ---------- embedded compare pane ----------

    def _update_compare(self) -> None:
        pane = self.query_one("#cmp-pane", Horizontal)
        table = self.query_one("#pairs-table", DataTable)
        cursor = table.cursor_row
        if cursor is None or not (0 <= cursor < len(self._visible_rows)):
            pane.display = False
            return
        state = self.state
        if state.current_course is None:
            pane.display = False
            return
        course_dir_name = state.current_course.dir_name
        a, pair = self._visible_rows[cursor]
        overlap = pair.get("token_overlap")
        overlap_lines = (
            {int(line) for line in overlap} if isinstance(overlap, list) else set()
        )
        sim = _pair_pct(pair)
        flag_note = "  [red]FLAG[/red]" if sim >= self._threshold_pct else ""
        test_name = pair_side_name(
            state.assignments_dir,
            course_dir_name,
            a,
            str(pair.get("test_file")),
        )
        ref_name = pair_side_name(
            state.assignments_dir,
            course_dir_name,
            a,
            str(pair.get("reference_file")),
        )
        self.query_one("#cmp-title", Static).update(
            f"[b]Compare: {escape(test_name)} ↔ "
            f"{escape(ref_name)}   max_sim {sim:.1f}%"
            f"   token_overlap {_overlap_display(pair)}{flag_note}[/b]"
        )
        assignment_dir = a.config_path.parent
        self.query_one("#cmp-left", Static).update(
            _side_lines(assignment_dir, str(pair.get("test_file")), overlap_lines)
        )
        self.query_one("#cmp-right", Static).update(
            _side_lines(assignment_dir, str(pair.get("reference_file")), overlap_lines)
        )
        pane.display = True

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "pairs-table":
            return
        self._update_compare()

    # ---------- actions ----------

    def action_next_pane(self) -> None:
        tabs = self.query_one("#plag-tabs", TabbedContent)
        try:
            next_index = (_PANE_ORDER.index(str(tabs.active)) + 1) % len(_PANE_ORDER)
        except ValueError:
            next_index = 0
        tabs.active = _PANE_ORDER[next_index]
        self._focus_active_table()

    def action_cursor_up(self) -> None:
        table = self._active_table()
        if table is not None and table.row_count > 0:
            table.action_cursor_up()

    def action_cursor_down(self) -> None:
        table = self._active_table()
        if table is not None and table.row_count > 0:
            table.action_cursor_down()

    def action_reload(self) -> None:
        self.reload_all()
        self.app.notify("Reloaded", severity="information")

    def action_go_dashboard(self) -> None:
        """esc: back to the Dashboard tab (no-op when not inside the shell)."""
        for tabbed in self.app.query(TabbedContent):
            if tabbed.id == "plag-tabs":
                continue
            tabbed.active = 0
            return

    def action_cancel_job(self) -> None:
        job = self._job
        if job is None:
            return
        if job["state"] == "stopping":
            return
        job["cancel_event"].set()
        job["state"] = "stopping"
        self._log_line("Cancel requested — current item finishes, no new tasks start")
        self._render_busy()

    def action_run_detect(self) -> None:
        if self._protect():
            return
        info = self.state.current_assignment
        if info is None:
            self.app.notify(
                "No assignment selected — enter a course and assignment first",
                severity="warning",
            )
            return
        self._start_job("detect", _run_detect_job, info.config_path)

    def action_run_aggregate(self) -> None:
        if self._protect():
            return
        course = self.state.current_course
        if course is None:
            self.app.notify("No course selected", severity="warning")
            return
        self._start_job("aggregate", run_aggregate_job, course.config_path)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "plag-run":
            self.action_run_detect()
        elif button_id == "plag-aggregate":
            self.action_run_aggregate()
        elif button_id == "plag-cancel":
            self.action_cancel_job()

    # ---------- job protocol (shared core in src.tata_jobs.JobHost) ----------

    def _render_busy(self) -> None:
        busy = self._job is not None
        self.query_one("#plag-progress", Horizontal).display = busy
        self.query_one("#plag-run", Button).disabled = busy
        self.query_one("#plag-aggregate", Button).disabled = busy
        if not busy:
            self.query_one("#plag-run", Button).label = "Run detection"
            self.query_one("#plag-aggregate", Button).label = "Run aggregate"
            return
        self._render_busy_cancel()

    @override
    def job_finished(self, job: dict, summary: dict | None) -> None:
        self.reload_all()
        self._focus_active_table()
