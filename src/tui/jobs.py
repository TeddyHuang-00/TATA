"""Shared JobHandle protocol for the job-running screens (design 99 §3.1).

The Assignment workspace (``src.tui.workspace``) and the Plagiarism screen
(``src.tui.plagiarism``) both run long stage jobs the same way: a worker
thread runs the existing synchronous stage function with stdout/stderr
redirected into a ``queue.Queue``; a 0.1 s timer on the main thread drains
the queue into a RichLog and refreshes the busy row.  Worker threads never
touch widgets.

.. caveat:: the 0.1 s drain timer is widget-bound (``set_interval`` in each
   screen's ``on_mount``).  If a JobHost screen is ever unmounted mid-job,
   the worker's ``('done', …)`` marker is never drained and
   ``state.active_job`` stays stuck.  Upgrade path: host the drain on the
   App (or clear ``active_job`` in ``on_unmount``) if a screen is ever
   removed while its job runs.

:class:`JobHost` carries that protocol; the screens set class attrs for
their widget ids / message texts and override the hooks below only where
behavior genuinely differs.  Do NOT change the job dict key set, the
``run_worker(thread=True, group='stage', exclusive=True)`` arguments, the
queue drain semantics, the cancel semantics, or the shared
``state.active_job`` slot logic.
"""

from __future__ import annotations

import contextlib
import queue
import threading
from functools import partial
from pathlib import Path
from typing import ClassVar

from rich.markup import escape
from textual.containers import Vertical
from textual.widgets import Button, RichLog, Static

from src import cli as main


# ---------- log queue writer ----------
class _LineQueueWriter:
    """Stdout sink that splits writes into lines and enqueues them.

    Stage functions print per-file lines; each ``write`` is buffered until a
    newline so the queue carries whole log lines (best effort when the stage
    writes from its own thread pool).
    """

    def __init__(self, q: queue.Queue) -> None:
        self._q = q
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.rstrip():
                self._q.put(("log", line.rstrip()))
        return len(s)

    def flush(self) -> None:
        if self._buf.rstrip():
            self._q.put(("log", self._buf.rstrip()))
            self._buf = ""


# ---------- worker body ----------


def run_stage_worker(job: dict) -> None:
    """Worker thread body for one stage job (design 99 §3.1).

    Reads a JobHandle dict; redirects the stage function's stdout/stderr into
    the handle's log queue and pushes a ``("done", summary)`` marker at the
    end. Never touches widgets — the main thread drains the queue.
    """
    writer = _LineQueueWriter(job["queue"])
    with (
        contextlib.redirect_stdout(writer),
        contextlib.redirect_stderr(writer),
    ):
        try:
            summary = job["fn"](job["config_path"], **job["kwargs"])
        except BaseException as exc:  # SystemExit from fetch's non-tty exit included
            job["queue"].put(("log", f"[error] {type(exc).__name__}: {exc}"))
            summary = {
                "stage": job["stage"],
                "success": 0,
                "errors": 1,
                "total": 0,
                "success_rate": 0.0,
            }
    if job["cancel_event"].is_set():
        job["queue"].put(("done", {"cancelled": True}))
    else:
        job["queue"].put(("done", summary))


def format_job_summary(summary: dict) -> str:
    """Summary line shaped exactly like the CLI's (design 02 §6)."""
    return main._format_job_summary(summary)


# ---------- the mixin ----------


class JobHost(Vertical):
    """Shared JobHandle protocol (design 99 §3.1).

    Job-running screens inherit this and set the class attrs below (widget
    ids and message texts; both screens use their own exact wording).
    ``_start_job`` takes the config explicitly, or ``config_path=None`` when
    the screen provides ``_config_path()`` (AssignmentScreen derives it from
    its bound assignment).

    ``_render_busy`` is a REQUIRED override — JobHost calls it
    unconditionally (``_start_job`` and ``_job_done``) but does not define
    it.  ``log_widget_id`` / ``cancel_button_id`` / ``protect_message`` are
    also required: they have no defaults and ``_protect``/``_log_line``/
    ``_render_busy_cancel`` query them at runtime.
    """

    log_widget_id: ClassVar[str]
    cancel_button_id: ClassVar[str]
    progress_text_id: ClassVar[str]
    protect_message: ClassVar[str]
    cancelled_log: ClassVar[str]
    cancelled_notify: ClassVar[str]
    # Green-log-line markers (styling only; screens narrow/extend as needed).
    green_contains: ClassVar[tuple[str, ...]] = ("[done]", "✓", "→")
    green_prefixes: ClassVar[tuple[str, ...]] = ()

    # ---------- protect / start ----------

    def _protect(self) -> bool:
        """True when a job is already running (block new stage starts)."""
        if self._job is not None:
            self.app.notify(
                self.protect_message.format(stage=self._job["stage"]),
                severity="warning",
            )
            return True
        return False

    def _start_job(
        self,
        stage: str,
        fn: object,
        config_path: Path | None = None,
        *,
        kwargs: dict | None = None,
        total: int | None = None,
    ) -> None:
        if config_path is None:
            config_path = self._config_path()
        if config_path is None:
            return
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
            "kwargs": kwargs or {},
            "config_path": config_path,
            "queue": q,
            "cancel_event": cancel_event,
            "total": total,
            "progress": 0,
            "state": "running",
            "text": f"0/{total}" if total else "running…",
            "dir_name": config_path.parent.name,
        }
        self.state.active_job = stage
        self._log_line(f"▶ {stage} started ({config_path.name})")
        self._render_busy()
        self.focus()  # keep bindings alive while the buttons are disabled
        self.run_worker(
            partial(run_stage_worker, job=self._job),
            thread=True,
            group="stage",
            exclusive=True,
        )

    # ---------- tick / drain ----------

    def _tick(self) -> None:
        """Main-thread queue drain (0.1 s).

        The queue is drained unconditionally — the worker's ``('done', …)``
        marker must always be consumed so ``state.active_job`` is released,
        even when the job belongs to another assignment (the user navigated
        away while it ran; F1). Only UI updates are gated on the dir match.
        """
        job = self._job
        if job is None:
            return
        ours = self._job_is_ours(job)
        q = job["queue"]
        while True:
            try:
                kind, payload = q.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                if ours:
                    self._log_line(payload)
            elif kind == "done":
                self._job_done(payload)
                return
        if ours:
            self.poll_progress(job)

    def _job_is_ours(self, job: dict) -> bool:
        """True when the job targets this screen's current scope.

        Default: any current job handle (PlagiarismScreen — course scope).
        AssignmentScreen overrides with its per-assignment dir match.
        """
        return job is self._job

    def poll_progress(self, job: dict) -> None:
        """Per-tick progress hook (no-op by default)."""

    def _job_done(self, summary: dict | None) -> None:
        job = self._job
        if job is None:
            return
        # F1: the shared active_job slot is released unconditionally — the
        # job may belong to another assignment (user navigated away while
        # it ran). UI updates stay gated on the dir match (_job_is_ours).
        ours = self._job_is_ours(job)
        self.state.active_job = None
        self._job = None
        self._render_busy()
        if summary and ours:
            if summary.get("cancelled"):
                self._log_line(self.cancelled_log)
                self.app.notify(self.cancelled_notify, severity="information")
            else:
                line = format_job_summary(summary)
                if line:
                    self._log_line(line)
                errors = int(summary.get("errors") or 0)
                self.app.notify(
                    line or f"Job {job['stage']} finished",
                    severity="warning" if errors else "success",
                )
        self.job_finished(job, summary)

    def job_finished(self, job: dict, summary: dict | None) -> None:
        """Post-done hook (reload/rescan/focus); overridden per screen."""

    # ---------- busy row ----------

    def _render_busy_cancel(self) -> None:
        """Shared cancel-row state: stop-mode button + progress text."""
        job = self._job
        if job is None:
            return
        cancel = self.query_one(self.cancel_button_id, Button)
        cancel.disabled = job["state"] == "stopping"
        cancel.label = "Stop…" if job["state"] == "stopping" else "Cancel"
        self.query_one(self.progress_text_id, Static).update(
            escape(job.get("text", "running…"))
        )

    # ---------- log ----------

    def _log_line(self, line: str) -> None:
        text = escape(line)
        if line.startswith("[error]") or "✗" in line:
            styled = f"[red]{text}[/red]"
        elif any(m in line for m in self.green_contains) or line.startswith(
            self.green_prefixes
        ):
            styled = f"[green]{text}[/green]"
        else:
            styled = text
        self.query_one(self.log_widget_id, RichLog).write(styled)
