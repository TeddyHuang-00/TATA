"""Regression tests for the cross-assignment aggregate (audit fixes).

- ``_extract_assignment_name`` took the first component of the path relative
  to the assignments root: with the real nested layout
  (``data/<course>/<assignment>/plagiarism/all_pairs.json`` globbed from
  ``data/``) that is the *course* name, so every assignment pooled into one
  and the per-assignment z-scores / Stouffer K were computed over the pool.
- ``_load_assignment_records`` overwrote ``per_assignment_records[name]`` on
  a name collision: two files resolving to the same name kept only the last
  one while ``pair_data_parsed`` counted both.
- ``_student_identity`` split one student in two: ``<uid>__<member>.py``
  (folder extraction) resolved to ``id:<uid>`` while flat ``<uid>.py`` and
  ``<uid>_LATE_0.py`` resolved to ``name:<uid>``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from src.shared.plagiarism_aggregate import (
    BuildConfig,
    _extract_assignment_name,
    _load_assignment_records,
    _student_identity,
)

_PAIRS_PAYLOAD = {
    "version": 1,
    "test_file_count": 2,
    "reference_file_count": 2,
    "pair_count": 1,
    "pairs": [
        {
            "test_file": "100.md",
            "reference_file": "200.md",
            "test_similarity_pct": 90.0,
            "reference_similarity_pct": 90.0,
            "max_similarity_pct": 90.0,
            "token_overlap": 12,
        }
    ],
}


def _config(files: list[Path], root: Path) -> BuildConfig:
    return BuildConfig(
        assignments_root=root,
        pairs_glob="**/all_pairs.json",
        pairwise_alpha=0.01,
        individual_alpha=0.01,
        score_floor=0.001,
        score_cap=0.999,
        pair_data_files=files,
    )


# -- _extract_assignment_name ----------------------------------------------


def test_assignment_name_is_the_dir_above_the_output_dir() -> None:
    # Standard two-level layout (root = assignments root).
    assert _extract_assignment_name(Path("data/a1/plagiarism/all_pairs.json")) == "a1"


def test_assignment_name_survives_nested_course_dirs() -> None:
    """Regression: globbed from data/ (root one level above the course dir),
    the old parts[0] rule returned the course name for every assignment."""
    nested = Path("data/271218/3042922/plagiarism/all_pairs.json")
    assert _extract_assignment_name(nested) == "3042922"


# -- _student_identity -------------------------------------------------------


def test_student_identity_is_the_leading_uid_for_every_shape() -> None:
    """Regression: one student, three file shapes, one identity."""
    flat = _student_identity("281819.py")
    folder = _student_identity("281819__281819_1.py")
    late = _student_identity("390489_LATE_0.py")
    assert flat == ("id:281819", "281819")
    assert folder == ("id:281819", "281819")
    assert late == ("id:390489", "390489")
    assert flat[0] == folder[0]  # the split the longitudinal Review hit


def test_student_identity_name_plus_id_shape_unchanged() -> None:
    assert _student_identity("alice_12345.py") == ("id:12345", "alice(12345)")
    assert _student_identity("alice_lab2.py") == ("name:alice", "alice")


# -- _load_assignment_records -----------------------------------------------


def test_same_name_files_merge_instead_of_overwriting(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path]
) -> None:
    """Regression: a name collision (same-named assignment dirs at different
    depths) used to keep only the last file's records while pair_data_parsed
    counted both — now both files' records are present."""
    root = tmp_path / "data"
    payload = json.dumps(_PAIRS_PAYLOAD)
    file_a = write_tree(root, "a1/plagiarism/all_pairs.json", payload)
    file_b = write_tree(root, "course/a1/plagiarism/all_pairs.json", payload)

    files, parsed, errors, per_assignment = _load_assignment_records(
        _config([file_a, file_b], root)
    )

    assert parsed == 2
    assert errors == 0
    assert len(files) == 2
    assert len(per_assignment["a1"]) == 2  # both files' records survive
