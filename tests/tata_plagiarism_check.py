"""Runnable headless check for the S4 Plagiarism screen (T6a).

Drives PlagiarismScreen (pushed on a host Screen over TataApp.run_test) on a
tmp course fixture: one course with one assignment carrying
``plagiarism/all_pairs.json`` (2 pairs, one over the config display
threshold) plus a course-level ``plagiarism/aggregate.json``. Covers the
pairs table values/flag styling, tab switch to aggregate, compare modal
(open/close, red highlight), p and a job starts (stubbed detect —
no real copydetect), the no-course empty state, and JSON-corrupt error
states.

Run: uv run tests/tata_plagiarism_check.py
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

import src.tata_plagiarism as plag_mod
from src.tata_app import TataApp
from src.tata_plagiarism import CompareModal, PlagiarismScreen, _pair_student_name
from textual.app import ComposeResult, Screen
from textual.pilot import Pilot
from textual.widgets import DataTable, RichLog, Static, TabbedContent

COURSE = "c1-first"
ASSIGNMENT = "a1"

PAIRS_JSON = {
    "version": 1,
    "pair_count": 2,
    "pairs": [
        {
            "test_file": f"{ASSIGNMENT}b.md",
            "reference_file": f"{ASSIGNMENT}c.md",
            "test_similarity_pct": 88.0,
            "reference_similarity_pct": 91.2,
            "max_similarity_pct": 91.2,
            # line-set form: lines 1-3 highlighted in the compare modal
            "token_overlap": [1, 2, 3],
        },
        {
            "test_file": f"{ASSIGNMENT}d.md",
            "reference_file": f"{ASSIGNMENT}e.md",
            "test_similarity_pct": 60.1,
            "reference_similarity_pct": 65.5,
            "max_similarity_pct": 65.5,
            "token_overlap": 12,
        },
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


def _make_fixture(assignments_dir: Path) -> None:
    course_dir = assignments_dir / COURSE
    course_dir.mkdir(parents=True)
    (course_dir / "config.toml").write_text(
        "[fetch]\ncourse_id = 111111\n", encoding="utf-8"
    )
    a_dir = course_dir / ASSIGNMENT
    for sub in ("raw", "processed", "plagiarism"):
        (a_dir / sub).mkdir(parents=True)
    (a_dir / "config.toml").write_text(
        "[fetch]\nassignment_id = 1001\n"
        "[grading]\nrubric = 'rubrics/exam.toml'\n"
        "system_prompt = 'prompt/system.md'\n"
        "provider = 'deepseek'\n"
        "max_parallel_tasks = 4\n"
        "[plagiarism]\ndisplay_threshold = 0.9\n",  # config override: 90%
        encoding="utf-8",
    )
    (a_dir / "processed" / f"{ASSIGNMENT}b.md").write_text(
        "def solve(data):\n    total = sum(data)\n    return total / len(data)\n"
        "# a fully distinct final line\n",
        encoding="utf-8",
    )
    (a_dir / "processed" / f"{ASSIGNMENT}c.md").write_text(
        "# another docstring header\nimport numpy as np\n", encoding="utf-8"
    )
    (a_dir / "plagiarism" / "all_pairs.json").write_text(
        json.dumps(PAIRS_JSON, indent=2), encoding="utf-8"
    )
    (course_dir / "plagiarism").mkdir()
    (course_dir / "plagiarism" / "aggregate.json").write_text(
        json.dumps(AGGREGATE_JSON, indent=2), encoding="utf-8"
    )
    # alias.toml chain for display names:
    # global [student] only (overridden by course), course [course]/[assignment],
    # assignment-level [student] for the pairs file stems + a course-student
    # override (later files win -> "Carol, Z").
    (assignments_dir / "alias.toml").write_text(
        '[student]\n"1001" = "Global Alice"\n', encoding="utf-8"
    )
    (course_dir / "alias.toml").write_text(
        '[course]\n"111111" = "My Course"\n'
        '[assignment]\n"1001" = "First Assignment"\n'
        '[student]\n"1001" = "Alice, A"\n"1002" = "Bob, B"\n'
        '"1003" = "Carol, C"\n"1004" = "Dave, D"\n',
        encoding="utf-8",
    )
    (a_dir / "alias.toml").write_text(
        '[student]\n"a1b" = "Alice A"\n"a1c" = "Bob B"\n"1003" = "Carol, Z"\n'
        '"333333" = "Doe, Jane"\n',
        encoding="utf-8",
    )


class PlagiarismHost(Screen):
    """Thin host so the Vertical PlagiarismScreen can be pushed as a screen."""

    def __init__(self, state: object) -> None:
        super().__init__()
        self._state = state

    def compose(self) -> ComposeResult:
        yield PlagiarismScreen(self._state)  # type: ignore[arg-type]


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


def _cell(table: DataTable, row: int, col: int) -> object:
    from textual.coordinate import Coordinate

    cell = table.get_cell_at(Coordinate(row, col))
    return cell.plain if hasattr(cell, "plain") else str(cell)


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
    assert len(app.state.assignments) == 1
    app.state.current_assignment = app.state.assignments[0]


def _check_pairs_pane(screen: PlagiarismScreen) -> None:
    table = screen.query_one("#pairs-table", DataTable)
    assert table.display, "pairs table should be visible"
    assert table.row_count == 2, table.row_count
    labels = [str(c.label) for c in table.columns.values()]
    assert labels == ["File A", "File B", "sim %", "overlap", "diff", "Flag"], labels
    # sorted by sim desc: 91.2 first, threshold 90% from the config override;
    # file stems (student uids) resolve to [student] aliases
    assert _cell(table, 0, 0) == "Alice A", _cell(table, 0, 0)
    assert _cell(table, 0, 1) == "Bob B", _cell(table, 0, 1)
    assert _cell(table, 0, 2) == "91.2", _cell(table, 0, 2)
    assert _cell(table, 0, 3) == "3", _cell(table, 0, 3)  # line-set length
    assert _cell(table, 0, 4) == "+1.2", _cell(table, 0, 4)
    assert _cell(table, 0, 5) == "FLAG", _cell(table, 0, 5)
    assert _cell(table, 1, 5) == "-", _cell(table, 1, 5)
    assert _cell(table, 1, 2) == "65.5", _cell(table, 1, 2)
    assert _cell(table, 1, 4) == "-24.5", _cell(table, 1, 4)
    # unaliased stem falls back to the raw stem
    assert _cell(table, 1, 0) == "a1d", _cell(table, 1, 0)
    assert _cell(table, 1, 1) == "a1e", _cell(table, 1, 1)
    topbar = str(screen.query_one("#plag-topbar", Static).content)
    assert "My Course / First Assignment" in topbar, topbar
    assert "pairs 2" in topbar, topbar
    assert "display threshold 90%" in topbar, topbar
    status = str(screen.query_one("#plag-status", Static).content)
    assert "1 of 2 pairs over display_threshold 90%" in status, status
    assert "0.01" in status, status


async def _check_aggregate_pane(screen: PlagiarismScreen, pilot: Pilot) -> None:
    tabs = screen.query_one("#plag-tabs", TabbedContent)
    assert tabs.active == "pane-pairs"
    await pilot.press("tab")
    await pilot.pause()
    assert tabs.active == "pane-aggregate"
    table = screen.query_one("#agg-table", DataTable)
    assert table.display, "aggregate table should be visible"
    assert table.row_count == 2, table.row_count
    # course-scoped student names: course alias beats global; assignment-level
    # override wins for 1003 ("Carol, Z")
    assert _cell(table, 0, 0) == "Alice, A", _cell(table, 0, 0)
    assert _cell(table, 0, 1) == "Bob, B", _cell(table, 0, 1)
    assert _cell(table, 0, 2) == "88.4", _cell(table, 0, 2)
    assert _cell(table, 0, 3) == "5.21", _cell(table, 0, 3)
    assert _cell(table, 0, 4) == "0.0002", _cell(table, 0, 4)
    assert _cell(table, 0, 5) == "FLAG", _cell(table, 0, 5)
    assert _cell(table, 1, 0) == "Carol, Z", _cell(table, 1, 0)
    assert _cell(table, 1, 1) == "Dave, D", _cell(table, 1, 1)
    assert _cell(table, 1, 5) == "? watch", _cell(table, 1, 5)
    assert _cell(table, 1, 3) == "3.40", _cell(table, 1, 3)
    # back to pairs
    await pilot.press("tab")
    await pilot.pause()
    assert tabs.active == "pane-pairs"


async def _check_compare_modal(screen: PlagiarismScreen, pilot: Pilot, app: TataApp) -> None:
    table = screen.query_one("#pairs-table", DataTable)
    table.focus()
    table.move_cursor(row=0)
    await pilot.press("enter")
    await pilot.pause()
    assert isinstance(app.screen, CompareModal), type(app.screen)
    title = str(app.screen.query_one(".modal-title", Static).content)  # type: ignore[union-attr]
    assert "Compare: Alice A ↔ Bob B" in title, title
    assert "max_sim 91.2%" in title, title
    assert "token_overlap 3" in title, title
    assert "FLAG" in title, title
    left = str(app.screen.query_one("#cmp-left", Static).content)  # type: ignore[union-attr]
    assert "[red]   1" in left, left
    assert "[red]   2" in left, left
    await pilot.press("escape")
    await pilot.pause()
    assert not isinstance(app.screen, CompareModal)


async def _check_no_course_state(
    screen: PlagiarismScreen, pilot: Pilot, app: TataApp
) -> None:
    app.state.current_course = None
    screen.reload_all()
    await pilot.pause()
    empty = screen.query_one("#plag-empty", Static)
    assert empty.display
    text = str(empty.content)
    assert text == "No course selected. Open Dashboard and enter a course first.", text
    # restore for the remaining checks
    _set_state(app)
    screen.reload_all()
    await pilot.pause()


async def _check_error_states(screen: PlagiarismScreen, app: TataApp) -> None:
    pairs_path = (
        app.state.current_assignment.config_path.parent
        / "plagiarism"
        / "all_pairs.json"
    )
    original = pairs_path.read_text(encoding="utf-8")
    notices: list[tuple[str, str | None]] = []
    orig_notify = app.notify

    def notify_spy(message: str, *args: object, **kwargs: object) -> None:
        notices.append((str(message), str(kwargs.get("severity"))))
        orig_notify(message, *args, **kwargs)

    app.notify = notify_spy
    try:
        # corrupt JSON -> Load failed
        pairs_path.write_text("{not json", encoding="utf-8")
        screen.reload_all()
        await asyncio.sleep(0.05)
        empty = screen.query_one("#pairs-empty", Static)
        assert empty.display
        assert str(empty.content).startswith("Load failed:"), str(empty.content)
        assert any(sev == "error" for _m, sev in notices), notices
        # missing file -> not-run empty state
        pairs_path.unlink()
        screen.reload_all()
        empty = screen.query_one("#pairs-empty", Static)
        assert empty.display
        assert "No pairs yet." in str(empty.content), str(empty.content)
        # 0 pairs -> done empty state
        pairs_path.write_text(
            json.dumps({"version": 1, "pair_count": 0, "pairs": []}),
            encoding="utf-8",
        )
        screen.reload_all()
        empty = screen.query_one("#pairs-empty", Static)
        assert empty.display
        assert "Detection done, 0 pairs" in str(empty.content), str(empty.content)
    finally:
        pairs_path.write_text(original, encoding="utf-8")
        app.notify = orig_notify
        screen.reload_all()
        await asyncio.sleep(0.05)


def _fake_detect(config_path: Path, *, aggregate: bool = False, output: Path | None = None) -> dict:
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
    notices: list[tuple[str, str | None]] = []
    orig_notify = app.notify

    def notify_spy(message: str, *args: object, **kwargs: object) -> None:
        notices.append((str(message), str(kwargs.get("severity"))))
        orig_notify(message, *args, **kwargs)

    plag_mod.detect_plagiarism = _fake_detect

    def fake_write(config_path: Path) -> Path:
        written.append(config_path)
        return config_path.parent

    plag_mod._write_aggregate_json = fake_write
    app.notify = notify_spy
    try:
        # [p] -> detect job on the assignment config (single-assignment, no aggregate)
        await pilot.press("p")
        await _wait_for(pilot, lambda: screen._job is not None)
        assert screen._job["stage"] == "detect", screen._job
        assert screen._job["config_path"].name == "config.toml"
        assert str(screen._job["config_path"].parent).endswith(ASSIGNMENT)
        # a second job is refused while one runs
        await pilot.press("a")
        await pilot.pause()
        assert screen._job["stage"] == "detect"
        assert any("running" in msg for msg, _sev in notices), notices
        await _wait_for(pilot, lambda: screen._job is None)
        log = screen.query_one("#plag-log", RichLog)
        lines = [str(line) for line in log.lines]
        assert any("[done]" in line for line in lines), lines
        assert any("plagiarism" in line for line in lines), lines

        # [a] -> aggregate job on the course config, writes the aggregate JSON
        await pilot.press("a")
        await _wait_for(pilot, lambda: screen._job is not None)
        assert screen._job["stage"] == "aggregate", screen._job
        assert str(screen._job["config_path"]).endswith(f"{COURSE}/config.toml")
        await _wait_for(pilot, lambda: screen._job is None)
        assert written, "aggregate JSON writer should have been called"
        assert any(sev == "success" for _m, sev in notices), notices
    finally:
        plag_mod.detect_plagiarism = original_detect
        plag_mod._write_aggregate_json = original_write
        app.notify = orig_notify


def _check_late_alias_resolution(app: TataApp) -> None:
    """_LATE_N / _N file stems resolve to the base-uid alias for pair names."""
    state = app.state
    assert _pair_student_name(state, "333333_LATE_0.ipynb") == "Doe, Jane"
    assert _pair_student_name(state, "333333_0.ipynb") == "Doe, Jane"
    assert _pair_student_name(state, "333333_LATE_1.txt") == "Doe, Jane"
    # unaliased stem falls back to the raw stem (unchanged)
    assert _pair_student_name(state, "999999.ipynb") == "999999"


async def check_screen(app: TataApp, pilot: Pilot) -> None:
    _set_state(app)
    screen = await _enter(app, pilot)
    _check_pairs_pane(screen)
    _check_late_alias_resolution(app)
    await _check_aggregate_pane(screen, pilot)
    await _check_compare_modal(screen, pilot, app)
    await _check_no_course_state(screen, pilot, app)
    await _check_error_states(screen, app)
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
