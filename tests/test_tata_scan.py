"""T4a scan/app regressions: dirty pair JSON, display-threshold boundary,
.env ancestor walk. (MAJOR-2 / MINOR-7 fixes.)"""

from __future__ import annotations

import json
from pathlib import Path

from src.tui.scan import _flagged_pairs


def _write_pairs(assignment_dir: Path, pairs: list) -> None:
    p = assignment_dir / "plagiarism" / "all_pairs.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"pairs": pairs}), encoding="utf-8")


def test_dirty_max_similarity_pct_does_not_crash(tmp_path: Path) -> None:
    """MAJOR-2: strings/None/non-dict entries must not crash the scan."""
    a1 = tmp_path / "a1"
    _write_pairs(
        a1,
        [
            {"test_file": "a", "reference_file": "b", "max_similarity_pct": "95.0"},
            {"test_file": "a", "reference_file": "c", "max_similarity_pct": None},
            {
                "test_file": "a",
                "reference_file": "d",
                "max_similarity_pct": "not-a-number",
            },
            "not-a-dict",
            {},
        ],
    )
    assert _flagged_pairs(a1) == 1  # only the "95.0" string counts


def test_display_threshold_boundary(tmp_path: Path) -> None:
    """80.0 counts as flagged, 79.9 does not (display threshold, not z-alpha)."""
    a1 = tmp_path / "a1"
    _write_pairs(
        a1,
        [
            {"test_file": "a", "reference_file": "b", "max_similarity_pct": 80.0},
            {"test_file": "a", "reference_file": "c", "max_similarity_pct": 79.9},
        ],
    )
    assert _flagged_pairs(a1) == 1


def test_course_config_display_threshold_drives_flags(tmp_path: Path) -> None:
    """Course [plagiarism] display_threshold (0.9) flags a 90% pair — the
    same threshold the Plagiarism pane uses (single display-threshold source)."""
    from src.tui.scan import scan_courses

    course = tmp_path / "data" / "c1"
    a1 = course / "a1"
    a1.mkdir(parents=True)
    (course / "config.toml").write_text(
        "[plagiarism]\ndisplay_threshold = 0.9\n", encoding="utf-8"
    )
    (a1 / "config.toml").write_text("", encoding="utf-8")
    _write_pairs(
        a1,
        [
            {"test_file": "a", "reference_file": "b", "max_similarity_pct": 90.0},
            {"test_file": "a", "reference_file": "c", "max_similarity_pct": 89.0},
        ],
    )
    courses = scan_courses(tmp_path / "data")
    assert len(courses) == 1
    assert courses[0].flagged_pairs == 1, courses[0].flagged_pairs


def test_malformed_plagiarism_config_falls_back_to_default(tmp_path: Path) -> None:
    """M1 regression: wrong-typed [plagiarism] display_threshold must not
    crash _plagiarism_threshold_pct/scan_courses; threshold falls back to the
    default 80.0 (dirty-data tolerance doctrine)."""
    from src.tui.scan import (
        DISPLAY_THRESHOLD_PCT,
        _plagiarism_threshold_pct,
        scan_courses,
    )

    course = tmp_path / "data" / "c1"
    a1 = course / "a1"
    a1.mkdir(parents=True)
    (course / "config.toml").write_text(
        '[plagiarism]\ndisplay_threshold = "not-a-number"\n',
        encoding="utf-8",
    )
    (a1 / "config.toml").write_text("", encoding="utf-8")
    _write_pairs(
        a1, [{"test_file": "a", "reference_file": "b", "max_similarity_pct": 90.0}]
    )
    assert _plagiarism_threshold_pct(course / "config.toml") == DISPLAY_THRESHOLD_PCT
    courses = scan_courses(tmp_path / "data")  # must not raise
    assert len(courses) == 1
    assert courses[0].flagged_pairs == 1  # flags use the 80.0 default


def test_assignment_id_from_numeric_dir_name(tmp_path: Path) -> None:
    """Assignment identity = the numeric dir name; non-numeric dirs -> None."""
    from src.tui.scan import scan_assignments

    course = tmp_path / "data" / "111111"
    for name in ("222222", "legacy-name"):
        (course / name).mkdir(parents=True)
        (course / name / "config.toml").write_text("", encoding="utf-8")
    infos = scan_assignments(course)
    by_name = {i.dir_name: i.assignment_id for i in infos}
    assert by_name["222222"] == 222222
    assert by_name["legacy-name"] is None


def test_raw_count_counts_student_folders_once(tmp_path: Path) -> None:
    """SUBMIT-ALL: raw counts top-level ITEMS — a multi-file student folder
    is one submission, dot-entries (.fetch-cache.json) are excluded."""
    from src.tui.scan import scan_assignments

    course = tmp_path / "data" / "111111"
    a1 = course / "222222"
    a1.mkdir(parents=True)
    (course / "config.toml").write_text("", encoding="utf-8")
    (a1 / "config.toml").write_text("", encoding="utf-8")
    raw = a1 / "raw"
    raw.mkdir()
    (raw / "100001.html").write_text("<p>a</p>", encoding="utf-8")
    (raw / ".fetch-cache.json").write_text("{}", encoding="utf-8")
    multi = raw / "100002"
    multi.mkdir()
    (multi / "100002.html").write_text("<p>a</p>", encoding="utf-8")
    (multi / "100002_0.ipynb").write_text("{}", encoding="utf-8")
    infos = scan_assignments(course)
    assert len(infos) == 1
    assert infos[0].counts.raw == 2  # 1 flat file + 1 folder student


def test_raw_count_dedupes_same_uid_flat_files(tmp_path: Path) -> None:
    """Item 1: ``<uid>.html`` + ``<uid>_1.ipynb`` both flat = ONE student."""
    from src.tui.scan import scan_assignments

    course = tmp_path / "data" / "111111"
    a1 = course / "222222"
    a1.mkdir(parents=True)
    (course / "config.toml").write_text("", encoding="utf-8")
    (a1 / "config.toml").write_text("", encoding="utf-8")
    raw = a1 / "raw"
    raw.mkdir()
    (raw / "100001.html").write_text("<p>a</p>", encoding="utf-8")
    (raw / "100001_1.ipynb").write_text("{}", encoding="utf-8")
    (raw / "100002.html").write_text("<p>b</p>", encoding="utf-8")
    infos = scan_assignments(course)
    assert infos[0].counts.raw == 2  # 100001 once + 100002 once


def test_raw_count_skips_stale_flat_leftovers(tmp_path: Path) -> None:
    """Item 1: parity with preprocess' mixed-layout stale-flat rule."""
    from src.tui.scan import scan_assignments

    course = tmp_path / "data" / "111111"
    a1 = course / "222222"
    a1.mkdir(parents=True)
    (course / "config.toml").write_text("", encoding="utf-8")
    (a1 / "config.toml").write_text("", encoding="utf-8")
    raw = a1 / "raw"
    (raw / "415019").mkdir(parents=True)
    (raw / "415019" / "415019.html").write_text("<p>a</p>", encoding="utf-8")
    (raw / "415019.docx").write_bytes(b"stale flat")
    (raw / "415019_1.docx").write_bytes(b"stale flat")
    infos = scan_assignments(course)
    assert infos[0].counts.raw == 1


def test_counts_exclude_reference_and_dedupe_graded(tmp_path: Path) -> None:
    """Item 1: reference md is not a student; graded dedupes _LATE_ stems."""
    from src.tui.scan import scan_assignments

    course = tmp_path / "data" / "111111"
    a1 = course / "222222"
    a1.mkdir(parents=True)
    (course / "config.toml").write_text("", encoding="utf-8")
    (a1 / "config.toml").write_text(
        '[grading]\nrubric = "r.toml"\nsystem_prompt = ["p.md"]\nprovider = "test"\n'
        '[assignment]\nreference_file = "reference.md"\n',
        encoding="utf-8",
    )
    processed = a1 / "processed"
    processed.mkdir(parents=True)
    (processed / "100001.md").write_text("# s", encoding="utf-8")
    (processed / "reference.md").write_text("# ref", encoding="utf-8")
    (processed / ".preprocess.cache.json").write_text("{}", encoding="utf-8")
    graded = a1 / "graded"
    graded.mkdir()
    (graded / "100001.json").write_text("{}", encoding="utf-8")
    (graded / "100001_LATE_0.json").write_text("{}", encoding="utf-8")
    infos = scan_assignments(course)
    assert infos[0].counts.processed == 1  # reference.md excluded
    assert infos[0].counts.graded == 1  # 100001 and 100001_LATE_0 same uid


def test_env_status_continues_up_after_incomplete_env(tmp_path: Path) -> None:
    """MINOR-7: a .env missing either key must not short-circuit the walk."""
    from src.tui.app import _env_status

    root = tmp_path / "proj"
    root.mkdir()
    (root / ".env").write_text(
        "CANVAS_BASE_URL=https://canvas.example.com\nCANVAS_ACCESS_TOKEN=abc\n",
        encoding="utf-8",
    )
    sub = root / "data"
    sub.mkdir()
    (sub / ".env").write_text("CANVAS_BASE_URL=https://broken\n", encoding="utf-8")

    status = _env_status(sub)
    assert status["has_env"] is True
    assert status["base_url"] == "https://canvas.example.com"
