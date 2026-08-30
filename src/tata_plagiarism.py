"""S4 Plagiarism screen (T6a): pair ranking + course aggregate panes.

Two panes (``TabbedContent``): ``#pane-pairs`` — per-assignment pair table
(sorted by ``max_similarity_pct``, top 20 rows) — and ``#pane-aggregate`` —
course-level z-score table.  ``enter`` on a pair opens a side-by-side
compare ``ModalScreen``.  All UI copy is English (design 04 v1.1; no
cross-course pane — that was dropped by user decision).

Jobs reuse the S2 JobHandle protocol (design 99 §3.1): the worker thread
runs :func:`src.plagiarism.detect_plagiarism` with stdout redirected into a
log queue; the main thread drains it into the RichLog.  ``[p]`` detects the
current assignment, ``[a]`` runs the course aggregate.

Data sources:
- pairs: ``<assignment>/plagiarism/all_pairs.json`` (copydetect rows)
- aggregate: ``<course>/plagiarism/aggregate.json`` — ``detect_plagiarism``
  only prints the text report, so the [a] worker additionally writes this
  JSON; the pane reads it.  Rows are built with ``aggregate_pair_rows`` —
  the same public entry point the CLI report assembly uses.
"""

from __future__ import annotations

import json
import queue
import threading
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, override

from rich.markup import escape
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    ProgressBar,
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
from src.assignment_config import load_assignment_file
from src.plagiarism import detect_plagiarism
from src.plagiarism_aggregate import aggregate_pair_rows
from src.score_review import _base_uid, _find_raw_file, _preview_content
from src.tata_workspace import (
    _format_job_summary,
    _is_displayed,
    _run_stage_worker,
)

if TYPE_CHECKING:
    from src.tata_app import AppState

PAGE_ROWS = 20
SIDE_MAX_LINES = 300
DEFAULT_DISPLAY_THRESHOLD_PCT = 80.0
AGG_ALPHA_FALLBACK = 0.01
AGG_JSON_NAME = "aggregate.json"
Z_WATCH_THRESHOLD = 3.0

_ALPHA = "\N{GREEK SMALL LETTER ALPHA}"

_FLAG_TEXT = Text("FLAG", style="red bold")
_WATCH_TEXT = Text("? watch", style="yellow bold")
_DASH_TEXT = Text("-", style="dim")


# ---------- data loading ----------


def _pairs_file(assignment_dir: Path) -> Path:
    return assignment_dir / "plagiarism" / "all_pairs.json"


def _aggregate_file(course_dir: Path) -> Path:
    return course_dir / "plagiarism" / AGG_JSON_NAME


def _load_pairs(assignment_dir: Path) -> tuple[list[dict] | None, str | None]:
    """(pairs, error).  ``None`` pairs = file absent (detection not run)."""
    file_ = _pairs_file(assignment_dir)
    if not file_.is_file():
        return None, None
    try:
        data = json.loads(file_.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(data, dict) or not isinstance(data.get("pairs"), list):
        return None, "malformed payload (missing list 'pairs')"
    return data["pairs"], None


def _load_aggregate(course_dir: Path) -> tuple[dict | None, str | None]:
    """(aggregate payload, error).  ``None`` payload = file absent."""
    file_ = _aggregate_file(course_dir)
    if not file_.is_file():
        return None, None
    try:
        data = json.loads(file_.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return None, "malformed payload (not an object)"
    return data, None


def _display_threshold_pct(config_path: Path | None) -> float:
    """Assignment ``[plagiarism] display_threshold`` as percent (80 default)."""
    if config_path is None:
        return DEFAULT_DISPLAY_THRESHOLD_PCT
    try:
        return float(load_assignment_file(config_path).plagiarism.display_threshold) * 100.0
    except Exception:
        return DEFAULT_DISPLAY_THRESHOLD_PCT


def _overlap_display(pair: dict) -> str:
    """token_overlap cell text (int count, or line-set length when a list)."""
    overlap = pair.get("token_overlap")
    if isinstance(overlap, list):
        return str(len(overlap))
    if isinstance(overlap, float):
        return str(int(overlap))
    return str(overlap)  # int or missing


# ---------- aggregate JSON writer (used by the [a] job) ----------


def _aggregate_rows(course_dir: Path, alpha: float, floor: float, cap: float) -> list[dict]:
    """Full combined pair ranking (shared core in plagiarism_aggregate)."""
    return aggregate_pair_rows(course_dir, alpha, floor, cap)


def _write_aggregate_json(course_config: Path) -> Path:
    """Write ``<course>/plagiarism/aggregate.json`` for the pane."""
    course_dir = course_config.parent
    plag = load_assignment_file(course_config).plagiarism
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
    """Single-assignment copydetect run (no aggregate)."""
    return detect_plagiarism(config_path, aggregate=False)


def _run_aggregate_job(config_path: Path) -> dict | None:
    """Course aggregate: detect_plagiarism (text to the log) + JSON for the pane."""
    summary = detect_plagiarism(config_path, aggregate=True)
    try:
        _write_aggregate_json(config_path)
    except Exception as exc:
        print(f"[error] aggregate json write failed: {exc}")
    return summary


# ---------- compare modal ----------


def _pair_student_name(
    state: AppState, file_name: str | None
) -> str:
    """Display name for one pair side: the file stem is the student uid."""
    stem = Path(str(file_name)).stem
    info = state.current_assignment
    if info is None:
        return stem
    return student_display_name(
        state.assignments_dir,
        state.current_course.dir_name if state.current_course is not None else "",
        info.dir_name,
        _base_uid(stem),
    )


def _resolve_side(assignment_dir: Path, file_name: str) -> tuple[Path | None, Path | None]:
    """(raw, processed) for one compare side; code submissions carry a
    ``<assignment>__<stem>`` prefix handled by stripping segments."""
    stem = Path(file_name).stem
    candidates = [stem, *(part for part in stem.split("__") if part)]
    processed_dir = assignment_dir / "processed"
    for candidate in candidates:
        processed = processed_dir / f"{candidate}.md"
        raw = _find_raw_file(processed_dir, candidate)
        if raw is not None or processed.is_file():
            return raw, processed if processed.is_file() else None
    return None, None


def _side_lines(
    assignment_dir: Path, file_name: str, overlap_lines: set[int]
) -> str:
    """Numbered file lines; lines in ``overlap_lines`` rendered red."""
    raw, processed = _resolve_side(assignment_dir, file_name)
    result = _preview_content(raw, processed)
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


class CompareModal(ModalScreen[None]):
    """Side-by-side file comparison (design 04 §1)."""

    BINDINGS: ClassVar = [
        Binding("escape", "close", "Close", show=False),
        Binding("c", "close", "Close", show=False),
    ]

    def __init__(
        self,
        title: str,
        left_name: str,
        left_text: str,
        right_name: str,
        right_text: str,
    ) -> None:
        super().__init__()
        self._title = title
        self._left_name = left_name
        self._left_text = left_text
        self._right_name = right_name
        self._right_text = right_text

    def action_close(self) -> None:
        self.dismiss()

    @override
    def compose(self) -> ComposeResult:
        with Vertical(classes="confirm-modal"):
            yield Static(f"[b]{escape(self._title)}[/b]", classes="modal-title")
            with Grid(id="cmp-grid"):
                with VerticalScroll():
                    yield Static(f"[b]{escape(self._left_name)}[/b]", markup=True)
                    yield Static(self._left_text, markup=True, id="cmp-left")
                with VerticalScroll():
                    yield Static(f"[b]{escape(self._right_name)}[/b]", markup=True)
                    yield Static(self._right_text, markup=True, id="cmp-right")

    def on_mount(self) -> None:
        grid = self.query_one("#cmp-grid", Grid)
        grid.styles.grid_size_columns = 2
        grid.styles.grid_size_rows = 1
        grid.styles.grid_columns = "1fr 1fr"
        grid.styles.grid_rows = "1fr"
        for scroll in self.query(VerticalScroll):
            scroll.styles.height = "1fr"
        self.query_one("Vertical", Vertical).styles.max_height = 36


# ---------- the screen ----------


class PlagiarismScreen(Vertical):
    """S4 plagiarism tab: pairs + aggregate panes with p/a job buttons."""

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
        self._pairs: list[dict] | None = None
        self._pairs_error: str | None = None
        self._visible_pairs: list[dict] = []
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
        with TabbedContent(id="plag-tabs", initial="pane-pairs"):
            with TabPane("Pairs", id="pane-pairs"):
                yield DataTable(id="pairs-table", cursor_type="row", zebra_stripes=True)
                yield Static("", id="pairs-empty", markup=True)
            with TabPane("Aggregate", id="pane-aggregate"):
                yield DataTable(id="agg-table", cursor_type="row", zebra_stripes=True)
                yield Static("", id="agg-empty", markup=True)
        with Horizontal(id="plag-progress"):
            yield Static("", id="plag-progress-text", markup=True)
            yield ProgressBar(show_eta=False, id="plag-bar")
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
        self.query_one("#plag-log", RichLog).styles.height = 12
        self.query_one("#plag-progress", Horizontal).display = False
        for table_id in ("pairs-table", "agg-table"):
            self.query_one(f"#{table_id}", DataTable).styles.height = "1fr"
        for empty_id in ("pairs-empty", "agg-empty"):
            empty = self.query_one(f"#{empty_id}", Static)
            empty.styles.height = "1fr"
            empty.styles.content_align = ("center", "middle")
            empty.styles.opacity = 0.6
        self.set_interval(0.1, self._tick)
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
            empty.update(
                "No course selected. Open Dashboard and enter a course first."
            )
            empty.display = True
            return

        # pre-existing errors may have cleared on a later load
        self._last_error = None
        self._pairs_error = None
        self._agg_error = None
        for widget_id in (
            "#plag-topbar",
            "#plag-buttons",
            "#plag-tabs",
            "#plag-status",
        ):
            self.query_one(widget_id).display = True
        empty.display = False

        info = state.current_assignment
        self._threshold_pct = _display_threshold_pct(
            info.config_path if info is not None else None
        )
        if info is not None:
            self._pairs, self._pairs_error = _load_pairs(info.config_path.parent)
        else:
            self._pairs, self._pairs_error = None, None
        self._agg, self._agg_error = _load_aggregate(
            state.current_course.config_path.parent
        )
        self._notify_load_errors()
        self._render_topbar()
        self._render_pairs()
        self._render_aggregate()
        self._render_status()

    def _notify_load_errors(self) -> None:
        errors = [e for e in (self._pairs_error, self._agg_error) if e is not None]
        joined = " / ".join(errors) if errors else None
        if joined is not None and joined != self._last_error:
            self._last_error = joined
            self.app.notify(f"Load failed: {joined}", severity="error")

    # ---------- rendering ----------

    def _render_topbar(self) -> None:
        state = self.state
        info = state.current_assignment
        course = state.current_course
        context = (
            course_display_name(
                state.assignments_dir, course.dir_name, course.course_id
            )
            if course is not None
            else "-"
        )
        if info is not None:
            context += (
                " / "
                + assignment_display_name(
                    state.assignments_dir,
                    course.dir_name if course is not None else "",
                    info.dir_name,
                    info.assignment_id,
                )
            )
        n = len(self._pairs) if self._pairs is not None else 0
        top = f"Plagiarism · [b]{escape(context)}[/b]  ·  pairs {n}"
        if n > PAGE_ROWS:
            top += f" (top {PAGE_ROWS})"
        top += f"  ·  display threshold {self._threshold_pct:.0f}%"
        self.query_one("#plag-topbar", Static).update(top)

    def _render_pairs(self) -> None:
        table = self.query_one("#pairs-table", DataTable)
        empty = self.query_one("#pairs-empty", Static)
        table.clear(columns=True)
        if self._pairs_error is not None:
            self._show_pane_empty(empty, table, f"Load failed: {self._pairs_error}")
            return
        if self.state.current_assignment is None:
            self._show_pane_empty(
                empty,
                table,
                "No assignment selected. Open Dashboard and enter an assignment first.",
            )
            return
        if self._pairs is None:
            self._show_pane_empty(
                empty,
                table, "No pairs yet. Run plagiarism (p) or aggregation (a)."
            )
            return
        if not self._pairs:
            self._show_pane_empty(
                empty,
                table,
                "Detection done, 0 pairs (submissions <2 or all below threshold).",
            )
            return
        table.add_columns("File A", "File B", "sim %", "overlap", "diff", "Flag")
        rows = sorted(
            self._pairs,
            key=lambda p: float(p.get("max_similarity_pct") or 0.0),
            reverse=True,
        )[:PAGE_ROWS]
        self._visible_pairs = rows
        for index, pair in enumerate(rows):
            sim = float(pair.get("max_similarity_pct") or 0.0)
            diff = sim - self._threshold_pct
            table.add_row(
                escape(_pair_student_name(self.state, str(pair.get("test_file")))),
                escape(_pair_student_name(self.state, str(pair.get("reference_file")))),
                f"{sim:.1f}",
                _overlap_display(pair),
                f"{diff:+.1f}",
                _FLAG_TEXT if sim >= self._threshold_pct else _DASH_TEXT,
                key=str(index),
            )
        empty.display = False
        table.display = True

    @staticmethod
    def _show_pane_empty(empty: Static, table: DataTable, message: str) -> None:
        empty.update(message)
        empty.display = True
        table.display = False

    def _render_aggregate(self) -> None:
        table = self.query_one("#agg-table", DataTable)
        empty = self.query_one("#agg-empty", Static)
        table.clear(columns=True)
        if self._agg_error is not None:
            self._show_pane_empty(empty, table, f"Load failed: {self._agg_error}")
            return
        if self._agg is None:
            self._show_pane_empty(
                empty, table, "No aggregate report yet. Run (a)."
            )
            return
        rows = self._agg.get("pairs") or []
        if not rows:
            self._show_pane_empty(
                empty, table, "Aggregation done, 0 tested pairs."
            )
            return
        alpha = float(self._agg.get("alpha") or AGG_ALPHA_FALLBACK)
        table.add_columns("Student A", "Student B", "raw sim", "z", "p", "Flag")
        course_dir_name = (
            self.state.current_course.dir_name
            if self.state.current_course is not None
            else ""
        )
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
        parts: list[str] = []
        if self._pairs and not self._pairs_error:
            flags = sum(
                1
                for p in self._pairs
                if float(p.get("max_similarity_pct") or 0.0) >= self._threshold_pct
            )
            parts.append(
                f"{flags} of {len(self._pairs)} pairs over display_threshold"
                f" {self._threshold_pct:.0f}%"
            )
        if self._agg and not self._agg_error:
            alpha = float(self._agg.get("alpha") or AGG_ALPHA_FALLBACK)
            parts.append(
                f"{self._agg.get('flagged_pairs', 0)} of"
                f" {self._agg.get('tested_pairs', len(self._agg.get('pairs') or []))}"
                f" over {_ALPHA}={alpha:.4g}"
            )
        self.query_one("#plag-status", Static).update(" · ".join(parts))

    # ---------- pane/table helpers ----------

    def _active_table(self) -> DataTable | None:
        tabs = self.query_one("#plag-tabs", TabbedContent)
        pane = tabs.active_pane
        if pane is None:
            return None
        return pane.query(DataTable).first() if pane.query(DataTable) else None

    def _focus_active_table(self) -> None:
        if not _is_displayed(self):
            return  # plagiarism tab hidden — never steal focus (F2)
        table = self._active_table()
        if table is not None and table.display and table.row_count > 0:
            table.focus()
        else:
            self.focus()

    # ---------- actions ----------

    def action_next_pane(self) -> None:
        tabs = self.query_one("#plag-tabs", TabbedContent)
        tabs.active = "pane-aggregate" if tabs.active == "pane-pairs" else "pane-pairs"
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
        self._start_job("aggregate", _run_aggregate_job, course.config_path)

    def _protect(self) -> bool:
        if self._job is not None:
            self.app.notify(
                f"Job '{self._job['stage']}' is running — use the Cancel button",
                severity="warning",
            )
            return True
        return False

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "pairs-table":
            return
        self._open_compare()

    def _open_compare(self) -> None:
        info = self.state.current_assignment
        if info is None or self._pairs is None:
            return
        table = self.query_one("#pairs-table", DataTable)
        cursor = table.cursor_row
        if not 0 <= cursor < len(self._visible_pairs):
            return
        pair = self._visible_pairs[cursor]
        assignment_dir = info.config_path.parent
        overlap = pair.get("token_overlap")
        overlap_lines = {int(line) for line in overlap} if isinstance(overlap, list) else set()
        sim = float(pair.get("max_similarity_pct") or 0.0)
        flag_note = "  [red]FLAG[/red]" if sim >= self._threshold_pct else ""
        test_name = _pair_student_name(self.state, str(pair.get("test_file")))
        ref_name = _pair_student_name(self.state, str(pair.get("reference_file")))
        title = (
            f"Compare: {test_name} ↔ "
            f"{ref_name}   max_sim {sim:.1f}%"
            f"   token_overlap {_overlap_display(pair)}{flag_note}"
        )
        left = _side_lines(
            assignment_dir, str(pair.get("test_file")), overlap_lines
        )
        right = _side_lines(
            assignment_dir, str(pair.get("reference_file")), overlap_lines
        )
        self.app.push_screen(
            CompareModal(
                title,
                test_name,
                left,
                ref_name,
                right,
            )
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "plag-run":
            self.action_run_detect()
        elif button_id == "plag-aggregate":
            self.action_run_aggregate()
        elif button_id == "plag-cancel":
            self.action_cancel_job()

    # ---------- job protocol (design 99 §3.1, mirrors AssignmentScreen) ----------

    def _start_job(self, stage: str, fn: object, config_path: Path) -> None:
        if self.state.active_job is not None:
            self.app.notify(
                f"'{self.state.active_job}' is running — finish or cancel it first",
                severity="warning",
            )
            return
        q: queue.Queue = queue.Queue()
        cancel_event = threading.Event()
        self._job = {
            "stage": stage,
            "fn": fn,
            "kwargs": {},
            "config_path": config_path,
            "queue": q,
            "cancel_event": cancel_event,
            "total": None,
            "progress": 0,
            "state": "running",
            "text": "running…",
            "dir_name": config_path.parent.name,
        }
        self.state.active_job = stage
        self._log_line(f"▶ {stage} started ({config_path.name})")
        self._render_busy()
        self.focus()
        self.run_worker(
            partial(_run_stage_worker, job=self._job),
            thread=True,
            group="stage",
            exclusive=True,
        )

    def _render_busy(self) -> None:
        busy = self._job is not None
        self.query_one("#plag-progress", Horizontal).display = busy
        self.query_one("#plag-run", Button).disabled = busy
        self.query_one("#plag-aggregate", Button).disabled = busy
        if not busy:
            self.query_one("#plag-run", Button).label = "Run detection"
            self.query_one("#plag-aggregate", Button).label = "Run aggregate"
            return
        job = self._job
        cancel = self.query_one("#plag-cancel", Button)
        cancel.disabled = job["state"] == "stopping"
        cancel.label = "Stop…" if job["state"] == "stopping" else "Cancel"
        self.query_one("#plag-progress-text", Static).update(
            escape(job.get("text", "running…"))
        )

    def _tick(self) -> None:
        job = self._job
        if job is None:
            return
        q = job["queue"]
        while True:
            try:
                kind, payload = q.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._log_line(payload)
            elif kind == "done":
                self._job_done(payload)
                return

    def _job_done(self, summary: dict | None) -> None:
        job = self._job
        if job is None:
            return
        self.state.active_job = None
        self._job = None
        self._render_busy()
        if summary:
            if summary.get("cancelled"):
                self._log_line("Job cancelled — progress saved")
                self.app.notify("Cancelled", severity="information")
            else:
                line = _format_job_summary(summary)
                if line:
                    self._log_line(line)
                errors = int(summary.get("errors") or 0)
                self.app.notify(
                    line or f"Job {job['stage']} finished",
                    severity="warning" if errors else "success",
                )
        self.reload_all()
        self._focus_active_table()

    def _log_line(self, line: str) -> None:
        text = escape(line)
        if line.startswith("[error]") or "✗" in line:
            styled = f"[red]{text}[/red]"
        elif "[done]" in line or "✓" in line or "→" in line:
            styled = f"[green]{text}[/green]"
        else:
            styled = text
        self.query_one("#plag-log", RichLog).write(styled)
