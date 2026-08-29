"""Runnable headless check for the T4b Assignment workspace.

Drives the real DashboardScreen (App.run_test + Pilot) on a tmp course
layout with a valid assignment config and a partial pipeline state. Covers:
6 stage buttons + incremental subtitles, config panel, [i] summary, grade
confirm modal (open/dismiss/confirm), a mocked stage job (worker thread ->
queue -> RichLog -> rescan), cooperative cancel (x), help modal, esc back to
Course. The stage function is monkeypatched with a stub — no real
grading/LLM call ever happens.

Run: uv run tests/tata_workspace_check.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.tata_workspace as tw
from src.tata_app import TataApp
from src.tata_workspace import AssignmentScreen
from textual.pilot import Pilot
from textual.widgets import Button, RichLog

COURSE = "c1-first"


def _make_assignment(assignments_dir: Path) -> None:
    course_dir = assignments_dir / COURSE
    course_dir.mkdir(parents=True)
    (course_dir / "config.toml").write_text(
        "[fetch]\ncourse_id = 271218\n", encoding="utf-8"
    )
    a_dir = course_dir / "a1"
    for sub in ("raw", "processed", "graded", "scored", "logs", "plagiarism"):
        (a_dir / sub).mkdir(parents=True)
    (a_dir / "config.toml").write_text(
        "[fetch]\nassignment_id = 1001\n"
        "[grading]\nrubric = 'rubrics/exam.toml'\n"
        "system_prompt = 'prompt/system.md'\n"
        "provider = 'deepseek'\n"
        "max_parallel_tasks = 4\n",
        encoding="utf-8",
    )
    (a_dir / "raw" / "100572.ipynb").write_text("{}", encoding="utf-8")
    (a_dir / "raw" / "201818.txt").write_text("hi", encoding="utf-8")
    (a_dir / "raw" / ".fetch-cache.json").write_text("{}", encoding="utf-8")
    (a_dir / "processed" / "100572.md").write_text("# p", encoding="utf-8")
    (a_dir / "processed" / "201818.md").write_text("# q", encoding="utf-8")
    (a_dir / "graded" / "100572.json").write_text(
        json.dumps({"task1": {"rating": "correct"}}), encoding="utf-8"
    )
    (a_dir / "scored" / "100572.txt").write_text(
        "Total Score: 15.0/25.0", encoding="utf-8"
    )
    (a_dir / "logs" / "grading.checkpoint.json").write_text(
        json.dumps({"done": ["100572"]}), encoding="utf-8"
    )
    (a_dir / "plagiarism" / "all_pairs.json").write_text(
        json.dumps({
            "version": 1,
            "pair_count": 1,
            "pairs": [
                {
                    "test_file": "x.py",
                    "reference_file": "y.py",
                    "max_similarity_pct": 95.0,
                }
            ],
        }),
        encoding="utf-8",
    )
    (assignments_dir.parent / ".env").write_text(
        "CANVAS_BASE_URL=https://canvas.example.edu\nCANVAS_ACCESS_TOKEN=tok\n",
        encoding="utf-8",
    )


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], timeout: float = 30.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause()
        if predicate():
            return
        await asyncio.sleep(0.02)
    message = "timeout waiting for predicate"
    raise AssertionError(message)


def _stage_buttons(app: TataApp) -> dict[str, Button]:
    ws = app.query_one(AssignmentScreen)
    return {
        name: ws.query_one(f"#stage-{name}", Button)
        for name in ("fetch", "preprocess", "grade", "score", "plagiarism", "analyze")
    }


async def _enter_assignment(app: TataApp, pilot: Pilot) -> None:
    table = app.query_one("#dashboard-table")
    await _wait_for(pilot, lambda: table.row_count == 1)
    await pilot.press("enter")
    await pilot.pause()
    assert app.state.dashboard_level == "course"
    await pilot.press("enter")
    await pilot.pause()
    assert app.state.dashboard_level == "assignment"
    await _wait_for(pilot, lambda: app.query_one(AssignmentScreen).display)


async def _check_buttons_and_panel(app: TataApp, pilot: Pilot) -> None:
    ws = app.query_one(AssignmentScreen)
    buttons = _stage_buttons(app)

    assert len(buttons) == 6
    assert str(buttons["fetch"].label).startswith("fetch\n")
    assert "2/2 done" in str(buttons["preprocess"].label), buttons["preprocess"].label
    assert "1 pending · 1 done" in str(buttons["grade"].label), buttons["grade"].label
    assert "1/1 scored" in str(buttons["score"].label), buttons["score"].label
    assert "1 pair (1 flag)" in str(buttons["plagiarism"].label), buttons[
        "plagiarism"
    ].label
    assert "Not run" in str(buttons["analyze"].label), buttons["analyze"].label

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
    assert "Skip" not in line, line  # F8: formerly double-counted (processed+done+scored)
    assert "No change:" in line, line
    await pilot.press("i")


async def _check_grade_modal(app: TataApp, pilot: Pilot) -> None:
    """Open modal, dismiss with escape (no job), confirm with enter (job)."""
    ws = app.query_one(AssignmentScreen)
    await pilot.press("g")
    await pilot.pause()
    modal = ws.app.screen
    assert isinstance(modal, tw.ConfirmationModal), modal
    await pilot.press("escape")
    await _wait_for(pilot, lambda: not isinstance(ws.app.screen, tw.ConfirmationModal))
    assert ws._job is None

    # slow stub so the check can observe the running JobHandle; no real grading
    def fake_grade(config_path: Path, *, force: bool = False) -> dict:
        time.sleep(0.4)
        print("[done] 100572")
        return {
            "stage": "grading",
            "success": 1,
            "errors": 0,
            "total": 1,
            "success_rate": 100.0,
        }

    tw.grade_assignment = fake_grade
    await pilot.press("g")
    await pilot.pause()
    await _wait_for(pilot, lambda: isinstance(ws.app.screen, tw.ConfirmationModal))
    await pilot.press("enter")
    await _wait_for(pilot, lambda: ws._job is not None)
    log = ws.query_one("#richlog", RichLog)
    await _wait_for(
        pilot,
        lambda: ws._job is None and any("[grading]" in str(line) for line in log.lines),
    )
    lines = [str(line) for line in log.lines]
    assert any("[grading]" in line for line in lines), lines


async def _check_cancel(app: TataApp, pilot: Pilot) -> None:
    ws = app.query_one(AssignmentScreen)
    log = ws.query_one("#richlog", RichLog)

    def slow_grade(config_path: Path, *, force: bool = False) -> dict:
        time.sleep(0.6)
        return {
            "stage": "grading",
            "success": 1,
            "errors": 0,
            "total": 1,
            "success_rate": 100.0,
        }

    tw.grade_assignment = slow_grade
    await pilot.press("g")
    await pilot.pause()
    await _wait_for(pilot, lambda: isinstance(ws.app.screen, tw.ConfirmationModal))
    await pilot.press("enter")
    await _wait_for(pilot, lambda: ws._job is not None)
    await pilot.press("x")
    await pilot.pause()
    assert ws._job["state"] == "stopping"  # cancel_event set, UI in Stopping
    await _wait_for(pilot, lambda: ws._job is None)
    lines = [str(line) for line in log.lines]
    assert any("Cancel requested" in line for line in lines), lines


async def _check_button_click(app: TataApp, pilot: Pilot) -> None:
    """F2/F3: mouse path — click stage button opens modal; click cancel works."""
    ws = app.query_one(AssignmentScreen)
    log = ws.query_one("#richlog", RichLog)

    def slow_grade(config_path: Path, *, force: bool = False) -> dict:
        time.sleep(0.6)
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
    await _wait_for(pilot, lambda: not isinstance(ws.app.screen, tw.ConfirmationModal))

    await pilot.click("#stage-grade")
    await _wait_for(pilot, lambda: isinstance(ws.app.screen, tw.ConfirmationModal))
    await pilot.press("enter")
    await _wait_for(pilot, lambda: ws._job is not None)
    await pilot.click("#ws-cancel")
    await pilot.pause()
    assert ws._job["state"] == "stopping"
    await _wait_for(pilot, lambda: ws._job is None)
    lines = [str(line) for line in log.lines]
    assert any("Cancel requested" in line for line in lines), lines


async def _check_editor_warning(app: TataApp, pilot: Pilot) -> None:
    """F5: e with EDITOR unset -> warning notify (no fake 'Config reloaded')."""
    notices: list[tuple[str, str | None]] = []
    orig_notify = app.notify

    def notify_spy(message: str, *args: object, **kwargs: object) -> None:
        notices.append((str(message), str(kwargs.get("severity")) if "severity" in kwargs else None))
        orig_notify(message, *args, **kwargs)

    app.notify = notify_spy
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
    """F1: fetch with no assignment_id -> error notify, no job started."""
    ws = app.query_one(AssignmentScreen)
    cfg_path = ws._info.config_path
    original = cfg_path.read_text(encoding="utf-8")
    notices: list[tuple[str, str | None]] = []
    orig_notify = app.notify

    def notify_spy(message: str, *args: object, **kwargs: object) -> None:
        notices.append((str(message), str(kwargs.get("severity")) if "severity" in kwargs else None))
        orig_notify(message, *args, **kwargs)

    app.notify = notify_spy
    try:
        cfg_path.write_text(
            "[grading]\n"
            "rubric = 'rubrics/exam.toml'\n"
            "system_prompt = 'prompt/system.md'\n"
            "provider = 'deepseek'\n"
            "max_parallel_tasks = 4\n",
            encoding="utf-8",
        )
        await pilot.press("f")
        await pilot.pause()
        assert ws._job is None, ws._job
        assert any("assignment_id" in msg for msg, _sev in notices), notices
        assert any(sev == "error" for _msg, sev in notices), notices
        assert not any("started" in msg for msg, _sev in notices), notices
    finally:
        cfg_path.write_text(original, encoding="utf-8")
        app.notify = orig_notify


async def _check_help_and_back(app: TataApp, pilot: Pilot) -> None:
    ws = app.query_one(AssignmentScreen)
    await pilot.press("?")
    await pilot.pause()
    assert isinstance(ws.app.screen, tw.HelpModal)
    await pilot.press("escape")
    await _wait_for(pilot, lambda: not isinstance(ws.app.screen, tw.HelpModal))
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
    await _check_help_and_back(app, pilot)


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_assignment(root / "assignments")
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
    print("tata_workspace check OK")


if __name__ == "__main__":
    asyncio.run(main())
