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
    _write_pairs(a1, [
        {"test_file": "a", "reference_file": "b", "max_similarity_pct": "95.0"},
        {"test_file": "a", "reference_file": "c", "max_similarity_pct": None},
        {"test_file": "a", "reference_file": "d", "max_similarity_pct": "not-a-number"},
        "not-a-dict",
        {},
    ])
    assert _flagged_pairs(a1) == 1  # only the "95.0" string counts


def test_display_threshold_boundary(tmp_path: Path) -> None:
    """80.0 counts as flagged, 79.9 does not (display threshold, not z-alpha)."""
    a1 = tmp_path / "a1"
    _write_pairs(a1, [
        {"test_file": "a", "reference_file": "b", "max_similarity_pct": 80.0},
        {"test_file": "a", "reference_file": "c", "max_similarity_pct": 79.9},
    ])
    assert _flagged_pairs(a1) == 1


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
