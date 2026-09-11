"""Runnable headless check for the T4b Assignment workspace.

Drives the real DashboardScreen (App.run_test + Pilot) on a tmp course
layout with a valid assignment config and a partial pipeline state. Covers:
6 stage buttons with icon labels + visible subtitles (content region AND
SVG text), the always-visible #ws-status pending row (fixture-consistent
counts, D8 colours, refresh on rescan/re-entry), the removal of the old
'Pipeline' prefix / 'incremental' toggle, config panel, grade confirm modal
(open/dismiss/confirm), a mocked stage job (worker thread -> queue ->
RichLog -> rescan), cooperative cancel (x) with a polling stub — the job
really exits early and the cancelled log line lands (v10 batch 2), the
progress row under the log with a stretched inner Bar, native ETA and the
elapsed clock (v10 batch 2), native '?' help panel, esc back to Course, a
100x30 short-window layout check that PROVES the log really paints (content
row + 'Live log' + a written line in the exported SVG) and that the
mid-job progress row fits below it, and a 120x44 tall-window no-regression
guard. The stage function is monkeypatched with a stub — no real
grading/LLM call ever happens.

Run: uv run tests/tata_workspace_check.py
"""

from __future__ import annotations

import asyncio
import html
import os
import queue
import re
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from e2e_common import COURSE, make_course, spy_notify, wait_for  # isort: skip - seeds repo-root sys.path before src imports
from rich.text import Text as RichText
from src.shared.assignment_config import load_assignment_file
from src.shared.caching import cache_file, save_cache_file
from src.shared.grading import (
    grading_pending,
    load_assignment_config,
    pending_grade_submissions,
)
from src.shared.processing import pending_preprocess_items
from src.tui import icons, workspace as tw
from src.tui.app import AliasEditorModal, TataApp
from src.tui.score_review import ScoreReviewScreen
from src.tui.workspace import AssignmentScreen
from textual.containers import Horizontal
from textual.pilot import Pilot
from textual.widgets import Button, ProgressBar, RichLog, Static

ZERO_GRADE = {
    "stage": "grading",
    "success": 0,
    "errors": 0,
    "total": 0,
    "success_rate": 0.0,
}


def _polling_grade(seconds: float) -> tuple[Callable[..., dict], dict]:
    """Sleeping stage stub that honors cancel_event (v10 batch 2).

    Returns (fn, state); ``state["exited_early"]`` flips when the stub
    observed the set event before the full sleep (proves cooperative exit,
    not just an early done marker).
    """
    state = {"exited_early": False}

    def stub(
        config_path: Path,
        *,
        force: bool = False,
        cancel_event: object | None = None,
    ) -> dict:
        steps = max(1, int(seconds / 0.05))
        for _ in range(steps):
            if cancel_event is not None and cancel_event.is_set():  # type: ignore[attr-defined]
                state["exited_early"] = True
                return dict(ZERO_GRADE)
            time.sleep(0.05)
        print("[done] 100001")
        return {
            "stage": "grading",
            "success": 1,
            "errors": 0,
            "total": 1,
            "success_rate": 100.0,
        }

    return stub, state


ASSIGNMENT_CFG = (
    "[grading]\n"
    "rubric = 'rubrics/exam.toml'\n"
    "system_prompt = 'prompt/system.md'\n"
    "provider = 'deepseek'\n"
    "max_parallel_tasks = 4\n"
)


def _seed_grade_cache(data_root: Path) -> None:
    """Fixture state the grading hash-cache rule needs (the grade subtitle
    follows ``<assignment>/.cache/grading.json``; values come from the rule
    itself via grading_pending).

    The [grading] config references rubrics/exam.toml + prompt/system.md;
    without them the pending lookup raises -> 0 and the grade button shows
    "2/2 done". Write those files plus a cache entry marking 100001 done,
    so the workspace renders the intended partial state "1 pending · 1
    done".
    """
    (data_root / "rubrics").mkdir(exist_ok=True)
    (data_root / "rubrics" / "exam.toml").write_text(
        '[[criterion]]\nname = "C1"\ndesc = "d"\npts = 10\n'
        'rating = "binary"\ngrading = "standard"\n',
        encoding="utf-8",
    )
    (data_root / "prompt").mkdir(exist_ok=True)
    (data_root / "prompt" / "system.md").write_text("You are a TA.\n", encoding="utf-8")
    a1 = data_root / COURSE / "a1"
    cfg = load_assignment_config(a1 / "config.toml")
    cfg_model = load_assignment_file(a1 / "config.toml")
    _pending, hashes = grading_pending(cfg, cfg_model)
    save_cache_file(
        cache_file(a1, "grading"),
        {stem: {"hash": h} for stem, h in hashes.items() if stem == "100001"},
    )


def _stage_buttons(app: TataApp) -> dict[str, Button]:
    ws = app.query_one(AssignmentScreen)
    return {
        name: ws.query_one(f"#stage-{name}", Button)
        for name in ("fetch", "preprocess", "grade", "score", "analyze", "score_review")
    }


def _plain(widget: Static) -> RichText:
    """Display text of a markup Static (content holds the markup source)."""
    return RichText.from_markup(str(widget.content))


def _span_style(text: RichText, needle: str) -> str:
    """Style tag of the span covering ``needle`` ('' when unstyled)."""
    start = text.plain.index(needle)
    end = start + len(needle)
    for span in text.spans:
        if span.start <= start and end <= span.end:
            return str(span.style)
    return ""


def _svg_plain(svg: str) -> str:
    """Plain text of an exported SVG: join the text runs (Textual splits a
    line into runs) then unescape entities (spaces are &#160;)."""
    joined = "".join(re.findall(r"<text[^>]*>(.*?)</text>", svg, re.DOTALL))
    return html.unescape(joined).replace("\xa0", " ")


async def _enter_assignment(app: TataApp, pilot: Pilot) -> None:
    table = app.query_one("#dashboard-table")
    await wait_for(pilot, lambda: table.row_count == 1)
    await pilot.press("enter")
    await pilot.pause()
    assert app.state.dashboard_level == "course"
    await pilot.press("enter")
    await pilot.pause()
    assert app.state.dashboard_level == "assignment"
    await wait_for(pilot, lambda: app.query_one(AssignmentScreen).display)


def _check_buttons_and_panel(app: TataApp) -> None:
    ws = app.query_one(AssignmentScreen)
    buttons = _stage_buttons(app)

    assert len(buttons) == 6
    assert str(buttons["fetch"].label).startswith(f"{icons.FETCH} fetch\n")
    assert "2/2 done" in str(buttons["preprocess"].label), buttons["preprocess"].label
    assert "1 pending · 1 done" in str(buttons["grade"].label), buttons["grade"].label
    assert "1/1 scored" in str(buttons["score"].label), buttons["score"].label
    assert "Not run" in str(buttons["analyze"].label), buttons["analyze"].label
    # score review now carries a real subtitle, never the '…' fallback
    assert str(buttons["score_review"].label) == (
        f"{icons.REVIEW} score review\nview scores"
    ), buttons["score_review"].label
    assert not ws.query("#stage-plagiarism"), "plagiarism button should be gone"
    assert not hasattr(ws, "action_run_plagiarism")

    body = ws.query_one("#config-body")
    text = str(body.content)
    assert "provider" in text, text
    assert "deepseek" in text, text
    assert "max_parallel" in text, text
    assert "4" in text, text


async def _check_status_row(app: TataApp, pilot: Pilot) -> None:
    """#ws-status: always visible; counts straight from the shared rules
    (never re-implement them here); D8 colours (>0 yellow, 0 green, absent
    dim)."""
    ws = app.query_one(AssignmentScreen)
    info = ws._info
    assert info is not None
    cfg = info.config_path
    a_dir = cfg.parent
    status = ws.query_one("#ws-status", Static)
    await wait_for(pilot, lambda: status.region.height >= 1)
    assert status.display, "status row must be always visible"

    pre = len(pending_preprocess_items(cfg))
    grade = len(pending_grade_submissions(cfg))
    score = max(info.counts.graded - info.counts.scored, 0)

    st = _plain(status)
    assert f"{icons.OK} fetch" in st.plain, st.plain
    assert f"{pre} preprocess pending" in st.plain, st.plain
    assert f"{grade} grade pending" in st.plain, st.plain
    assert f"{score} score pending" in st.plain, st.plain
    assert "analyze Not run" in st.plain, st.plain  # fixture: no meta_analysis.json

    assert _span_style(st, f"{icons.OK} fetch") == "green"
    assert _span_style(st, f"{pre} preprocess pending") == (
        "yellow" if pre > 0 else "green"
    )
    assert _span_style(st, f"{grade} grade pending") == (
        "yellow" if grade > 0 else "green"
    )
    assert _span_style(st, f"{score} score pending") == (
        "green" if score == 0 else "yellow"
    )
    assert _span_style(st, "analyze Not run") == "dim"

    # fetch state comes from the fetch cache, same rule as the subtitle
    assert cache_file(a_dir, "fetch").is_file()


async def _check_concepts_removed(app: TataApp, pilot: Pilot) -> None:
    """Feedback 3: the 'Pipeline' prefix and the 'incremental' concept are
    gone from bindings and from every rendered string.

    The Footer renders the focused widget's bindings, so the exported SVG
    (composited screen) also covers the key-hint strip.
    """
    ws = app.query_one(AssignmentScreen)
    for binding in ws.BINDINGS:
        assert binding.key != "i", binding
        assert "incremental" not in str(binding.description).lower(), binding
    for widget in ws.query(Static):
        widget_text = _plain(widget).plain.lower()
        assert "pipeline" not in widget_text, (widget.id, widget_text)
        assert "incremental" not in widget_text, (widget.id, widget_text)
    ws.focus()
    await pilot.pause()
    svg = _svg_plain(app.export_screenshot()).lower()
    assert "pipeline" not in svg, "the 'Pipeline' prefix must be gone"
    assert "incremental" not in svg, "the incremental hint/binding must be gone"


async def _check_subtitles_visible(app: TataApp, pilot: Pilot) -> None:
    """Stage-button subtitles really render: content region has both lines
    AND the subtitle strings appear in the exported SVG (joined runs)."""
    buttons = _stage_buttons(app)
    await wait_for(
        pilot,
        lambda: all(btn.content_region.height >= 2 for btn in buttons.values()),
    )
    for name, btn in buttons.items():
        assert btn.content_region.height >= 2, (name, btn.content_region)
        lines = str(btn.label).split("\n")
        assert len(lines) == 2, (name, str(btn.label))
        assert lines[1], (name, lines[1])
        assert lines[1] != "…", (name, lines[1])
    # v10 item 2: the subtitle is dim markup inside the label (the main line
    # keeps the normal button colour); the span covers the subtitle text.
    fetch_spans = buttons["fetch"].label.spans
    assert any(str(span.style) == "dim" for span in fetch_spans), fetch_spans
    svg = _svg_plain(app.export_screenshot())
    for name, btn in buttons.items():
        subtitle = str(btn.label).split("\n", 1)[1]
        assert subtitle in svg, (name, subtitle)


async def _check_status_refresh(app: TataApp, pilot: Pilot) -> None:
    """#ws-status recomputes on rescan (`r`) and on level re-entry."""
    ws = app.query_one(AssignmentScreen)
    info = ws._info
    assert info is not None
    status = ws.query_one("#ws-status", Static)
    meta = info.config_path.parent / "logs" / "meta_analysis.json"
    meta.write_text("{}", encoding="utf-8")
    try:
        await pilot.press("r")
        await wait_for(pilot, lambda: "analyze OK" in _plain(status).plain)
        assert _span_style(_plain(status), "analyze OK") == "green"
    finally:
        meta.unlink()
    # leave the level and re-enter: render_all runs again from fixture state
    ws.focus()
    await pilot.press("escape")
    await wait_for(pilot, lambda: app.state.dashboard_level == "course")
    await wait_for(pilot, lambda: app.query_one("#dashboard-table").row_count == 1)
    await pilot.press("enter")
    await wait_for(pilot, lambda: app.state.dashboard_level == "assignment")
    await wait_for(pilot, lambda: "analyze Not run" in _plain(status).plain)
    assert _span_style(_plain(status), "analyze Not run") == "dim"


async def _wait_modal_focused(app: TataApp, pilot: Pilot, button_id: str) -> None:
    """Wait until the ConfirmationModal is up AND its first button is focused
    (enter would otherwise be swallowed by the widget focused below)."""
    await wait_for(
        pilot,
        lambda: (
            isinstance(app.screen, tw.ConfirmationModal)
            and app.screen.query_one(f"Button#{button_id}").has_focus
        ),
    )


async def _check_grade_modal(app: TataApp, pilot: Pilot) -> None:
    """Open modal, dismiss with escape (no job), confirm with enter (job)."""
    ws = app.query_one(AssignmentScreen)
    await pilot.press("g")
    await _wait_modal_focused(app, pilot, "normal")
    await pilot.press("escape")
    await wait_for(pilot, lambda: not isinstance(ws.app.screen, tw.ConfirmationModal))
    assert ws._job is None

    # slow stub so the check can observe the running JobHandle; no real grading
    def fake_grade(
        config_path: Path,
        *,
        force: bool = False,
        cancel_event: object | None = None,
    ) -> dict:
        time.sleep(0.4)
        print("[done] 100001")
        return {
            "stage": "grading",
            "success": 1,
            "errors": 0,
            "total": 1,
            "success_rate": 100.0,
        }

    tw.grade_assignment = fake_grade
    await pilot.press("g")
    await _wait_modal_focused(app, pilot, "normal")
    await pilot.press("enter")
    # The job may start AND finish inside a single pilot.pause (the worker is
    # a thread, so the running _job window is transient); assert the
    # observable outcome instead — the job summary line in the log.
    log = ws.query_one("#richlog", RichLog)
    await wait_for(
        pilot,
        lambda: any("[grading]" in str(line) for line in log.lines),
    )
    lines = [str(line) for line in log.lines]
    assert any("[grading]" in line for line in lines), lines


async def _check_cancel(app: TataApp, pilot: Pilot) -> None:
    """Cooperative cancel (v10 batch 2): the stub really observes the event
    and exits early, the job slot releases well before the stub's full
    sleep, and the cancelled log line lands."""
    ws = app.query_one(AssignmentScreen)
    log = ws.query_one("#richlog", RichLog)

    stub, state = _polling_grade(2.0)
    tw.grade_assignment = stub
    await pilot.press("g")
    await _wait_modal_focused(app, pilot, "normal")
    await pilot.press("enter")
    await wait_for(pilot, lambda: ws._job is not None)
    t0 = time.monotonic()
    await pilot.press("x")
    await pilot.pause()
    # the worker may already have exited within one poll interval
    if ws._job is not None:
        assert ws._job["state"] == "stopping", ws._job  # cancel set, UI stopping
    await wait_for(pilot, lambda: ws._job is None)
    release = time.monotonic() - t0
    assert release < 1.0, release  # 2.0 s stub: early exit, not a full sleep
    assert state["exited_early"], "the stub never observed the cancel event"
    lines = [str(line) for line in log.lines]
    assert any(
        "Cancel requested — queued items dropped, in-flight item finishes" in line
        for line in lines
    ), lines
    assert any(
        "Job cancelled — progress saved (cache based)" in line for line in lines
    ), lines


async def _check_button_click(app: TataApp, pilot: Pilot) -> None:
    """F2/F3: mouse path — click stage button opens modal; click cancel works."""
    ws = app.query_one(AssignmentScreen)
    log = ws.query_one("#richlog", RichLog)

    stub, state = _polling_grade(2.0)
    tw.grade_assignment = stub
    await pilot.click("#stage-grade")
    await pilot.pause()
    assert isinstance(ws.app.screen, tw.ConfirmationModal), ws.app.screen
    await pilot.press("escape")
    await wait_for(pilot, lambda: not isinstance(ws.app.screen, tw.ConfirmationModal))

    await pilot.click("#stage-grade")
    await _wait_modal_focused(app, pilot, "normal")
    await pilot.press("enter")
    await wait_for(pilot, lambda: ws._job is not None)
    t0 = time.monotonic()
    await pilot.click("#ws-cancel")
    await pilot.pause()
    if ws._job is not None:
        assert ws._job["state"] == "stopping", ws._job
    await wait_for(pilot, lambda: ws._job is None)
    assert time.monotonic() - t0 < 1.0
    assert state["exited_early"], "the stub never observed the cancel event"
    lines = [str(line) for line in log.lines]
    assert any("Cancel requested" in line for line in lines), lines


async def _check_progress_row(app: TataApp, pilot: Pilot) -> None:
    """v10 batch 2: the progress row renders below the log, its inner Bar
    stretches past Textual's 32-cell default, the native ETA status exists,
    and #ws-progress-text carries a ticking elapsed clock (sleeping stub)."""
    ws = app.query_one(AssignmentScreen)
    progress = ws.query_one("#ws-progress", Horizontal)
    log = ws.query_one("#richlog", RichLog)
    bar = ws.query_one("#ws-progress > ProgressBar", ProgressBar)
    text = ws.query_one("#ws-progress-text", Static)
    assert bar.show_eta is True

    stub, state = _polling_grade(3.0)
    tw.grade_assignment = stub
    await pilot.press("g")
    await _wait_modal_focused(app, pilot, "normal")
    await pilot.press("enter")
    await wait_for(pilot, lambda: ws._job is not None)

    await wait_for(pilot, lambda: progress.region.y > log.region.y)
    assert progress.region.y + progress.region.height <= ws.screen.size.height
    inner = bar.query_one("Bar")
    await wait_for(pilot, lambda: inner.size.width > 32)
    assert bar.query_one("ETAStatus") is not None  # native ETA present
    await wait_for(
        pilot, lambda: bool(re.search(r" · \d{2}:\d{2}$", str(text.content)))
    )
    stamped = str(text.content)
    # the elapsed clock repaints on the integer-second change (~1 s)
    await wait_for(pilot, lambda: str(text.content) != stamped, timeout=5)

    await pilot.press("x")
    await wait_for(pilot, lambda: ws._job is None)
    assert state["exited_early"]


async def _check_foreign_elapsed(app: TataApp, pilot: Pilot) -> None:
    """C4 (round 3): the busy-row stopwatch is not ours-gated. A job dict for
    another assignment (the user switched away mid-job) still advances
    elapsed_tick and repaints the clock text when _tick runs."""
    ws = app.query_one(AssignmentScreen)
    job = {
        "stage": "grade",
        "queue": queue.Queue(),
        "dir_name": "a-foreign",
        "started_at": time.monotonic() - 2.0,
        "elapsed_tick": 7,
        "state": "running",
        "text": "1/2",
        "total": 2,
    }
    assert not ws._job_is_ours(job)  # dir_name points at another assignment
    ws._job = job
    ws._render_busy()
    try:
        ws._tick()
        assert job["elapsed_tick"] == 2, job  # stale 7 replaced, not frozen
        text = ws.query_one("#ws-progress-text", Static)
        assert str(text.content) == "1/2 · 00:02", text.content
    finally:
        ws._job = None
        ws._render_busy()
        ws.focus_stage()  # the same seat focus_stage gives back after a job
    await pilot.pause()


async def _check_editor_warning(app: TataApp, pilot: Pilot) -> None:
    """F5: e with EDITOR unset -> warning notify (no fake 'Config reloaded')."""
    notices, orig_notify = spy_notify(app)
    old_editor = os.environ.pop("EDITOR", None)
    try:
        await pilot.press("e")
        await pilot.pause()
        assert any("EDITOR" in msg for msg, _sev in notices), notices
        assert any(sev == "warning" for _msg, sev in notices), notices
        assert not any("Config reloaded" in msg for msg, _sev in notices), notices
    finally:
        if old_editor is not None:
            os.environ["EDITOR"] = old_editor
        app.notify = orig_notify


async def _check_fetch_gate(app: TataApp, pilot: Pilot) -> None:
    """F1: fetch with no course [fetch] course_id -> error notify, no job."""
    ws = app.query_one(AssignmentScreen)
    cfg_path = ws._info.config_path
    course_cfg = cfg_path.parent.parent / "config.toml"
    original = cfg_path.read_text(encoding="utf-8")
    original_course = course_cfg.read_text(encoding="utf-8")
    notices, orig_notify = spy_notify(app)
    try:
        cfg_path.write_text(ASSIGNMENT_CFG, encoding="utf-8")
        # No [fetch] in ANY layer -> merged fetch section is empty.
        course_cfg.write_text("", encoding="utf-8")
        await pilot.press("f")
        await pilot.pause()
        assert ws._job is None, ws._job
        assert any("Course not configured for fetch" in msg for msg, _sev in notices), (
            notices
        )
        assert any(sev == "error" for _msg, sev in notices), notices
        assert not any("started" in msg for msg, _sev in notices), notices
    finally:
        cfg_path.write_text(original, encoding="utf-8")
        course_cfg.write_text(original_course, encoding="utf-8")
        app.notify = orig_notify


async def _check_score_review(app: TataApp, pilot: Pilot) -> None:
    """Click #stage-score_review -> ScoreReviewScreen pushed; esc pops back."""
    await pilot.click("#stage-score_review")
    await pilot.pause()
    assert isinstance(app.screen, ScoreReviewScreen), type(app.screen)
    assert len(app.screen.students) > 0
    await pilot.press("escape")
    await pilot.pause()
    assert not isinstance(app.screen, ScoreReviewScreen), type(app.screen)
    assert len(app.screen_stack) == 1
    assert app.query_one(AssignmentScreen).display


async def _check_score_review_empty(app: TataApp, pilot: Pilot) -> None:
    """Empty graded/ -> notify, no push; fresh fixture status says not fetched."""
    ws = app.query_one(AssignmentScreen)
    status = ws.query_one("#ws-status", Static)
    st = _plain(status)
    assert "not fetched" in st.plain, st.plain
    assert _span_style(st, "not fetched") == "dim"
    notices, orig_notify = spy_notify(app)
    try:
        await pilot.click("#stage-score_review")
        await pilot.pause()
        assert not isinstance(app.screen, ScoreReviewScreen), type(app.screen)
        assert len(app.screen_stack) == 1
        assert any("No graded files" in msg for msg, _sev in notices), notices
    finally:
        app.notify = orig_notify


async def _check_analyze_key(app: TataApp, pilot: Pilot) -> None:
    """`a` is Analyze at the workspace level: a mocked analyze job runs and
    NO alias modal opens (the dashboard's `a`=Aliases must not capture it)."""
    ws = app.query_one(AssignmentScreen)
    calls: list[Path] = []
    orig = tw.analyze_assignment

    def fake_analyze(config_path: Path, **kwargs: object) -> dict:
        calls.append(config_path)
        print("[done] 100001")
        return {
            "stage": "analysis",
            "success": 1,
            "errors": 0,
            "total": 1,
            "success_rate": 100.0,
        }

    tw.analyze_assignment = fake_analyze
    try:
        await pilot.press("a")
        await wait_for(pilot, lambda: len(calls) > 0)
        await wait_for(pilot, lambda: ws._job is None)
        assert not isinstance(app.screen, AliasEditorModal), type(app.screen)
    finally:
        tw.analyze_assignment = orig


async def _check_help_and_back(app: TataApp, pilot: Pilot) -> None:
    from textual.widgets import HelpPanel

    ws = app.query_one(AssignmentScreen)
    await pilot.press("?")
    await pilot.pause()
    assert app.screen.query(HelpPanel), "native HelpPanel not mounted"
    assert not app.screen.query(".confirm-modal"), "no custom HelpModal expected"
    # toggle: the second '?' closes the panel
    await pilot.press("?")
    await pilot.pause()
    assert not app.screen.query(HelpPanel), "toggle must close the panel"
    ws.focus()
    await pilot.press("escape")
    await pilot.pause()
    assert app.state.dashboard_level == "course", app.state.dashboard_level


async def check_workspace(app: TataApp, pilot: Pilot) -> None:
    """Full UI-flow walk for the workspace (see module docstring)."""
    await _enter_assignment(app, pilot)
    _check_buttons_and_panel(app)
    await _check_status_row(app, pilot)
    await _check_concepts_removed(app, pilot)
    await _check_subtitles_visible(app, pilot)
    await _check_status_refresh(app, pilot)
    await _check_grade_modal(app, pilot)
    await _check_cancel(app, pilot)
    await _check_button_click(app, pilot)
    await _check_progress_row(app, pilot)
    await _check_foreign_elapsed(app, pilot)
    await _check_editor_warning(app, pilot)
    await _check_fetch_gate(app, pilot)
    await _check_score_review(app, pilot)
    await _check_analyze_key(app, pilot)
    await _check_help_and_back(app, pilot)


async def _check_short_window(app: TataApp, pilot: Pilot) -> None:
    """100x30: grid keeps auto height (no squashed buttons), the 1fr log
    paints REAL content (>= 1 content row: region >= 3 = both border rows;
    the old region-only assertion passed a collapsed log), still fits on
    screen, subtitles survive the squeeze, and the mid-job progress row
    (v10 batch 2) sits below the log without clipping."""
    ws = app.query_one(AssignmentScreen)
    buttons = _stage_buttons(app)
    await wait_for(
        pilot,
        lambda: all(btn.content_region.height >= 2 for btn in buttons.values()),
    )
    for name, btn in buttons.items():
        assert btn.region.height == 4, (name, btn.region)  # measured at 100x30/120x40
        assert btn.content_region.height >= 2, (name, btn.content_region)
    status = ws.query_one("#ws-status", Static)
    assert status.region.height >= 1, status.region
    log = ws.query_one("#richlog", RichLog)
    await wait_for(pilot, lambda: log.size.height >= 1)
    assert log.region.height >= 3, log.region  # 2 border rows + >= 1 content row
    assert log.size.height >= 1, log.size  # REAL visibility at 100x30 (T6)
    assert log.region.y + log.region.height <= ws.screen.size.height, (
        log.region,
        ws.screen.size,
    )
    probe_line = "short-window probe: log paints"
    log.write(probe_line)
    await pilot.pause()
    svg = _svg_plain(app.export_screenshot())
    assert "Live log" in svg, "the log border title must paint at 100x30"
    assert probe_line in svg, "a written log line must be visible at 100x30"
    for name, btn in buttons.items():
        subtitle = str(btn.label).split("\n", 1)[1]
        assert subtitle in svg, (name, subtitle)

    # v10 batch 2: mid-job the progress row must sit BELOW the log and the
    # whole stack must still fit 100x30 (no clipping). The 3-row bar row
    # (Cancel button height) costs the 1fr log its content row at this
    # shortest window — the frame stays (both border rows).
    progress = ws.query_one("#ws-progress", Horizontal)
    stub, state = _polling_grade(2.0)
    tw.grade_assignment = stub
    await pilot.press("g")
    await _wait_modal_focused(app, pilot, "normal")
    await pilot.press("enter")
    await wait_for(pilot, lambda: ws._job is not None)
    await wait_for(pilot, lambda: progress.display)
    assert progress.region.y > log.region.y, (progress.region, log.region)
    assert progress.region.y + progress.region.height <= ws.screen.size.height, (
        progress.region,
        ws.screen.size,
    )
    assert progress.region.height >= 1, progress.region
    assert log.region.height >= 2, log.region  # frame intact (see docstring)
    await pilot.press("x")
    await wait_for(pilot, lambda: ws._job is None)
    assert state["exited_early"]


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_course(
            root / "data",
            assignment_cfg=ASSIGNMENT_CFG,
            graded="first",
            processed=["100001", "100002"],
            scored=True,
            fetch_cache=True,
            logs=True,
            pairs="full",
            env=True,
        )
        _seed_grade_cache(root / "data")
        tw.grade_assignment = lambda config_path, **kwargs: {
            "stage": "grading",
            "success": 1,
            "errors": 0,
            "total": 1,
            "success_rate": 100.0,
        }
        app = TataApp(root_dir=root)
        async with app.run_test(size=(120, 40)) as pilot:
            await check_workspace(app, pilot)
        # short window: layout sanity (grid auto, log 1fr, subtitles visible)
        app_short = TataApp(root_dir=root)
        async with app_short.run_test(size=(100, 30)) as pilot_short:
            await _enter_assignment(app_short, pilot_short)
            await _check_short_window(app_short, pilot_short)
        # tall window: the 1fr log keeps at least its pre-reclaim content
        # height (measured 10 rows at 120x44 before the short-window fix)
        app_tall = TataApp(root_dir=root)
        async with app_tall.run_test(size=(120, 44)) as pilot_tall:
            await _enter_assignment(app_tall, pilot_tall)
            log_tall = app_tall.query_one(AssignmentScreen).query_one(
                "#richlog", RichLog
            )
            await wait_for(pilot_tall, lambda: log_tall.size.height >= 10)
            assert log_tall.size.height >= 10, log_tall.size
        # empty-graded guard: fresh root with a graded/ dir but no *.json
        empty_root = root / "empty"
        make_course(
            empty_root / "data",
            assignment_cfg=ASSIGNMENT_CFG,
            env=True,
        )
        app2 = TataApp(root_dir=empty_root)
        async with app2.run_test(size=(120, 40)) as pilot2:
            await _enter_assignment(app2, pilot2)
            await _check_score_review_empty(app2, pilot2)
    print("tata_workspace check OK")


if __name__ == "__main__":
    asyncio.run(main())
