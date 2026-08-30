from __future__ import annotations

from pathlib import Path

from src.tata_alias import (
    assignment_display_name,
    course_display_name,
    course_student_display_name,
    load_alias_chain,
    load_alias_file,
    lookup,
    migrate_course_to_ids,
    student_display_name,
    upsert_student_aliases,
)


def _write_chain(tmp_path: Path) -> Path:
    """Two-level chain: assignments/, assignments/271218/, .../a1/"""
    assignments = tmp_path / "assignments"
    (assignments / "271218" / "a1").mkdir(parents=True)
    (assignments / "alias.toml").write_text(
        '[course]\n"271218" = "Global Course"\n'
        '[assignment]\n"2978557" = "Global Assign"\n'
        '[student]\n"1" = "Global One"\n',
        encoding="utf-8",
    )
    (assignments / "271218" / "alias.toml").write_text(
        '[course]\n"271218" = "Course 271218"\n'
        '[assignment]\n"2978557" = "Course Assign"\n'
        '[student]\n"2" = "Course Two"\n',
        encoding="utf-8",
    )
    (assignments / "271218" / "a1" / "alias.toml").write_text(
        '[assignment]\n"2978557" = "Local Assign"\n'
        '[student]\n"3" = "Local Three"\n',
        encoding="utf-8",
    )
    return assignments


def test_precedence_global_course_assignment(tmp_path: Path) -> None:
    assignments = _write_chain(tmp_path)
    aliases = load_alias_chain(
        [
            assignments / "alias.toml",
            assignments / "271218" / "alias.toml",
            assignments / "271218" / "a1" / "alias.toml",
        ]
    )
    # closer wins per key
    assert lookup(aliases, "course", "271218") == "Course 271218"
    assert lookup(aliases, "assignment", "2978557") == "Local Assign"
    assert lookup(aliases, "student", "1") == "Global One"  # only in global
    assert lookup(aliases, "student", "2") == "Course Two"  # only in course
    assert lookup(aliases, "student", "3") == "Local Three"  # only in assignment


def test_display_names_and_fallbacks(tmp_path: Path) -> None:
    assignments = _write_chain(tmp_path)
    # course: alias wins over dir_name; fallback = dir_name (no id / unknown id)
    assert (
        course_display_name(assignments, "271218", 271218) == "Course 271218"
    )
    assert course_display_name(assignments, "271218", None) == "271218"
    assert course_display_name(assignments, "271218", 999) == "271218"
    assert course_display_name(assignments, "no-aliases-dir", 1) == "no-aliases-dir"
    # assignment: alias wins; fallback = dir_name
    assert (
        assignment_display_name(assignments, "271218", "a1", 2978557)
        == "Local Assign"
    )
    assert assignment_display_name(assignments, "271218", "a1", None) == "a1"
    assert assignment_display_name(assignments, "271218", "a1", 999) == "a1"
    # student: alias wins; fallback = uid
    assert student_display_name(assignments, "271218", "a1", "2") == "Course Two"
    assert student_display_name(assignments, "271218", "a1", "nobody") == "nobody"


def test_course_student_display_name(tmp_path: Path) -> None:
    assignments = _write_chain(tmp_path)
    # global + course + assignment-level [student] tables all merge
    assert course_student_display_name(assignments, "271218", "1") == "Global One"
    assert course_student_display_name(assignments, "271218", "2") == "Course Two"
    assert course_student_display_name(assignments, "271218", "3") == "Local Three"
    # fallback = user_id
    assert course_student_display_name(assignments, "271218", "nobody") == "nobody"
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
        '[course]\n"271218" = "ITCS 5153"\n'
        '[assignment]\n"2978557" = "First Colab"\n'
        '[student]\n"100" = "Manual, Override"\n'
        "unknown_key = 1\n",
        encoding="utf-8",
    )
    upsert_student_aliases(root, {"100": "Fetch, Value", "200": "New, Entry"})
    text = (root / "alias.toml").read_text()
    assert "Manual, Override" in text  # existing key untouched
    assert "New, Entry" in text  # missing key added
    assert "Fetch, Value" not in text  # not added (key exists)
    assert '[course]\n"271218" = "ITCS 5153"' in text
    assert '[assignment]\n"2978557" = "First Colab"' in text
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


def _make_course_tree(tmp_path: Path) -> Path:
    course = tmp_path / "assignments" / "271218"
    (course / "first-colab" / "raw").mkdir(parents=True)
    (course / "first-colab" / "config.toml").write_text(
        '[fetch]\nassignment_id = 2978557\nmode = "text"\n', encoding="utf-8"
    )
    (course / "first-colab" / "roster.csv").write_text(
        'user_id,user_name,sortable_name,file\n'
        '100,"Alpha, A",Alpha A,100.txt\n'
        '200,"Q""ued, Name","Q"" Name",200.txt\n',
        encoding="utf-8",
    )
    (course / "config.toml").write_text(
        '[fetch]\ncourse_id = 271218\n'
        '[[fetch.assignments]]\nassignment_id = 2978557\n'
        'out = "first-colab/raw"\n',
        encoding="utf-8",
    )
    return course


def test_migrate_dry_run_reports_only(tmp_path: Path) -> None:
    course = _make_course_tree(tmp_path)
    actions = migrate_course_to_ids(course, dry_run=True)
    assert len(actions) == 4
    assert any("rename" in a and "first-colab" in a and "2978557" in a for a in actions)
    assert any('patch "first-colab/raw" -> "2978557/raw"' in a for a in actions)
    assert any('"2978557" = "first-colab"' in a for a in actions)
    assert any("roster.csv" in a and "delete" in a for a in actions)
    # dry run changes nothing
    assert (course / "first-colab").is_dir()
    assert (course / "first-colab" / "roster.csv").is_file()
    assert "2978557/raw" not in (course / "config.toml").read_text()


def test_migrate_ran_and_idempotent(tmp_path: Path) -> None:
    course = _make_course_tree(tmp_path)
    actions = migrate_course_to_ids(course)
    assert len(actions) == 4
    # rename happened
    assert not (course / "first-colab").exists()
    assert (course / "2978557").is_dir()
    # out patched
    config_text = (course / "config.toml").read_text()
    assert 'out = "2978557/raw"' in config_text
    assert "first-colab" not in config_text
    # course alias seeded with old dir name
    course_aliases = load_alias_file(course / "alias.toml")
    assert course_aliases["assignment"]["2978557"] == "first-colab"
    # roster -> assignment alias.toml, then deleted; quote-aware CSV rows
    assign_aliases = load_alias_file(course / "2978557" / "alias.toml")
    assert assign_aliases["student"]["100"] == "Alpha A"
    # quote-aware CSV: embedded quote and comma survive
    assert assign_aliases["student"]["200"] == 'Q" Name'
    assert not (course / "2978557" / "roster.csv").exists()
    # second run: no actions left
    assert migrate_course_to_ids(course) == []
    assert migrate_course_to_ids(course, dry_run=True) == []
