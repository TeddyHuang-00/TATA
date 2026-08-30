"""Shared helpers for the runnable headless TUI check scripts (tests/*_check.py).

Importing this module puts the repo root on ``sys.path`` once, so each check
script can ``import src...`` without its own sys.path boilerplate. Provides
the fixture builders and TUI helpers duplicated across the check scripts.

Run: uv run python tests/<check_name>.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from textual.app import App
from textual.pilot import Pilot
from textual.widgets import DataTable, Static

COURSE = "c1-first"

# the two flag-shape JSON variants the check fixtures use
PAIRS_FULL = {
    "version": 1,
    "pair_count": 1,
    "pairs": [
        {
            "test_file": "x.py",
            "reference_file": "y.py",
            "max_similarity_pct": 95.0,
        }
    ],
}
PAIRS_MINIMAL = {"version": 1, "pairs": [{"max_similarity_pct": 95.0}]}


def make_course(
    assignments_dir: Path,
    *,
    course: str = COURSE,
    course_id: int = 111111,
    assignments: dict[str, int] | None = None,
    entries: bool = False,
    assignment_cfg: str = "",
    graded: str | None = None,
    processed: list[str] | None = None,
    scored: bool = False,
    fetch_cache: bool = False,
    logs: bool = False,
    pairs: str | None = None,
    env: bool = False,
) -> None:
    """One course in ``assignments_dir`` with the TUI check fixture layout.

    ``assignments`` maps dir name -> fetch assignment_id (default {"a1": 1001}).
    ``entries`` adds ``[[fetch.assignments]]`` rows (out = "<name>/raw") to the
    course config. ``assignment_cfg`` is appended to each assignment
    config.toml (e.g. a ``[grading]`` section). ``graded`` is "first" or "all"
    (writes graded/100001.json). ``processed`` lists the processed/*.md stems
    (default ["100001"]). ``scored``/``fetch_cache``/``logs`` add the
    scored/txt, raw/.fetch-cache.json and logs/checkpoint files. ``pairs`` is
    "full" or "minimal" (plagiarism/all_pairs.json on the first assignment).
    ``env`` writes the Canvas .env next to the data root.
    """
    if assignments is None:
        assignments = {"a1": 1001}
    course_dir = assignments_dir / course
    course_dir.mkdir(parents=True)
    entries_txt = (
        "".join(
            f'[[fetch.assignments]]\nassignment_id = {aid}\nout = "{name}/raw"\n'
            for name, aid in assignments.items()
        )
        if entries
        else ""
    )
    (course_dir / "config.toml").write_text(
        f"[fetch]\ncourse_id = {course_id}\n" + entries_txt, encoding="utf-8"
    )
    first = next(iter(assignments))
    for name, aid in assignments.items():
        a_dir = course_dir / name
        for sub in ("raw", "processed", "graded"):
            (a_dir / sub).mkdir(parents=True)
        if scored:
            (a_dir / "scored" / "txt").mkdir(parents=True)
        if logs:
            (a_dir / "logs").mkdir(parents=True)
        (a_dir / "config.toml").write_text(
            f"[fetch]\nassignment_id = {aid}\n" + assignment_cfg, encoding="utf-8"
        )
        (a_dir / "raw" / "100001.ipynb").write_text("{}", encoding="utf-8")
        (a_dir / "raw" / "100002.txt").write_text("hi", encoding="utf-8")
        for stem in processed or ["100001"]:
            (a_dir / "processed" / f"{stem}.md").write_text("# p", encoding="utf-8")
        if graded and (graded == "all" or name == first):
            (a_dir / "graded" / "100001.json").write_text(
                json.dumps({"task1": {"rating": "correct"}}), encoding="utf-8"
            )
        if scored:
            (a_dir / "scored" / "txt" / "100001.txt").write_text(
                "Total Score: 15.0/25.0", encoding="utf-8"
            )
        if fetch_cache:
            (a_dir / "raw" / ".fetch-cache.json").write_text("{}", encoding="utf-8")
        if logs:
            (a_dir / "logs" / "grading.checkpoint.json").write_text(
                json.dumps({"done": ["100001"]}), encoding="utf-8"
            )
        if pairs and name == first:
            (a_dir / "plagiarism").mkdir(parents=True)
            (a_dir / "plagiarism" / "all_pairs.json").write_text(
                json.dumps(PAIRS_FULL if pairs == "full" else PAIRS_MINIMAL),
                encoding="utf-8",
            )
    if env:
        (assignments_dir.parent / ".env").write_text(
            "CANVAS_BASE_URL=https://canvas.example.edu\nCANVAS_ACCESS_TOKEN=tok\n",
            encoding="utf-8",
        )


def write_aliases(
    path: Path,
    *,
    course_alias: str | None = None,
    assignment_alias: dict[str, str] | None = None,
    students: dict[str, str] | None = None,
) -> None:
    """Write a [course]/[assignment]/[student] alias.toml chain (sections in
    that order, absent sections omitted)."""
    parts: list[str] = []
    if course_alias is not None:
        parts.append(f'[course]\n"111111" = "{course_alias}"\n')
    if assignment_alias:
        parts.append(
            "[assignment]\n"
            + "".join(
                f'"{aid}" = "{alias}"\n' for aid, alias in assignment_alias.items()
            )
        )
    if students:
        parts.append(
            "[student]\n"
            + "".join(f'"{uid}" = "{name}"\n' for uid, name in students.items())
        )
    path.write_text("".join(parts), encoding="utf-8")


def write_graded(graded_dir: Path, uid: str, rating: str, feedback: str) -> Path:
    """One graded/<uid>.json submission record ({"task1": {rating, feedback}})."""
    p = graded_dir / f"{uid}.json"
    p.write_text(
        json.dumps({"task1": {"rating": rating, "feedback": feedback}}),
        encoding="utf-8",
    )
    return p


async def wait_for(
    pilot: Pilot, predicate: Callable[[], bool], timeout: float = 30.0
) -> None:
    """Pause-loop until the predicate is true, else AssertionError."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause()
        if predicate():
            return
        await asyncio.sleep(0.02)
    message = "timeout waiting for predicate"
    raise AssertionError(message)


def text(widget: Static) -> str:
    return str(widget.content)


def cell(table: DataTable, row: int, col: int) -> object:
    from textual.coordinate import Coordinate

    cell = table.get_cell_at(Coordinate(row, col))
    return cell.plain if hasattr(cell, "plain") else str(cell)


def spy_notify(app: App) -> tuple[list[tuple[str, str | None]], Callable]:
    """Patch app.notify to record (message, severity) tuples; calls the real
    notify through. Returns (recorded, original_notify) for restore."""
    notices: list[tuple[str, str | None]] = []
    orig = app.notify

    def spy(message: str, *args: object, **kwargs: object) -> None:
        severity = kwargs.get("severity")
        notices.append((str(message), str(severity) if severity is not None else None))
        orig(message, *args, **kwargs)

    app.notify = spy
    return notices, orig
