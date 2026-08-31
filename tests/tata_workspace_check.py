"""Runnable headless check for the T4b Assignment workspace.

Drives the real DashboardScreen (App.run_test + Pilot) on a tmp course
layout with a valid assignment config and a partial pipeline state. Covers:
6 stage buttons + incremental subtitles, config panel, [i] summary, grade
confirm modal (open/dismiss/confirm), a mocked stage job (worker thread ->
queue -> RichLog -> rescan), cooperative cancel (x), native '?' help panel,
esc back to Course. The stage function is monkeypatched with a stub — no real
grading/LLM call ever happens.

Run: uv run tests/tata_workspace_check.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path

from e2e_common import make_course, spy_notify, wait_for  # isort: skip - seeds repo-root sys.path before src imports
from src.tui import tata_workspace as tw
from src.tui.score_review import ScoreReviewScreen
from src.tui.tata_app import AliasEditorModal, TataApp
from src.tui.tata_workspace import AssignmentScreen
from textual.pilot import Pilot
from textual.widgets import Button, RichLog

ASSIGNMENT_CFG = (
    "[grading]\n"
    "rubric = 'rubrics/exam.toml'\n"
    "system_prompt = 'prompt/system.md'\n"
    "provider = 'deepseek'\n"
    "max_parallel_tasks = 4\n"
)


def _stage_buttons(app: TataApp) -> dict[str, Button]:
    ws = app.query_one(AssignmentScreen)
    return {
        name: ws.query_one(f"#stage-{name}", Button)
        for name in ("fetch", "preprocess", "grade", "score", "analyze", "score_review")
    }


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


async def _check_buttons_and_panel(app: TataApp, pilot: Pilot) -> None:
    ws = app.query_one(AssignmentScreen)
    buttons = _stage_buttons(app)

    assert len(buttons) == 6
    assert str(buttons["fetch"].label).startswith("fetch\n")
    assert "2/2 done" in str(buttons["preprocess"].label), buttons["preprocess"].label
    assert "1 pending · 1 done" in str(buttons["grade"].label), buttons["grade"].label
    assert "1/1 scored" in str(buttons["score"].label), buttons["score"].label
    assert "Not run" in str(buttons["analyze"].label), buttons["analyze"].label
    assert str(buttons["score_review"].label).startswith("score review\n"), buttons[
        "score_review"
    ].label
    # no _sub entry -> single-'…' subtitle fallback
    assert str(buttons["score_review"].label).rstrip().endswith("…"), buttons[
        "score_review"
    ].label
    assert not ws.query("#stage-plagiarism"), "plagiarism button should be gone"
    assert not hasattr(ws, "action_run_plagiarism")

    body = ws.query_one("#config-body")
    text = str(body.content)
    assert "provider" in text, text
    assert "deepseek" in text, text
    assert "max_parallel" in text, text
    assert "4" in text, text

    incr = ws.query_one("#ws-incr")
    assert not incr.display
    await pilot.press("i")
    await pilot.pause()
    assert incr.display
    line = str(incr.content)
    assert "To run:" in line, line
    assert "Skip" not in line, (
        line
    )  # F8: formerly double-counted (processed+done+scored)
    assert "No change:" in line, line
    await pilot.press("i")


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
    def fake_grade(config_path: Path, *, force: bool = False) -> dict:
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
    ws = app.query_one(AssignmentScreen)
    log = ws.query_one("#richlog", RichLog)

    def slow_grade(config_path: Path, *, force: bool = False) -> dict:
        time.sleep(2.0)
        return {
            "stage": "grading",
            "success": 1,
            "errors": 0,
            "total": 1,
            "success_rate": 100.0,
        }

    tw.grade_assignment = slow_grade
    await pilot.press("g")
    await _wait_modal_focused(app, pilot, "normal")
    await pilot.press("enter")
    await wait_for(pilot, lambda: ws._job is not None)
    await pilot.press("x")
    await pilot.pause()
    assert ws._job["state"] == "stopping"  # cancel_event set, UI in Stopping
    await wait_for(pilot, lambda: ws._job is None)
    lines = [str(line) for line in log.lines]
    assert any("Cancel requested" in line for line in lines), lines


async def _check_button_click(app: TataApp, pilot: Pilot) -> None:
    """F2/F3: mouse path — click stage button opens modal; click cancel works."""
    ws = app.query_one(AssignmentScreen)
    log = ws.query_one("#richlog", RichLog)

    def slow_grade(config_path: Path, *, force: bool = False) -> dict:
        time.sleep(2.0)
        return {
            "stage": "grading",
            "success": 1,
            "errors": 0,
            "total": 1,
            "success_rate": 100.0,
        }

    tw.grade_assignment = slow_grade
    await pilot.click("#stage-grade")
    await pilot.pause()
    assert isinstance(ws.app.screen, tw.ConfirmationModal), ws.app.screen
    await pilot.press("escape")
    await wait_for(pilot, lambda: not isinstance(ws.app.screen, tw.ConfirmationModal))

    await pilot.click("#stage-grade")
    await _wait_modal_focused(app, pilot, "normal")
    await pilot.press("enter")
    await wait_for(pilot, lambda: ws._job is not None)
    await pilot.click("#ws-cancel")
    await pilot.pause()
    assert ws._job["state"] == "stopping"
    await wait_for(pilot, lambda: ws._job is None)
    lines = [str(line) for line in log.lines]
    assert any("Cancel requested" in line for line in lines), lines


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
    """Empty graded/ -> notify, no push."""
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
    await _check_buttons_and_panel(app, pilot)
    await _check_grade_modal(app, pilot)
    await _check_cancel(app, pilot)
    await _check_button_click(app, pilot)
    await _check_editor_warning(app, pilot)
    await _check_fetch_gate(app, pilot)
    await _check_score_review(app, pilot)
    await _check_analyze_key(app, pilot)
    await _check_help_and_back(app, pilot)


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
