"""Course/assignment directory scanning for the TATA TUI.

Course recognition follows design 01 §2: a course dir under ``data/``
holds a ``config.toml`` whose own subdirectories are leaf assignments (each
with a ``config.toml``).  :func:`src.assignment_config.is_course_config`
implements exactly that rule (it also rejects ``example/`` and stray leaf
legacy dirs); we keep the literal ``example`` name filter as belt-and-braces.

Scan cost: a handful of dirs + small files — well under 100 ms, no caching.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from src.assignment_config import FetchSection, is_course_config, load_root_section

# ponytail: display threshold for a "flagged" pair (aligns with design 04
# `display_threshold = 0.8`); NOT the aggregate z-score alpha — z-level flags
# live in the aggregate report (S4), which the TUI does not consume yet.
DISPLAY_THRESHOLD_PCT = 80.0

_TOTAL_SCORE_RE = re.compile(r"Total Score:\s*([0-9.]+)/([0-9.]+)")


@dataclass
class Counts:
    raw: int = 0
    processed: int = 0
    graded: int = 0
    scored: int = 0


@dataclass
class AssignmentInfo:
    dir_name: str
    config_path: Path
    assignment_id: int | None = None
    counts: Counts = field(default_factory=Counts)
    stage_mtime: dict[str, float] = field(default_factory=dict)  # epoch seconds
    last_run: float | None = None
    # Mean submission score as percent of the max (None when unscored).
    score_summary: float | None = None
    # Display-level pairs (max_similarity_pct >= DISPLAY_THRESHOLD_PCT);
    # aggregate z-level flags live in the aggregate report (S4).
    flagged_pairs: int = 0


@dataclass
class CourseInfo:
    dir_name: str
    config_path: Path
    course_id: int | None = None
    assignment_count: int = 0
    counts: Counts = field(default_factory=Counts)
    score_mean: float | None = None  # mean of per-assignment score_summary
    # Display-level pairs across the course's assignments (see AssignmentInfo).
    flagged_pairs: int = 0
    last_run: float | None = None


def count_files(dir_: Path, suffix: str | None = None) -> int:
    """Direct files in ``dir_``, skipping dotfiles ('.fetch-cache.json')."""
    if not dir_.is_dir():
        return 0
    return sum(
        1
        for p in dir_.iterdir()
        if p.is_file()
        and not p.name.startswith(".")
        and (suffix is None or p.suffix == suffix)
    )


def count_recursive(dir_: Path) -> int:
    if not dir_.is_dir():
        return 0
    return sum(
        1
        for p in dir_.rglob("*")
        if p.is_file() and not p.name.startswith(".")
    )


def _max_file_mtime(dir_: Path) -> float | None:
    """Newest mtime among ``dir_``'s direct files (None when empty)."""
    if not dir_.is_dir():
        return None
    latest: float | None = None
    for p in dir_.iterdir():
        if p.is_file():
            latest = p.stat().st_mtime if latest is None else max(latest, p.stat().st_mtime)
    return latest


def _fetch_id(config_path: Path, key: str) -> int | None:
    """Tolerant read of ``[fetch] <key>`` from a config path."""
    try:
        fetch = load_root_section(config_path, "fetch", FetchSection)
    except (OSError, ValueError):
        return None
    if fetch is None:
        return None
    value = getattr(fetch, key, None)
    return value if isinstance(value, int) else None


def _score_summary(scored_dir: Path) -> float | None:
    """Mean percent of the 'Total Score: X/Y' lines under scored/ (None if none)."""
    totals: list[float] = []
    for p in scored_dir.rglob("*"):
        if not p.is_file():
            continue
        for m in _TOTAL_SCORE_RE.finditer(
            p.read_text(encoding="utf-8", errors="replace")
        ):
            total, max_score = float(m.group(1)), float(m.group(2))
            if max_score > 0:
                totals.append(total / max_score * 100.0)
    if not totals:
        return None
    return sum(totals) / len(totals)


def _pair_pct(pair: dict) -> float:
    """max_similarity_pct as float (0.0 on missing/malformed values).

    Real data writes floats, but dirty JSON (strings, nulls, non-dict
    entries) must not crash the TUI's on_mount scan.
    """
    if not isinstance(pair, dict):
        return 0.0
    try:
        return float(pair.get("max_similarity_pct", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _flagged_pairs(assignment_dir: Path) -> int:
    """Display-level pairs in ``plagiarism/all_pairs.json`` at/above the
    display threshold (design 04 ``display_threshold``; NOT z-level flags)."""
    pairs_file = assignment_dir / "plagiarism" / "all_pairs.json"
    if not pairs_file.is_file():
        return 0
    try:
        data = json.loads(pairs_file.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return 0
    return sum(
        1
        for pair in data.get("pairs", [])
        if _pair_pct(pair) >= DISPLAY_THRESHOLD_PCT
    )


def scan_assignments(course_dir: Path) -> list[AssignmentInfo]:
    """Scan the leaf assignment dirs of ``course_dir`` (each holds config.toml)."""
    infos: list[AssignmentInfo] = []
    if not course_dir.is_dir():
        return infos
    for entry in sorted(course_dir.iterdir()):
        cfg = entry / "config.toml"
        if not entry.is_dir() or not cfg.is_file():
            continue
        counts = Counts(
            raw=count_files(entry / "raw"),
            processed=count_files(entry / "processed", ".md"),
            graded=count_files(entry / "graded", ".json"),
            scored=count_recursive(entry / "scored"),
        )
        mtimes: dict[str, float] = {}
        for stage in ("raw", "processed", "graded", "scored"):
            t = _max_file_mtime(entry / stage)
            if t is not None:
                mtimes[stage] = t
        t_plag = _max_file_mtime(entry / "plagiarism")
        if t_plag is not None:
            mtimes["plagiarism"] = t_plag
        infos.append(
            AssignmentInfo(
                dir_name=entry.name,
                config_path=cfg,
                assignment_id=_fetch_id(cfg, "assignment_id"),
                counts=counts,
                stage_mtime=mtimes,
                last_run=max(mtimes.values()) if mtimes else None,
                score_summary=_score_summary(entry / "scored"),
                flagged_pairs=_flagged_pairs(entry),
            )
        )
    return infos


def scan_courses(assignments_dir: Path) -> list[CourseInfo]:
    """Scan course dirs under ``assignments_dir`` with per-course aggregates."""
    courses: list[CourseInfo] = []
    if not assignments_dir.is_dir():
        return courses
    for entry in sorted(assignments_dir.iterdir()):
        cfg = entry / "config.toml"
        if (
            not entry.is_dir()
            or not cfg.is_file()
            or entry.name == "example"
            or not is_course_config(cfg)
        ):
            continue
        assignments = scan_assignments(entry)
        counts = Counts()
        score_parts: list[float] = []
        last_run: float | None = None
        for a in assignments:
            counts.raw += a.counts.raw
            counts.processed += a.counts.processed
            counts.graded += a.counts.graded
            counts.scored += a.counts.scored
            if a.score_summary is not None:
                score_parts.append(a.score_summary)
            if a.last_run is not None:
                last_run = a.last_run if last_run is None else max(last_run, a.last_run)
        courses.append(
            CourseInfo(
                dir_name=entry.name,
                config_path=cfg,
                course_id=_fetch_id(cfg, "course_id"),
                assignment_count=len(assignments),
                counts=counts,
                score_mean=(
                    sum(score_parts) / len(score_parts) if score_parts else None
                ),
                flagged_pairs=sum(a.flagged_pairs for a in assignments),
                last_run=last_run,
            )
        )
    return courses
