from __future__ import annotations

from pathlib import Path

import pytest
from src.shared.aliases import (
    assignment_display_name,
    course_display_name,
    course_student_display_name,
    load_alias_chain,
    load_alias_file,
    lookup,
    migrate_course_to_ids,
    seed_assignment_alias,
    seed_course_alias,
    set_alias,
    student_display_name,
    upsert_student_aliases,
)


def _write_chain(tmp_path: Path) -> Path:
    """Two-level chain: data/, data/111111/, .../a1/"""
    assignments = tmp_path / "data"
    (assignments / "111111" / "a1").mkdir(parents=True)
    (assignments / "alias.toml").write_text(
        '[course]\n"111111" = "Global Course"\n'
        '[assignment]\n"222222" = "Global Assign"\n'
        '[student]\n"1" = "Global One"\n',
        encoding="utf-8",
    )
    (assignments / "111111" / "alias.toml").write_text(
        '[course]\n"111111" = "Course 111111"\n'
        '[assignment]\n"222222" = "Course Assign"\n'
        '[student]\n"2" = "Course Two"\n',
        encoding="utf-8",
    )
    (assignments / "111111" / "a1" / "alias.toml").write_text(
        '[assignment]\n"222222" = "Local Assign"\n[student]\n"3" = "Local Three"\n',
        encoding="utf-8",
    )
    return assignments


def test_precedence_global_course_assignment(tmp_path: Path) -> None:
    assignments = _write_chain(tmp_path)
    aliases = load_alias_chain([
        assignments / "alias.toml",
        assignments / "111111" / "alias.toml",
        assignments / "111111" / "a1" / "alias.toml",
    ])
    # closer wins per key
    assert lookup(aliases, "course", "111111") == "Course 111111"
    assert lookup(aliases, "assignment", "222222") == "Local Assign"
    assert lookup(aliases, "student", "1") == "Global One"  # only in global
    assert lookup(aliases, "student", "2") == "Course Two"  # only in course
    assert lookup(aliases, "student", "3") == "Local Three"  # only in assignment


def test_display_names_and_fallbacks(tmp_path: Path) -> None:
    assignments = _write_chain(tmp_path)
    # course: alias wins over dir_name; fallback = dir_name (no id / unknown id)
    assert course_display_name(assignments, "111111", 111111) == "Course 111111"
    assert course_display_name(assignments, "111111", None) == "111111"
    assert course_display_name(assignments, "111111", 999) == "111111"
    assert course_display_name(assignments, "no-aliases-dir", 1) == "no-aliases-dir"
    # assignment: alias wins; fallback = dir_name
    assert (
        assignment_display_name(assignments, "111111", "a1", 222222) == "Local Assign"
    )
    assert assignment_display_name(assignments, "111111", "a1", None) == "a1"
    assert assignment_display_name(assignments, "111111", "a1", 999) == "a1"
    # student: alias wins; fallback = uid
    assert student_display_name(assignments, "111111", "a1", "2") == "Course Two"
    assert student_display_name(assignments, "111111", "a1", "nobody") == "nobody"


def test_course_student_display_name(tmp_path: Path) -> None:
    assignments = _write_chain(tmp_path)
    # global + course + assignment-level [student] tables all merge
    assert course_student_display_name(assignments, "111111", "1") == "Global One"
    assert course_student_display_name(assignments, "111111", "2") == "Course Two"
    assert course_student_display_name(assignments, "111111", "3") == "Local Three"
    # fallback = user_id
    assert course_student_display_name(assignments, "111111", "nobody") == "nobody"
    # missing course -> only global is in scope
    assert course_student_display_name(assignments, "no-course", "1") == "Global One"
    assert course_student_display_name(assignments, "no-course", "nobody") == "nobody"


def test_tolerant_load_missing_and_corrupt(tmp_path: Path) -> None:
    assert load_alias_file(tmp_path / "nope.toml") == {}
    bad = tmp_path / "bad.toml"
    bad.write_text("[student\nnot toml", encoding="utf-8")
    assert load_alias_file(bad) == {}
    unknown = tmp_path / "unknown.toml"
    unknown.write_text("[other]\nx = 1", encoding="utf-8")
    assert load_alias_file(unknown) == {}


def test_upsert_only_absent_keys_preserves_tables(tmp_path: Path) -> None:
    root = tmp_path / "assign"
    root.mkdir()
    (root / "alias.toml").write_text(
        "# manual comment\n"
        '[course]\n"111111" = "Example Course"\n'
        '[assignment]\n"222222" = "Assignment One"\n'
        '[student]\n"100" = "Manual, Override"\n'
        "unknown_key = 1\n",
        encoding="utf-8",
    )
    upsert_student_aliases(root, {"100": "Fetch, Value", "200": "New, Entry"})
    text = (root / "alias.toml").read_text()
    assert "Manual, Override" in text  # existing key untouched
    assert "New, Entry" in text  # missing key added
    assert "Fetch, Value" not in text  # not added (key exists)
    assert '[course]\n"111111" = "Example Course"' in text
    assert '[assignment]\n"222222" = "Assignment One"' in text
    assert "unknown_key = 1" in text
    assert "# manual comment" in text


def test_upsert_creates_file_with_header(tmp_path: Path) -> None:
    root = tmp_path / "assign"
    root.mkdir()
    upsert_student_aliases(root, {"42": "Doe, Jane"})
    text = (root / "alias.toml").read_text()
    assert text.startswith("# TATA alias.toml")
    file2 = load_alias_file(root / "alias.toml")
    assert file2["student"] == {"42": "Doe, Jane"}
    # adding again does not duplicate
    upsert_student_aliases(root, {"42": "Doe, Jane"})
    assert load_alias_file(root / "alias.toml")["student"] == {"42": "Doe, Jane"}


def test_set_alias_creates_and_cache_invalidates(tmp_path: Path) -> None:
    """New file (with header), in-place update, and the lru_cache is dropped:
    a second load sees the new value without a restart."""
    path = tmp_path / "alias.toml"
    set_alias(path, "course", "111111", "First Course")
    assert path.read_text().startswith("# TATA alias.toml")
    loaded = load_alias_file(path)  # populate the cache
    assert loaded["course"]["111111"] == "First Course"
    set_alias(path, "course", "111111", "Second Course")
    # cache was cleared by the write; no stale "First Course"
    assert load_alias_file(path)["course"]["111111"] == "Second Course"


def test_set_alias_preserves_other_content(tmp_path: Path) -> None:
    path = tmp_path / "alias.toml"
    path.write_text(
        "# manual comment\n"
        '[course]\n"111111" = "Old"\n'
        '[student]\n"1" = "One"\n'
        "unknown_key = 1\n",
        encoding="utf-8",
    )
    set_alias(path, "course", "111111", "New")
    text = path.read_text()
    assert "# manual comment" in text
    assert '"111111" = "New"' in text
    assert '[student]\n"1" = "One"' in text
    assert "unknown_key = 1" in text


def test_set_alias_empty_name_deletes_key(tmp_path: Path) -> None:
    path = tmp_path / "alias.toml"
    set_alias(path, "course", "111111", "Course")
    set_alias(path, "course", "111111", "")
    assert "111111" not in load_alias_file(path).get("course", {})
    # deleting an absent key is a no-op
    set_alias(path, "course", "missing", "")
    assert load_alias_file(path).get("course", {}) == {}


def test_set_alias_refuses_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "alias.toml"
    original = "[course\nnot toml"
    path.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt/unreadable"):
        set_alias(path, "course", "111111", "Nope")
    assert path.read_text(encoding="utf-8") == original  # untouched


def test_set_alias_refuses_non_table_section(tmp_path: Path) -> None:
    path = tmp_path / "alias.toml"
    path.write_text("course = 5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not a table"):
        set_alias(path, "course", "111111", "Nope")
    assert path.read_text(encoding="utf-8") == "course = 5\n"


def test_set_alias_rejects_unknown_section(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid alias section"):
        set_alias(tmp_path / "x.toml", "bogus", "k", "v")


def test_seed_course_alias_fill_missing(tmp_path: Path) -> None:
    assignments = tmp_path / "data"
    assignments.mkdir()
    (assignments / "alias.toml").write_text(
        '# manual\n[course]\n"111111" = "Manual Name"\n[student]\n"1" = "One"\n',
        encoding="utf-8",
    )
    seed_course_alias(assignments, 111111, "New Name")  # existing key untouched
    seed_course_alias(assignments, 222222, "Second Course")  # new key added
    text = (assignments / "alias.toml").read_text()
    assert load_alias_file(assignments / "alias.toml")["course"] == {
        "111111": "Manual Name",
        "222222": "Second Course",
    }
    assert "New Name" not in text
    assert load_alias_file(assignments / "alias.toml")["student"] == {"1": "One"}
    assert "# manual" in text


def test_seed_course_alias_creates_file_and_table(tmp_path: Path) -> None:
    assignments = tmp_path / "data"
    assignments.mkdir()
    seed_course_alias(assignments, 111111, "New Course")
    text = (assignments / "alias.toml").read_text()
    assert text.startswith("# TATA alias.toml")
    assert load_alias_file(assignments / "alias.toml")["course"] == {
        "111111": "New Course"
    }


def test_seed_assignment_alias_fill_missing(tmp_path: Path) -> None:
    course = tmp_path / "data" / "111111"
    course.mkdir(parents=True)
    (course / "alias.toml").write_text(
        '[assignment]\n"222222" = "Manual Assign"\n[student]\n"1" = "One"\n',
        encoding="utf-8",
    )
    seed_assignment_alias(course, 222222, "New Name")  # existing key untouched
    seed_assignment_alias(course, 333333, "Second Assign")  # new key added
    text = (course / "alias.toml").read_text()
    assert load_alias_file(course / "alias.toml")["assignment"] == {
        "222222": "Manual Assign",
        "333333": "Second Assign",
    }
    assert "New Name" not in text
    assert load_alias_file(course / "alias.toml")["student"] == {"1": "One"}


def test_seed_assignment_alias_creates_file_and_table(tmp_path: Path) -> None:
    course = tmp_path / "data" / "111111"
    course.mkdir(parents=True)
    seed_assignment_alias(course, 222222, "New Assignment")
    text = (course / "alias.toml").read_text()
    assert text.startswith("# TATA alias.toml")
    assert load_alias_file(course / "alias.toml")["assignment"] == {
        "222222": "New Assignment"
    }


def _make_course_tree(tmp_path: Path) -> Path:
    course = tmp_path / "data" / "111111"
    (course / "assignment-one" / "raw").mkdir(parents=True)
    (course / "assignment-one" / "config.toml").write_text(
        "[grading]\nrubric = 'x.toml'\nsystem_prompt = 'p.md'\nprovider = 'x'\n",
        encoding="utf-8",
    )
    (course / "assignment-one" / "roster.csv").write_text(
        "user_id,user_name,sortable_name,file\n"
        '100,"Alpha, A",Alpha A,100.txt\n'
        '200,"Q""ued, Name","Q"" Name",200.txt\n',
        encoding="utf-8",
    )
    (course / "config.toml").write_text(
        "[fetch]\ncourse_id = 111111\n[[fetch.assignments]]\nid = 222222\n",
        encoding="utf-8",
    )
    return course


def test_migrate_dry_run_reports_only(tmp_path: Path) -> None:
    course = _make_course_tree(tmp_path)
    actions = migrate_course_to_ids(course, dry_run=True)
    assert len(actions) == 3
    assert any(
        "rename" in a and "assignment-one" in a and "222222" in a for a in actions
    )
    assert any('"222222" = "assignment-one"' in a for a in actions)
    assert any("roster.csv" in a and "delete" in a for a in actions)
    # dry run changes nothing
    assert (course / "assignment-one").is_dir()
    assert (course / "assignment-one" / "roster.csv").is_file()
    assert "id = 222222" in (course / "config.toml").read_text()


def test_migrate_ran_and_idempotent(tmp_path: Path) -> None:
    course = _make_course_tree(tmp_path)
    actions = migrate_course_to_ids(course)
    assert len(actions) == 3
    # rename happened
    assert not (course / "assignment-one").exists()
    assert (course / "222222").is_dir()
    # course config entry keeps the id-only shape (no out)
    config_text = (course / "config.toml").read_text()
    assert "id = 222222" in config_text
    assert "out" not in config_text
    assert "assignment-one" not in config_text
    # course alias seeded with old dir name
    course_aliases = load_alias_file(course / "alias.toml")
    assert course_aliases["assignment"]["222222"] == "assignment-one"
    # roster -> assignment alias.toml, then deleted; quote-aware CSV rows
    assign_aliases = load_alias_file(course / "222222" / "alias.toml")
    assert assign_aliases["student"]["100"] == "Alpha A"
    # quote-aware CSV: embedded quote and comma survive
    assert assign_aliases["student"]["200"] == 'Q" Name'
    assert not (course / "222222" / "roster.csv").exists()
    # second run: no actions left
    assert migrate_course_to_ids(course) == []
    assert migrate_course_to_ids(course, dry_run=True) == []
