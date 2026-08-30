"""S1 Assignment workspace (T4b): the six-stage workbench.

Third dashboard level, hosted inside :class:`src.tata_app.DashboardScreen`
(the workspace is not its own Tab — design 02 v1.1). All UI copy is English.

Long jobs use the JobHandle protocol (design 99 §3.1), shared with the
Plagiarism screen via :class:`src.tata_jobs.JobHost` (worker thread, log
queue, 0.1 s drain; see that module for the contract):
a worker thread runs the existing synchronous stage functions with
stdout/stderr redirected into a ``queue.Queue``; a 0.1 s timer on the main
thread drains the queue into the RichLog and updates the ProgressBar.
Worker threads never touch widgets.

Honesty notes over the design (design 99 accepted trade-offs):
- The stage functions are not modified and print no done/total events, so
  determinate progress comes from polling the same counters the incremental
  scan uses (processed/checkpoint/scored file counts) once per tick. When the
  count is unknown (fetch/analyze) the bar is indeterminate.
- Synchronous stage functions cannot be killed: ``cancel_event.set()`` puts
  the UI in "Stopping…" and the job's result is dropped when the function
  returns (checkpoint/mtime semantics make the next run incremental). No new
  job starts while one runs (exclusive worker group).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, override

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, ProgressBar, RichLog, Static

from src import cli as main
from src.aliases import assignment_display_name
from src.analysis import analyze_assignment
from src.assignment_config import load_assignment_file
from src.cli_options import FetchCliOptions
from src.grading import grade_assignment
from src.processing import preprocess_assignment
from src.score_review import open_score_review
from src.scoring import score_assignment
from src.tata_jobs import JobHost
from src.tata_scan import AssignmentInfo, count_files, count_recursive

if TYPE_CHECKING:
    from src.tata_app import AppState


# ---------- shared display helpers (also imported by tata_app) ----------


def is_displayed(widget: Widget) -> bool:
    """True when the widget and every ancestor has display enabled, and the
    widget is on the app's active screen.

    ``Widget.display`` only checks the node's own style; TabPane hides
    inactive panes by setting ``display=False`` on the pane, so a child of
    a hidden tab still reports ``display=True`` (F2 — focus must not land
    on a widget inside a hidden tab, and the screen check keeps focus out
    of a base screen while a modal is up).
    """
    node: Widget | None = widget
    while node is not None:
        if not node.display:
            return False
        node = node.parent
    return widget.screen is widget.app.screen


# State vocabulary (design 99 §2). "Flagged" fires on display-level pairs
# (max_similarity_pct >= DISPLAY_THRESHOLD_PCT), NOT on aggregate z-score
# flags (S4) — the TUI does not consume the aggregate report.
_STATE_LABELS = {
    "not_run": "Not run",
    "partial": "Partial",
    "done": "Done",
    "flagged": "Flagged",
}

_BADGE_COLOR = {
    "not_run": "dim",
    "partial": "yellow",
    "done": "green",
    "flagged": "red",
}

_STAGE_KEYS = (
    ("fetch", "fetch"),
    ("preprocess", "preprocess"),
    ("grade", "grade"),
    ("score", "score"),
    ("analyze", "analyze"),
    ("score review", "score_review"),
)


def state_key(a: AssignmentInfo) -> str:
    """Map an AssignmentInfo to a ``_STATE_LABELS`` key."""
    if a.flagged_pairs:
        return "flagged"
    if a.counts.raw == 0:
        return "not_run"
    if (
        a.counts.processed < a.counts.raw
        or a.counts.graded < a.counts.processed
        or a.counts.scored == 0
    ):
        return "partial"
    return "done"


def fmt_state(a: AssignmentInfo) -> str:
    """Counts-based pipeline state label (design 99 §2 vocabulary)."""
    if a.flagged_pairs:
        return f"{_STATE_LABELS['flagged']} ({a.flagged_pairs})"
    return _STATE_LABELS[state_key(a)]


def fmt_last_run(ts: float | None) -> str:
    if ts is None:
        return "Never"
    dt = datetime.fromtimestamp(ts, tz=UTC).astimezone()
    now = datetime.now(tz=UTC).astimezone()
    if dt.date() == now.date():
        return f"Today {dt:%H:%M}"
    return f"{dt:%Y-%m-%d %H:%M}"


def _checkpoint_done(assignment_dir: Path) -> int:
    """Entries in ``logs/grading.checkpoint.json`` (0 on missing/garbage)."""
    cp = assignment_dir / "logs" / "grading.checkpoint.json"
    try:
        done = json.loads(cp.read_text(encoding="utf-8")).get("done", [])
        return len(done) if isinstance(done, list) else 0
    except (ValueError, OSError):
        return 0


def _is_fetched(assignment_dir: Path) -> bool:
    """Fetch freshness = ``raw/.fetch-cache.json`` presence (design §5)."""
    return (assignment_dir / "raw" / ".fetch-cache.json").is_file()


def _incremental_line(info: AssignmentInfo) -> str:
    """'To run / No change' summary shown by the [i] toggle."""
    a_dir = info.config_path.parent
    raw, processed, graded, scored = (
        info.counts.raw,
        info.counts.processed,
        info.counts.graded,
        info.counts.scored,
    )
    done = _checkpoint_done(a_dir)
    to_run = {
        "fetch": 0 if _is_fetched(a_dir) else 1,
        "pre": max(raw - processed, 0),
        "grade": max(processed - done, 0),
        "score": max(graded - scored, 0),
    }
    no_change = sum(
        1
        for current, target in ((processed, raw), (done, processed), (scored, graded))
        if target > 0 and current == target
    )
    return (
        f"To run: fetch {to_run['fetch']} · pre {to_run['pre']}"
        f" · grade {to_run['grade']} · score {to_run['score']}"
        f"  |  No change: {no_change}"
    )


def _run_fetch_job(config_path: Path) -> None:
    """Fetch through the CLI entry point (single source of truth, main.py)."""
    main._run_fetch(FetchCliOptions(config=config_path))


# ---------- modals ----------


class ConfirmationModal(ModalScreen[str | None]):
    """Two-action confirmation (design 99 §3.3); cancel dismisses with None."""

    BINDINGS: ClassVar = [Binding("escape", "close", "Close", show=False)]

    def __init__(
        self,
        title: str,
        message: str,
        actions: list[tuple[str, str]],
    ) -> None:
        super().__init__()
        self._title = title
        self._message = message
        self._actions = actions

    def action_close(self) -> None:
        self.dismiss(None)

    @override
    def compose(self) -> ComposeResult:
        with Vertical(classes="confirm-modal"):
            yield Static(f"[b]{escape(self._title)}[/b]", classes="modal-title")
            yield Static(escape(self._message))
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="cancel", variant="default")
                for label, value in self._actions:
                    yield Button(label, id=value)

    def on_mount(self) -> None:
        # Enter confirms the first (safe) action.
        self.query_one(f"Button#{self._actions[0][1]}", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        assert event.button.id is not None
        self.dismiss(None if event.button.id == "cancel" else event.button.id)


# ---------- the workspace ----------


class AssignmentScreen(JobHost):
    """Third dashboard level: 6 stage buttons + config panel + live log.

    Owns the JobHandle state (queue + cancel event + progress) via
    :class:`src.tata_jobs.JobHost`. The worker thread is a Textual
    ``run_worker(thread=True, group='stage', exclusive=True)`` so only one
    stage job runs at a time.
    """

    log_widget_id = "#richlog"
    cancel_button_id = "#ws-cancel"
    progress_text_id = "#ws-progress-text"
    protect_message = "Stage job '{stage}' is running — press x to cancel"
    cancelled_log = "Job cancelled — progress saved (checkpoint/mtime based)"
    cancelled_notify = "Cancelled — progress saved"
    green_contains: ClassVar[tuple[str, ...]] = ("[done]", "✓")
    green_prefixes: ClassVar[tuple[str, ...]] = ("[processed]",)

    can_focus = True  # holds focus while stage buttons are disabled mid-job

    BINDINGS: ClassVar = [
        Binding("f", "run_fetch", "Fetch"),
        Binding("p", "run_preprocess", "Preprocess"),
        Binding("g", "run_grade", "Grade"),
        Binding("s", "run_score", "Score"),
        Binding("a", "run_analyze", "Analyze"),
        Binding("x", "cancel_job", "Cancel job"),
        Binding("e", "edit_config", "Edit config"),
        Binding("i", "toggle_incr", "Incremental"),
        Binding("F", "toggle_config", "Config panel"),
    ]

    def __init__(self, state: AppState) -> None:
        super().__init__(id="workspace")
        self.state = state
        self._info: AssignmentInfo | None = None
        self._job: dict | None = None  # JobHandle
        self._config_error: str | None = None
        self._incr_on = False
        self._sub: dict[str, str] = {}
        self._total: dict[str, int | None] = {}
        self._pending = 0
        self._done = 0
        self._processed = 0

    # ---------- composition ----------

    @override
    def compose(self) -> ComposeResult:
        yield Static(id="ws-topbar", markup=True)
        yield Static(id="ws-incr", markup=True)
        with Horizontal(id="ws-main"):
            with Grid(id="stage-grid"):
                for label, key in _STAGE_KEYS:
                    yield Button(
                        f"{label}\n{'…'}", id=f"stage-{key}", classes="stage-btn"
                    )
            with Vertical(id="config-panel"):
                yield Static("Parsing config…", id="config-body", markup=True)
        with Horizontal(id="ws-progress"):
            yield Static("", id="ws-progress-text", markup=True)
            yield ProgressBar(show_eta=False)
            yield Button("Cancel", id="ws-cancel", variant="warning")
        yield RichLog(
            markup=True,
            wrap=True,
            max_lines=2000,
            auto_scroll=True,
            id="richlog",
        )
        yield Static(id="ws-empty", markup=True)

    def on_mount(self) -> None:
        self._buttons = {
            key: self.query_one(f"#stage-{key}", Button) for _, key in _STAGE_KEYS
        }
        self.query_one("#config-panel", Vertical).border_title = "Config"
        self.query_one("#richlog", RichLog).border_title = "Live log"
        # Main-thread queue drain + progress poll (design 99 §3.1).
        self.set_interval(0.1, self._tick)

    # ---------- public API used by DashboardScreen ----------

    def open_assignment(self) -> None:
        """(Re)bind to ``state.current_assignment`` and fully re-render."""
        info = self.state.current_assignment
        if info is None:
            return
        self._info = info
        self.render_all()

    def focus_stage(self) -> None:
        """Focus the first enabled stage button (or the container itself)."""
        if self._config_error is not None:
            self.focus()
            return
        for btn in self._buttons.values():
            if not btn.disabled:
                btn.focus()
                return
        self.focus()

    # ---------- rendering ----------

    def render_all(self) -> None:
        info = self._info
        if info is None:
            return
        a_dir = info.config_path.parent
        raw, processed, graded, scored = (
            info.counts.raw,
            info.counts.processed,
            info.counts.graded,
            info.counts.scored,
        )
        done = _checkpoint_done(a_dir)
        self._pending = max(processed - done, 0)
        self._done = done
        self._processed = processed
        fetched = _is_fetched(a_dir)
        self._sub = {
            "fetch": f"raw {raw}" if fetched else "Not fetched",
            "preprocess": (
                f"{processed}/{raw} done" if raw > 0 else "Needs fetch first"
            ),
            "grade": (
                (
                    f"{processed}/{processed} done"
                    if processed > 0 and done >= processed
                    else f"{self._pending} pending · {done} done"
                )
                if processed > 0
                else ("Needs preprocess" if raw > 0 else "Needs fetch first")
            ),
            "score": (
                f"{scored}/{graded} scored" if graded > 0 else "Needs grade first"
            ),
            "analyze": (
                "stats done"
                if (a_dir / "logs" / "meta_analysis.json").is_file()
                else "Not run"
            ),
        }
        self._total = {
            "fetch": None,
            "preprocess": raw if raw > 0 else None,
            "grade": processed if processed > 0 else None,
            "score": graded if graded > 0 else None,
            "analyze": None,
        }
        self._render_topbar()
        self._render_buttons()
        self._render_config()
        self._render_incr()
        self._render_busy()

    def _render_topbar(self) -> None:
        a = self._info
        assert a is not None
        key = state_key(a)
        color = _BADGE_COLOR[key]
        badge = f"[{color}]{_STATE_LABELS[key]}[/{color}]"
        self.query_one("#ws-topbar", Static).update(
            f"Pipeline · [b]{escape(assignment_display_name(self.state.assignments_dir, self.state.current_course.dir_name if self.state.current_course is not None else '', a.dir_name, a.assignment_id))}[/b]"
            f"  ·  ID {a.assignment_id or '-'}"
            f"  ·  {badge}  ·  last run {fmt_last_run(a.last_run)}"
            "   [i]Incremental"
        )

    def _render_buttons(self) -> None:
        if self._config_error is not None:
            self.query_one("#stage-grid", Grid).display = False
            self.query_one("#ws-empty", Static).display = True
            self.query_one("#ws-empty", Static).update(
                "No valid config.toml for this assignment (missing [grading]?).\n"
                "Press `e` to edit the config, or fix it and press `r` to re-scan."
            )
            for btn in self._buttons.values():
                btn.disabled = True
            return
        self.query_one("#stage-grid", Grid).display = True
        self.query_one("#ws-empty", Static).display = False
        busy = self._job is not None
        for stage, key in _STAGE_KEYS:
            btn = self._buttons[key]
            btn.label = f"{stage}\n{self._sub.get(stage, '…')}"
            btn.disabled = busy

    def _render_config(self) -> None:
        info = self._info
        assert info is not None
        body = self.query_one("#config-body", Static)
        try:
            cfg = load_assignment_file(info.config_path)
        except Exception as exc:
            self._config_error = f"{type(exc).__name__}: {exc}"
            body.update(
                f"[red]Config parse failed:[/red]\n{escape(self._config_error)}"
            )
            return
        self._config_error = None
        g = cfg.grading
        prompts = (
            g.system_prompt if isinstance(g.system_prompt, list) else [g.system_prompt]
        )
        body.update(
            "\n".join((
                f"[b]rubric[/b]        {escape(g.rubric)}",
                f"[b]prompt[/b]        {escape(', '.join(prompts))}",
                f"[b]provider[/b]      {escape(g.provider)}",
                f"[b]max_parallel[/b]  {g.max_parallel_tasks}",
                f"[b]reference[/b]     {escape(cfg.assignment.reference_file or '(unset)')}",
            ))
        )

    def _render_incr(self) -> None:
        info = self._info
        assert info is not None
        incr = self.query_one("#ws-incr", Static)
        incr.update(_incremental_line(info))
        incr.display = self._incr_on

    # ---------- stage actions ----------

    def action_run_fetch(self) -> None:
        if self._protect():
            return
        if not self.state.env_state.get("has_env"):
            self.app.notify(
                "Canvas environment missing (.env with CANVAS_BASE_URL / "
                "CANVAS_ACCESS_TOKEN) — set it up first",
                severity="error",
            )
            return
        info = self._info
        if info is not None:
            try:
                cfg = load_assignment_file(info.config_path)
            except Exception as exc:
                self.app.notify(
                    f"Config parse failed: {type(exc).__name__}: {exc}",
                    severity="error",
                )
                return
            if cfg.fetch is None or cfg.fetch.course_id is None:
                self.app.notify(
                    "Course not configured for fetch — add a [fetch] course_id "
                    "to the course config",
                    severity="error",
                )
                return
        self._start_job("fetch", _run_fetch_job)

    def _needs_fetch(self) -> bool:
        """raw==0 guard: notify and block (design §7 'Needs fetch first')."""
        if self._info is not None and self._info.counts.raw == 0:
            self.app.notify(
                "No raw submissions — run fetch first",
                severity="warning",
            )
            return True
        return False

    def action_run_preprocess(self) -> None:
        if self._protect() or self._needs_fetch():
            return
        self._start_job(
            "preprocess", preprocess_assignment, total=self._total["preprocess"]
        )

    def action_run_grade(self) -> None:
        if self._protect() or self._needs_fetch():
            return
        if self._processed == 0:
            self.app.notify(
                "No processed submissions — run preprocess first",
                severity="warning",
            )
            return
        self.app.push_screen(
            ConfirmationModal(
                "Grade",
                f"Will grade {self._pending} of {self._processed} submissions"
                f" (checkpoint {self._done}/{self._processed} done).\n"
                "Normal resumes from the checkpoint; --force regrades all"
                f" {self._processed}.",
                [("Normal", "normal"), ("--force regrade all", "force")],
            ),
            self._confirm_grade,
        )

    def _confirm_grade(self, choice: str | None) -> None:
        if choice is None:
            return
        self._start_job(
            "grade",
            grade_assignment,
            kwargs={"force": choice == "force"},
            total=self._total["grade"],
        )

    def action_run_score(self) -> None:
        if self._protect() or self._needs_fetch():
            return
        if self._processed == 0:
            self.app.notify(
                "No graded submissions — run preprocess and grade first",
                severity="warning",
            )
            return
        if self._total.get("score") is None:
            self.app.notify(
                "No graded submissions — run grade first",
                severity="warning",
            )
            return
        self._start_job("score", score_assignment, total=self._total["score"])

    def action_run_analyze(self) -> None:
        if self._protect() or self._needs_fetch():
            return
        self._start_job("analyze", analyze_assignment)

    def action_run_score_review(self) -> None:
        info = self._info
        if info is None:
            return
        open_score_review(self.app, info.config_path.parent)

    def action_cancel_job(self) -> None:
        job = self._job
        if job is None:
            self.app.notify("No job is running", severity="information")
            return
        if job["state"] == "stopping":
            return
        job["cancel_event"].set()
        job["state"] = "stopping"
        self._log_line("Cancel requested — current item finishes, no new tasks start")
        self._render_busy()

    def action_edit_config(self) -> None:
        config_path = self._config_path()
        if config_path is None:
            return
        editor = os.environ.get("EDITOR")
        if not editor or shutil.which(editor.split()[0]) is None:
            self.app.notify(
                "$EDITOR not set or not found; set EDITOR to open config",
                severity="warning",
            )
            return
        # ponytail: blocking on purpose — the editor needs the tty; the TUI
        # redraws once the child exits.
        subprocess.run(
            f"{editor} {shlex.quote(str(config_path))}", shell=True, check=False
        )
        self.render_all()
        self.app.notify("Config reloaded", severity="information")

    def action_toggle_incr(self) -> None:
        self._incr_on = not self._incr_on
        self._render_incr()

    def action_toggle_config(self) -> None:
        panel = self.query_one("#config-panel", Vertical)
        panel.display = not panel.display

    def action_rescan(self) -> None:
        state = self.state
        if state.current_course is not None:
            state.load_assignments(state.current_course)
            if self._info is not None:
                for a in state.assignments:
                    if a.dir_name == self._info.dir_name:
                        self._info = a
                        break
        self.render_all()
        self.app.notify("Rescan complete", severity="information")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Dispatch stage buttons / cancel to the matching action."""
        button_id = event.button.id
        if button_id == "ws-cancel":
            self.action_cancel_job()
            return
        if button_id and button_id.startswith("stage-"):
            action = getattr(self, f"action_run_{button_id[6:]}", None)
            if action is not None:
                action()

    # ---------- job protocol (shared core in src.tata_jobs.JobHost) ----------

    def _config_path(self) -> Path | None:
        return self._info.config_path if self._info is not None else None

    def _render_busy(self) -> None:
        job = self._job
        busy = job is not None
        self.query_one("#ws-progress", Horizontal).display = busy
        self._render_buttons()
        if not busy:
            return
        self._render_busy_cancel()
        bar = self.query_one("#ws-progress > ProgressBar", ProgressBar)
        total = job.get("total")
        bar.total = total
        bar.progress = job.get("progress", 0) if total else 0

    @override
    def _job_is_ours(self, job: dict) -> bool:
        """True when the job targets the currently bound assignment."""
        return self._info is not None and job.get("dir_name") == self._info.dir_name

    @override
    def poll_progress(self, job: dict) -> None:
        """Per-tick progress polls the same counters the incremental scan uses."""
        if job["total"]:
            new_done = self._stage_done(job["stage"])
            if new_done != job["progress"]:
                job["progress"] = new_done
                if job["state"] == "running":
                    job["text"] = f"{new_done}/{job['total']}"
                self._render_busy()

    def _stage_done(self, stage: str) -> int:
        info = self._info
        if info is None:
            return 0
        a_dir = info.config_path.parent
        if stage == "preprocess":
            return count_files(a_dir / "processed", ".md")
        if stage == "grade":
            return _checkpoint_done(a_dir)
        if stage == "score":
            return count_recursive(a_dir / "scored")
        return 0

    @override
    def job_finished(self, job: dict, summary: dict | None) -> None:
        on_this_dir = (
            self._info is not None and job.get("dir_name") == self._info.dir_name
        )
        if on_this_dir and is_displayed(self):
            self.focus_stage()
        self._rescan_after_job(job)

    def _rescan_after_job(self, job: dict) -> None:
        """Re-scan increments; the job may outlive the current level."""
        state = self.state
        if state.current_course is not None:
            state.load_assignments(state.current_course)
        name = job.get("dir_name") or (self._info.dir_name if self._info else None)
        if name is None:
            return
        fresh = {a.dir_name: a for a in state.assignments}
        if name in fresh:
            if self._info is not None and self._info.dir_name == name:
                self._info = fresh[name]
            if (
                state.current_assignment is not None
                and state.current_assignment.dir_name == name
            ):
                state.current_assignment = fresh[name]
        self.render_all()
