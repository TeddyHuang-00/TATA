"""Runnable headless check for the TATA TUI platform shell (T4a).

Follows tests/preview_check.py: App.run_test() + Pilot on a tmp course
layout, no pytest-asyncio. Asserts: scan counts, Global table rows, drill
down/up navigation, breadcrumb text, assignment placeholder, empty state.

Run: uv run tests/tata_app_check.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.tata_app import TataApp
from src.tata_plagiarism import PlagiarismScreen
from src.tata_scan import scan_assignments, scan_courses
from src.tata_settings import SettingsScreen
from src.tata_workspace import AssignmentScreen
from textual.pilot import Pilot
from textual.widgets import DataTable, Static

COURSE_A = "c1-first"
COURSE_B = "c2-second"


def _make_course(
    assignments_dir: Path, name: str, course_id: int, assignments: dict[str, int]
) -> None:
    course_dir = assignments_dir / name
    course_dir.mkdir(parents=True)
    (course_dir / "config.toml").write_text(
        f"[fetch]\ncourse_id = {course_id}\n", encoding="utf-8"
    )
    for a_name, a_id in assignments.items():
        a_dir = course_dir / a_name
        (a_dir / "raw").mkdir(parents=True)
        (a_dir / "processed").mkdir()
        (a_dir / "graded").mkdir()
        (a_dir / "scored" / "txt").mkdir(parents=True)
        (a_dir / "config.toml").write_text(
            f"[fetch]\nassignment_id = {a_id}\n", encoding="utf-8"
        )
        (a_dir / "raw" / "100001.ipynb").write_text("{}", encoding="utf-8")
        (a_dir / "raw" / "100002.txt").write_text("hi", encoding="utf-8")
        (a_dir / "processed" / "100001.md").write_text("# p", encoding="utf-8")
        (a_dir / "graded" / "100001.json").write_text(
            json.dumps({"task1": {"rating": "correct"}}), encoding="utf-8"
        )
        (a_dir / "scored" / "txt" / "100001.txt").write_text(
            "Total Score: 15.0/25.0", encoding="utf-8"
        )


def _text(widget: Static) -> str:
    return str(widget.content)


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


def _check_scanner(assignments_dir: Path) -> None:
    """Direct scan assertions (counts, ids, flags, score summary)."""
    courses = scan_courses(assignments_dir)
    assert [c.dir_name for c in courses] == [COURSE_A, COURSE_B], courses
    ca, _cb = courses
    assert ca.course_id == 111111
    assert ca.assignment_count == 2
    assert (ca.counts.raw, ca.counts.processed, ca.counts.graded, ca.counts.scored) == (
        4,
        2,
        2,
        2,
    ), ca.counts
    assert ca.score_mean is not None
    assert abs(ca.score_mean - 60.0) < 1e-6
    assert ca.flagged_pairs == 1, ca.flagged_pairs

    infos = scan_assignments(assignments_dir / COURSE_A)
    assert len(infos) == 2
    a1, a2 = infos
    assert a1.assignment_id == 1001
    assert a1.counts.raw == 2
    assert a1.score_summary is not None
    assert abs(a1.score_summary - 60.0) < 1e-6
    assert a1.flagged_pairs == 1
    assert a2.flagged_pairs == 0


async def _check_navigation(root: Path) -> None:
    """UI flow: global -> course -> assignment -> back up, breadcrumb text."""
    app = TataApp(root_dir=root)
    async with app.run_test(size=(120, 40)) as pilot:
        table = app.query_one("#dashboard-table", DataTable)
        breadcrumb = app.query_one("#breadcrumb", Static)
        await _wait_for(pilot, lambda: table.row_count == 2)

        assert table.row_count == 2, table.row_count
        assert "Global" in _text(breadcrumb)

        # drill down: Global -> Course (row 0 = COURSE_A)
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.state.dashboard_level == "course"
        assert app.state.current_course is not None
        assert app.state.current_course.dir_name == COURSE_A
        assert table.row_count == 2  # a1, a2
        assert COURSE_A in _text(breadcrumb)

        # drill up: Course -> Global
        await pilot.press("escape")
        await pilot.pause()
        assert app.state.dashboard_level == "global"
        assert "Global" in _text(breadcrumb)
        assert table.row_count == 2

        # drill all the way to the assignment workspace
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.state.dashboard_level == "assignment"
        assert app.state.current_assignment is not None
        assert app.state.current_assignment.dir_name == "a1"
        workspace = app.query_one(AssignmentScreen)
        assert workspace.display, "workspace not shown"
        # fixture configs have no [grading] -> workspace empty state, buttons off
        empty = workspace.query_one("#ws-empty", Static)
        assert empty.display, "no-config empty state not shown"
        assert "No valid config.toml" in _text(empty), _text(empty)
        assert len(workspace.query(".stage-btn")) == 6
        assert "a1" in _text(breadcrumb)

        # back up twice to Global
        await pilot.press("escape")
        await pilot.pause()
        assert app.state.dashboard_level == "course"
        # MINOR-5: cursor re-seated on the prior selection (a1) after rebuild
        assert table.cursor_row == 0, table.cursor_row
        await pilot.press("escape")
        await pilot.pause()
        assert app.state.dashboard_level == "global"


async def _check_empty_state(root: Path) -> None:
    """No assignments dir at all -> English empty state, table hidden."""
    app = TataApp(root_dir=root)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        empty = app.query_one("#dash-empty", Static)
        table = app.query_one("#dashboard-table", DataTable)
        assert empty.display, "empty state not shown"
        assert not table.display
        assert "No courses yet" in _text(empty), _text(empty)


async def _check_tabs(root: Path) -> None:
    """T4c: real Plagiarism/Settings screens mounted; tab switching refreshes."""
    app = TataApp(root_dir=root)
    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for(pilot, lambda: app.state.courses != [])
        # placeholders are gone: the real screens are mounted
        assert app.query_one(PlagiarismScreen) is not None
        assert app.query_one(SettingsScreen) is not None
        table = app.query_one("#dashboard-table", DataTable)
        # enter the course so settings derives 'course' and plagiarism has data
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.state.dashboard_level == "course"

        app.switch_tab("tab-plagiarism")
        await pilot.pause()
        plag = app.query_one(PlagiarismScreen)
        assert not plag.query_one("#plag-empty", Static).display
        assert plag.query_one("#plag-tabs").display

        app.switch_tab("tab-settings")
        await pilot.pause()
        settings = app.query_one(SettingsScreen)
        assert settings.current_context == "course", settings.current_context

        app.switch_tab("tab-dashboard")
        await pilot.pause()
        assert table.display


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assignments_dir = root / "data"
        _make_course(assignments_dir, COURSE_A, 111111, {"a1": 1001, "a2": 1002})
        _make_course(assignments_dir, COURSE_B, 999001, {"b1": 2001})
        # one flagged pair for a1's flagged_pairs
        pairs_path = assignments_dir / COURSE_A / "a1" / "plagiarism" / "all_pairs.json"
        pairs_path.parent.mkdir(parents=True)
        pairs_path.write_text(
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

        _check_scanner(assignments_dir)
        await _check_navigation(root)
        await _check_tabs(root)

    with tempfile.TemporaryDirectory() as tmp:
        await _check_empty_state(Path(tmp))

    print("tata_app check OK")


if __name__ == "__main__":
    asyncio.run(main())
