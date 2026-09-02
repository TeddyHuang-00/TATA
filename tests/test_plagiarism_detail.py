"""Unit tests for the pure plagiarism-detail helpers (S4 feedback 3)."""

from __future__ import annotations

from pathlib import Path

from src.tui.plagiarism_detail import (
    find_pair_for_uids,
    pair_uids,
    parse_uid,
    render_histogram,
)


def test_parse_uid_label_and_uid() -> None:
    assert parse_uid("Mia(415019)") == "415019"
    assert parse_uid("415019") == "415019"
    assert parse_uid("mia") == "mia"
    assert parse_uid("name:mia") == "name:mia"


def test_pair_uids_and_find(tmp_path: Path) -> None:
    absolute = tmp_path / "data" / "c1" / "a1" / "processed" / "415019.md"
    pairs = [
        {
            "test_file": str(absolute),
            "reference_file": "415020_LATE_1.md",
            "max_similarity_pct": 91.2,
        },
        {
            "test_file": "1001.md",
            "reference_file": "1002.md",
            "max_similarity_pct": 88.0,
        },
    ]
    # absolute path + _LATE_N suffix stripped to the base uid
    assert pair_uids(pairs[0]) == frozenset({"415019", "415020"})
    assert pair_uids(pairs[1]) == frozenset({"1001", "1002"})
    assert find_pair_for_uids(pairs, frozenset({"415019", "415020"})) is pairs[0]
    assert find_pair_for_uids(pairs, frozenset({"1002", "1001"})) is pairs[1]
    assert find_pair_for_uids(pairs, frozenset({"999", "1002"})) is None


def test_render_histogram_bins_counts_and_highlight() -> None:
    lines = render_histogram([95.0, 98.0, 92.0, 55.0, 44.0, "70.0"], highlight=95.0)
    assert len(lines) == 10
    # peak bin (3): full width, marked, yellow, arrow suffix
    assert lines[9].startswith("[yellow]◆ ")
    assert "90-100%" in lines[9]
    assert lines[9].count("█") == 48
    assert lines[9].endswith("← A-B[/yellow]")
    # 70.0 -> 70-80% bin, one entry
    assert lines[7].lstrip().startswith("70-80%")
    assert lines[7].count("█") == 16  # scaled round(1/3 * 48)
    assert "A-B" not in lines[7]
    # 55.0 -> 50-60% bin
    assert lines[5].lstrip().startswith("50-60%")


def test_render_histogram_empty_and_no_highlight() -> None:
    lines = render_histogram([], highlight=None)
    assert len(lines) == 10
    assert all("█" not in line for line in lines)
    lines2 = render_histogram([91.2, 88.0])
    assert lines2[9].count("█") == 48  # peak=2 -> full width for 91.2's bin
    assert "A-B" not in "\n".join(lines2)
