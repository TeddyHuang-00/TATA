"""T4a scan/app regressions: dirty pair JSON, display-threshold boundary,
.env ancestor walk. (MAJOR-2 / MINOR-7 fixes.)"""

from __future__ import annotations

import json
from pathlib import Path

from src.tata_scan import _flagged_pairs


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
    from src.tata_scan import scan_courses

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
    from src.tata_scan import (
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


def test_env_status_continues_up_after_incomplete_env(tmp_path: Path) -> None:
    """MINOR-7: a .env missing either key must not short-circuit the walk."""
    from src.tata_app import _env_status

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
