"""Runnable headless check for the S4 Plagiarism screen (T2).

Drives PlagiarismScreen (pushed on a host Screen over TataApp.run_test) on a
tmp course fixture: one course with two assignments, each carrying
``plagiarism/all_pairs.json`` (one pair over the course config display
threshold, one below; string-valued sims included), plus a course-level
``plagiarism/aggregate.json`` (z/p flags).  Covers the four course-scoped
tabs (Aggregate default, Assignments, Students, Pairs), the embedded
#cmp-pane compare (no modal pushed — screen stack unchanged), p/a job
starts (stubbed detect — no real copydetect), the no-course empty state,
and the missing-aggregate / corrupt-pairs / zero-pairs states.

Run: uv run tests/tata_plagiarism_check.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path

from e2e_common import cell, spy_notify, wait_for, write_aliases  # isort: skip - seeds repo-root sys.path before src imports
from src.tui import tata_plagiarism as plag_mod
from src.tui.tata_app import TataApp
from src.tui.tata_plagiarism import PlagiarismScreen, pair_side_name
from textual.app import ComposeResult, Screen
from textual.containers import Horizontal
from textual.pilot import Pilot
from textual.widgets import DataTable, RichLog, Static, TabbedContent

COURSE = "c1-first"
A1 = "a1"
A2 = "a2"

PAIRS_A1 = {
    "version": 1,
    "pair_count": 2,
    "pairs": [
        {
            "test_file": f"{A1}b.md",
            "reference_file": f"{A1}c.md",
            "test_similarity_pct": 88.0,
            "reference_similarity_pct": 91.2,
            "max_similarity_pct": 91.2,
            # line-set form: lines 1-3 highlighted in the compare pane
            "token_overlap": [1, 2, 3],
        },
        {
            "test_file": f"{A1}f.md",
            "reference_file": f"{A1}g.md",
            "test_similarity_pct": "70.0",  # string values must be tolerated
            "reference_similarity_pct": "72.0",
            "max_similarity_pct": "72.0",
            "token_overlap": 9,
        },
    ],
}

PAIRS_A2 = {
    "version": 1,
    "pair_count": 1,
    "pairs": [
        {
            "test_file": f"{A2}d.md",
            "reference_file": f"{A2}e.md",
            "test_similarity_pct": 60.1,
            "reference_similarity_pct": 65.5,
            "max_similarity_pct": "65.5",
            "token_overlap": 12,
        }
    ],
}

AGGREGATE_JSON = {
    "data_root": f"data/{COURSE}",
    "alpha": 0.01,
    "tested_pairs": 2,
    "flagged_pairs": 1,
    "pairs": [
        {
            "student_a": "1001",
            "student_b": "1002",
            "raw_similarity_pct": 88.4,
            "z_score": 5.21,
            "one_sided_p_value": 0.0002,
            "shared_assignments": 1,
        },
        {
            "student_a": "1003",
            "student_b": "1004",
            "raw_similarity_pct": 75.0,
            "z_score": 3.4,
            "one_sided_p_value": 0.02,  # z>=3 but not under alpha -> "? watch"
            "shared_assignments": 2,
        },
    ],
}

PROCESSED_A1B = (
    "def solve(data):\n    total = sum(data)\n    return total / len(data)\n"
    "# a fully distinct final line\n"
)
PROCESSED_A1C = (
    "# another docstring header\nimport numpy as np\nresult = np.array([1, 2])\n"
)


def _write_pairs(assignment_dir: Path, payload: dict) -> None:
    (assignment_dir / "plagiarism").mkdir(parents=True, exist_ok=True)
    (assignment_dir / "plagiarism" / "all_pairs.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def _make_fixture(assignments_dir: Path) -> None:
    course_dir = assignments_dir / COURSE
    course_dir.mkdir(parents=True)
    # course config carries NO [grading] (real course configs have none —
    # the panes read the root [plagiarism] section directly); display
    # threshold 0.9 -> 90% flag threshold for the panes
    (course_dir / "config.toml").write_text(
        "[fetch]\ncourse_id = 111111\n[plagiarism]\ndisplay_threshold = 0.9\n",
        encoding="utf-8",
    )
    for name in (A1, A2):
        a_dir = course_dir / name
        for sub in ("raw", "processed", "plagiarism"):
            (a_dir / sub).mkdir(parents=True)
        (a_dir / "config.toml").write_text("", encoding="utf-8")
    _write_pairs(course_dir / A1, PAIRS_A1)
    _write_pairs(course_dir / A2, PAIRS_A2)
    (course_dir / A1 / "processed" / f"{A1}b.md").write_text(
        PROCESSED_A1B, encoding="utf-8"
    )
    (course_dir / A1 / "processed" / f"{A1}c.md").write_text(
        PROCESSED_A1C, encoding="utf-8"
    )
    for name in (f"{A1}f", f"{A1}g", f"{A2}d", f"{A2}e"):
        # a1-processed files live under a1, a2's under a2; keep it boring
        target = course_dir / A1 if name.startswith(A1) else course_dir / A2
        (target / "processed" / f"{name}.md").write_text(
            f"# {name}\nline two\nline three\n", encoding="utf-8"
        )
    (course_dir / "plagiarism").mkdir()
    (course_dir / "plagiarism" / "aggregate.json").write_text(
        json.dumps(AGGREGATE_JSON, indent=2), encoding="utf-8"
    )
    # alias.toml chain: global [student] only (overridden by course),
    # course [course]/[assignment]/[student], assignment-level [student]
    # for the pair file stems (a1g unaliased on purpose).
    write_aliases(assignments_dir / "alias.toml", students={"1001": "Global Alice"})
    write_aliases(
        course_dir / "alias.toml",
        course_alias="My Course",
        assignment_alias={"a1": "First Assignment", "a2": "Second Assignment"},
        students={
            "1001": "Alice, A",
            "1002": "Bob, B",
            "1003": "Carol, C",
            "1004": "Dave, D",
        },
    )
    write_aliases(
        course_dir / A1 / "alias.toml",
        students={
            "a1b": "Alice A",
            "a1c": "Bob B",
            "a1f": "Frank F",
            "333333": "Doe, Jane",
        },
    )
    write_aliases(
        course_dir / A2 / "alias.toml",
        students={"a2d": "Dora D", "a2e": "Eve E"},
    )


class PlagiarismHost(Screen):
    """Thin host so the Vertical PlagiarismScreen can be pushed as a screen."""

    def __init__(self, state: object) -> None:
        super().__init__()
        self._state = state

    def compose(self) -> ComposeResult:
        yield PlagiarismScreen(self._state)  # type: ignore[arg-type]


async def _enter(app: TataApp, pilot: Pilot) -> PlagiarismScreen:
    app.push_screen(PlagiarismHost(app.state))
    await pilot.pause()
    # App.query does not descend into pushed screens; query the host itself
    screen = app.screen.query_one(PlagiarismScreen)
    screen._focus_active_table()  # type: ignore[attr-defined]
    await pilot.pause()
    return screen


def _set_state(app: TataApp) -> None:
    app.state.refresh_courses()
    assert len(app.state.courses) == 1
    app.state.current_course = app.state.courses[0]
    app.state.load_assignments(app.state.current_course)
    assert len(app.state.assignments) == 2
    app.state.current_assignment = app.state.assignments[0]


async def _go_tab(screen: PlagiarismScreen, pilot: Pilot, pane_id: str) -> None:
    tabs = screen.query_one("#plag-tabs", TabbedContent)
    tabs.active = pane_id
    await pilot.pause()


def _check_aggregate_pane(screen: PlagiarismScreen, pilot: Pilot) -> None:
    tabs = screen.query_one("#plag-tabs", TabbedContent)
    assert tabs.active == "pane-aggregate", tabs.active  # default tab
    table = screen.query_one("#agg-table", DataTable)
    assert table.display, "aggregate table should be visible"
    assert table.row_count == 2, table.row_count
    labels = [str(c.label) for c in table.columns.values()]
    assert labels == ["Student A", "Student B", "raw sim", "z", "p", "Flag"], labels
    # course-scoped student names: course alias beats global
    assert cell(table, 0, 0) == "Alice, A", cell(table, 0, 0)
    assert cell(table, 0, 1) == "Bob, B", cell(table, 0, 1)
    assert cell(table, 0, 2) == "88.4", cell(table, 0, 2)
    assert cell(table, 0, 3) == "5.21", cell(table, 0, 3)
    assert cell(table, 0, 4) == "0.0002", cell(table, 0, 4)
    assert cell(table, 0, 5) == "FLAG", cell(table, 0, 5)
    assert cell(table, 1, 0) == "Carol, C", cell(table, 1, 0)
    assert cell(table, 1, 1) == "Dave, D", cell(table, 1, 1)
    assert cell(table, 1, 3) == "3.40", cell(table, 1, 3)
    assert cell(table, 1, 5) == "? watch", cell(table, 1, 5)


def _check_topbar_status(screen: PlagiarismScreen) -> None:
    topbar = str(screen.query_one("#plag-topbar", Static).content)
    assert "My Course" in topbar, topbar
    assert "pairs 3" in topbar, topbar
    assert "display threshold 90%" in topbar, topbar
    status = str(screen.query_one("#plag-status", Static).content)
    assert "3 pairs total" in status, status
    assert "1 flagged (display threshold 90%)" in status, status


def _check_assignments_pane(screen: PlagiarismScreen) -> None:
    table = screen.query_one("#assign-table", DataTable)
    assert table.display, "assignments table should be visible"
    assert table.row_count == 2, table.row_count
    labels = [str(c.label) for c in table.columns.values()]
    assert labels == ["Assignment", "Pairs", "Flagged", "Max sim %"], labels
    assert cell(table, 0, 0) == "First Assignment", cell(table, 0, 0)
    assert cell(table, 0, 1) == "2", cell(table, 0, 1)
    assert cell(table, 0, 2) == "1", cell(table, 0, 2)
    assert cell(table, 0, 3) == "91.2", cell(table, 0, 3)
    assert cell(table, 1, 0) == "Second Assignment", cell(table, 1, 0)
    assert cell(table, 1, 1) == "1", cell(table, 1, 1)
    assert cell(table, 1, 2) == "0", cell(table, 1, 2)
    assert cell(table, 1, 3) == "65.5", cell(table, 1, 3)


def _check_students_pane(screen: PlagiarismScreen) -> None:
    table = screen.query_one("#students-table", DataTable)
    assert table.display, "students table should be visible"
    assert table.row_count == 6, table.row_count
    labels = [str(c.label) for c in table.columns.values()]
    assert labels == ["Student", "Pairs", "Flagged", "Max sim %"], labels
    # sorted by max sim desc, ties by name; a1g unaliased -> raw stem
    expected = [
        ("Alice A", "1", "1", "91.2"),
        ("Bob B", "1", "1", "91.2"),
        ("Frank F", "1", "0", "72.0"),
        ("a1g", "1", "0", "72.0"),
        ("Dora D", "1", "0", "65.5"),
        ("Eve E", "1", "0", "65.5"),
    ]
    for row, (name, pairs, flagged, max_sim) in enumerate(expected):
        assert cell(table, row, 0) == name, (row, cell(table, row, 0))
        assert cell(table, row, 1) == pairs, (row, cell(table, row, 1))
        assert cell(table, row, 2) == flagged, (row, cell(table, row, 2))
        assert cell(table, row, 3) == max_sim, (row, cell(table, row, 3))


def _check_pairs_pane(screen: PlagiarismScreen) -> None:
    table = screen.query_one("#pairs-table", DataTable)
    assert table.display, "pairs table should be visible"
    assert table.row_count == 3, table.row_count
    labels = [str(c.label) for c in table.columns.values()]
    assert labels == [
        "Assignment",
        "Student A",
        "Student B",
        "sim %",
        "overlap",
        "Flag",
    ], labels
    # sorted by sim desc: 91.2 (FLAG, threshold 90%), then 72.0, then 65.5
    assert cell(table, 0, 0) == "First Assignment", cell(table, 0, 0)
    assert cell(table, 0, 1) == "Alice A", cell(table, 0, 1)
    assert cell(table, 0, 2) == "Bob B", cell(table, 0, 2)
    assert cell(table, 0, 3) == "91.2", cell(table, 0, 3)
    assert cell(table, 0, 4) == "3", cell(table, 0, 4)  # line-set length
    assert cell(table, 0, 5) == "FLAG", cell(table, 0, 5)
    assert cell(table, 1, 3) == "72.0", cell(table, 1, 3)
    assert cell(table, 1, 5) == "-", cell(table, 1, 5)
    assert cell(table, 2, 0) == "Second Assignment", cell(table, 2, 0)
    assert cell(table, 2, 1) == "Dora D", cell(table, 2, 1)
    assert cell(table, 2, 2) == "Eve E", cell(table, 2, 2)
    assert cell(table, 2, 3) == "65.5", cell(table, 2, 3)
    assert cell(table, 2, 5) == "-", cell(table, 2, 5)


async def _check_compare_pane(
    screen: PlagiarismScreen, pilot: Pilot, app: TataApp
) -> None:
    assert not hasattr(plag_mod, "CompareModal"), "CompareModal must be gone"
    table = screen.query_one("#pairs-table", DataTable)
    pane = screen.query_one("#cmp-pane", Horizontal)
    stack_len = len(app.screen_stack)

    # row 0: over-threshold pair -> pane shows names + red overlap lines
    table.move_cursor(row=0)
    await wait_for(pilot, lambda: pane.display)
    assert app.screen is screen.parent, "no modal was pushed"
    assert len(app.screen_stack) == stack_len, "screen stack must not grow"
    title = str(screen.query_one("#cmp-title", Static).content)
    assert "Compare: Alice A ↔ Bob B" in title, title
    assert "max_sim 91.2%" in title, title
    assert "token_overlap 3" in title, title
    assert "FLAG" in title, title
    left = str(screen.query_one("#cmp-left", Static).content)
    assert "[red]   1" in left, left
    assert "[red]   2" in left, left
    right = str(screen.query_one("#cmp-right", Static).content)
    assert "[red]   1" in right, right

    # row 2: below-threshold pair -> pane updates, no red markup
    table.move_cursor(row=2)
    await wait_for(
        pilot, lambda: "Eve E" in str(screen.query_one("#cmp-title", Static).content)
    )
    assert pane.display
    title = str(screen.query_one("#cmp-title", Static).content)
    assert "FLAG" not in title, title
    left = str(screen.query_one("#cmp-left", Static).content)
    assert "[red]" not in left, left
    assert len(app.screen_stack) == stack_len, "screen stack must not grow"

    # back to row 0 so subsequent tab checks see a sane cursor
    table.move_cursor(row=0)
    await pilot.pause()


async def _check_no_course_state(screen: PlagiarismScreen, app: TataApp) -> None:
    app.state.current_course = None
    screen.reload_all()
    await asyncio.sleep(0.05)
    empty = screen.query_one("#plag-empty", Static)
    assert empty.display
    assert (
        str(empty.content)
        == "No course selected. Open Dashboard and enter a course first."
    ), str(empty.content)
    _set_state(app)
    screen.reload_all()
    await asyncio.sleep(0.05)


async def _check_error_states(
    screen: PlagiarismScreen, pilot: Pilot, app: TataApp
) -> None:
    course = app.state.current_course
    assert course is not None
    course_dir = course.config_path.parent
    agg_path = course_dir / "plagiarism" / "aggregate.json"
    a1_pairs = course_dir / A1 / "plagiarism" / "all_pairs.json"
    a2_pairs = course_dir / A2 / "plagiarism" / "all_pairs.json"
    agg_original = agg_path.read_text(encoding="utf-8")
    a1_original = a1_pairs.read_text(encoding="utf-8")
    a2_original = a2_pairs.read_text(encoding="utf-8")
    notices, orig_notify = spy_notify(app)
    try:
        # missing aggregate file -> quiet empty state on the aggregate pane
        agg_path.unlink()
        screen.reload_all()
        await asyncio.sleep(0.05)
        await _go_tab(screen, pilot, "pane-aggregate")
        empty = screen.query_one("#agg-empty", Static)
        assert empty.display
        assert str(empty.content) == "No aggregate report yet. Run (a).", str(
            empty.content
        )

        # corrupt pairs JSON -> per-assignment error; assignments pane shows
        # the note under the table, the good assignment still renders
        a2_pairs.write_text("{not json", encoding="utf-8")
        screen.reload_all()
        await asyncio.sleep(0.05)
        await _go_tab(screen, pilot, "pane-assignments")
        table = screen.query_one("#assign-table", DataTable)
        assert table.display
        assert table.row_count == 2, table.row_count
        note = str(screen.query_one("#assign-empty", Static).content)
        assert "Load failed" in note, note
        assert A2 in note, note
        assert any(sev == "error" for _m, sev in notices), notices

        # zero pairs (both files valid but empty) -> quiet empty state
        a2_pairs.write_text(
            json.dumps({"version": 1, "pair_count": 0, "pairs": []}),
            encoding="utf-8",
        )
        a1_pairs.write_text(
            json.dumps({"version": 1, "pair_count": 0, "pairs": []}),
            encoding="utf-8",
        )
        screen.reload_all()
        await asyncio.sleep(0.05)
        await _go_tab(screen, pilot, "pane-pairs")
        empty = screen.query_one("#pairs-empty", Static)
        assert empty.display
        assert "No pairs yet." in str(empty.content), str(empty.content)
    finally:
        agg_path.write_text(agg_original, encoding="utf-8")
        a1_pairs.write_text(a1_original, encoding="utf-8")
        a2_pairs.write_text(a2_original, encoding="utf-8")
        app.notify = orig_notify
        screen.reload_all()
        await asyncio.sleep(0.05)


_detect_calls: list[dict] = []


def _fake_detect(
    config_path: Path,
    *,
    aggregate: bool = False,
    output: Path | None = None,
    quiet: bool = False,
) -> dict:
    _detect_calls.append({
        "path": config_path,
        "aggregate": aggregate,
        "output": output,
        "quiet": quiet,
    })
    time.sleep(0.3)
    print(f"[done] {config_path.name}")
    return {
        "stage": "plagiarism",
        "success": 2,
        "errors": 0,
        "total": 2,
        "success_rate": 100.0,
    }


async def _check_jobs(screen: PlagiarismScreen, pilot: Pilot, app: TataApp) -> None:
    assert screen._job is None
    screen.focus()
    await pilot.pause()
    original_detect = plag_mod.detect_plagiarism
    original_write = plag_mod._write_aggregate_json
    written: list[Path] = []
    notices, orig_notify = spy_notify(app)

    plag_mod.detect_plagiarism = _fake_detect
    _detect_calls.clear()

    def fake_write(config_path: Path) -> Path:
        written.append(config_path)
        return config_path.parent

    plag_mod._write_aggregate_json = fake_write
    try:
        # [p] -> detect job on the assignment config (single-assignment, no aggregate)
        await pilot.press("p")
        await wait_for(pilot, lambda: screen._job is not None)
        assert screen._job["stage"] == "detect", screen._job
        assert screen._job["config_path"].name == "config.toml"
        assert str(screen._job["config_path"].parent).endswith(A1)
        # a second job is refused while one runs
        await pilot.press("a")
        await pilot.pause()
        assert screen._job["stage"] == "detect"
        assert any("running" in msg for msg, _sev in notices), notices
        await wait_for(pilot, lambda: screen._job is None)
        log = screen.query_one("#plag-log", RichLog)
        lines = [str(line) for line in log.lines]
        assert any("[done]" in line for line in lines), lines
        assert any("plagiarism" in line for line in lines), lines
        # [p] detect ran quietly (panes read the JSON, not a printed report)
        assert _detect_calls, _detect_calls
        assert _detect_calls[-1]["quiet"] is True, _detect_calls

        # [a] -> aggregate job on the course config, writes the aggregate JSON
        await pilot.press("a")
        await wait_for(pilot, lambda: screen._job is not None)
        assert screen._job["stage"] == "aggregate", screen._job
        assert str(screen._job["config_path"]).endswith(f"{COURSE}/config.toml")
        await wait_for(pilot, lambda: screen._job is None)
        assert written, "aggregate JSON writer should have been called"
        assert any(sev == "success" for _m, sev in notices), notices
        # [a] detected through detect_plagiarism, also quiet
        assert len(_detect_calls) >= 2, _detect_calls
        assert _detect_calls[-1]["aggregate"] is True, _detect_calls
        assert _detect_calls[-1]["quiet"] is True, _detect_calls
        # after the job, tabs got reloaded: aggregate pane still renders
        assert screen.query_one("#agg-table", DataTable).row_count == 2
    finally:
        plag_mod.detect_plagiarism = original_detect
        plag_mod._write_aggregate_json = original_write
        app.notify = orig_notify


def _check_late_alias_resolution(app: TataApp) -> None:
    """_LATE_N / _N file stems resolve to the base-uid alias for pair names."""
    state = app.state
    course = state.current_course
    a_info = state.assignments[0]
    assert course is not None
    assignments_dir = state.assignments_dir
    course_name = course.dir_name
    assert (
        pair_side_name(assignments_dir, course_name, a_info, "333333_LATE_0.ipynb")
        == "Doe, Jane"
    )
    assert (
        pair_side_name(assignments_dir, course_name, a_info, "333333_0.ipynb")
        == "Doe, Jane"
    )
    assert (
        pair_side_name(assignments_dir, course_name, a_info, "333333_LATE_1.txt")
        == "Doe, Jane"
    )
    # unaliased stem falls back to the raw stem (unchanged)
    assert (
        pair_side_name(assignments_dir, course_name, a_info, "999999.ipynb") == "999999"
    )


async def check_screen(app: TataApp, pilot: Pilot) -> None:
    _set_state(app)
    screen = await _enter(app, pilot)
    _check_topbar_status(screen)
    _check_aggregate_pane(screen, pilot)
    await _go_tab(screen, pilot, "pane-assignments")
    _check_assignments_pane(screen)
    await _go_tab(screen, pilot, "pane-students")
    _check_students_pane(screen)
    await _go_tab(screen, pilot, "pane-pairs")
    _check_pairs_pane(screen)
    await _check_compare_pane(screen, pilot, app)
    await _check_no_course_state(screen, app)
    await _check_error_states(screen, pilot, app)
    _check_late_alias_resolution(app)
    await _check_jobs(screen, pilot, app)


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_fixture(root / "data")
        app = TataApp(root_dir=root)
        async with app.run_test(size=(120, 40)) as pilot:
            await check_screen(app, pilot)
    print("tata plagiarism check OK")


if __name__ == "__main__":
    asyncio.run(main())
