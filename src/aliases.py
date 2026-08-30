"""Unified alias.toml configuration for display names.

Each alias.toml holds up to three optional tables — ``[course]``,
``[assignment]``, ``[student]`` — mapping string IDs to display names::

    [course]
    "271218" = "ITCS 5153"
    [assignment]
    "2978557" = "First Colab"
    [student]
    "412607" = "Aalla, Movin Reddy"

Files merge at three levels, closer wins (later files in the chain override
earlier ones, per table and per key):

* global:     ``<assignments_dir>/alias.toml``
* course:     ``<assignments_dir>/<course_dir_name>/alias.toml``
* assignment: ``<assignments_dir>/<course_dir_name>/<assignment_dir_name>/alias.toml``

Reading is tolerant (missing/corrupt files return ``{}``); writing is
field-level TOML patching (never whole-file rewrite) so manual edits survive.
"""
from __future__ import annotations

import argparse
import csv
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

import tomlkit

from src.assignment_config import FetchSection, load_root_section

SECTIONS = ("course", "assignment", "student")

# roster.csv columns: user_id, user_name, sortable_name, file (file optional)
_ROSTER_COLS = 3

_HEADER = (
    "# TATA alias.toml: optional [course]/[assignment]/[student] display names.\n"
    "# Keys are string IDs; values are display names. Merge order: global ->\n"
    "# course -> assignment (closer files win). Entries added by fetch only\n"
    "# fill missing keys (manual edits here are preserved).\n"
)


def load_alias_file(path: Path) -> dict[str, dict[str, str]]:
    """Parse one alias.toml into {section: {id: name}}; {} on any problem."""
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    out: dict[str, dict[str, str]] = {}
    for section in SECTIONS:
        table = data.get(section)
        if isinstance(table, dict):
            out[section] = {
                str(k): str(v) for k, v in table.items() if isinstance(v, str)
            }
    return out


def load_alias_chain(paths: Sequence[Path]) -> dict[str, dict[str, str]]:
    """Merge alias files in order; later files (and later sections) win."""
    merged: dict[str, dict[str, str]] = {}
    for path in paths:
        for section, table in load_alias_file(path).items():
            merged.setdefault(section, {}).update(table)
    return merged


def lookup(aliases: dict[str, dict[str, str]], section: str, key: str) -> str | None:
    table = aliases.get(section)
    return table.get(key) if table else None


def _section_paths(assignments_dir: Path, course: str, assignment: str) -> list[Path]:
    return [
        assignments_dir / "alias.toml",
        assignments_dir / course / "alias.toml",
        assignments_dir / course / assignment / "alias.toml",
    ]


def course_display_name(
    assignments_dir: Path, dir_name: str, course_id: int | None
) -> str:
    aliases = load_alias_chain(
        [assignments_dir / "alias.toml", assignments_dir / dir_name / "alias.toml"]
    )
    if course_id is not None:
        name = lookup(aliases, "course", str(course_id))
        if name:
            return name
    return dir_name


def assignment_display_name(
    assignments_dir: Path,
    course_dir_name: str,
    dir_name: str,
    assignment_id: int | None,
) -> str:
    aliases = load_alias_chain(
        _section_paths(assignments_dir, course_dir_name, dir_name)
    )
    if assignment_id is not None:
        name = lookup(aliases, "assignment", str(assignment_id))
        if name:
            return name
    return dir_name


def student_display_name(
    assignments_dir: Path,
    course_dir_name: str,
    assignment_dir_name: str,
    user_id: str,
) -> str:
    aliases = load_alias_chain(
        _section_paths(assignments_dir, course_dir_name, assignment_dir_name)
    )
    return lookup(aliases, "student", user_id) or user_id


def course_student_display_name(
    assignments_dir: Path, course_dir_name: str, user_id: str
) -> str:
    """Course-scoped student name: global + course + every assignment-level
    [student] table of the course (later files win; child dirs in sorted
    order), or ``user_id`` itself when unaliased.

    Used by course-level surfaces (aggregate table) where a student appears
    across assignments without one assignment's alias.toml in scope.
    """
    paths = [
        assignments_dir / "alias.toml",
        assignments_dir / course_dir_name / "alias.toml",
    ]
    course_dir = assignments_dir / course_dir_name
    if course_dir.is_dir():
        paths.extend(
            course_dir / child.name / "alias.toml"
            for child in sorted(course_dir.iterdir())
            if child.is_dir()
        )
    aliases = load_alias_chain(paths)
    return lookup(aliases, "student", user_id) or user_id


# -- field-level TOML patching (mirrors canvas_fetch's [fetch] patching) ----

def _open_alias_doc(path: Path) -> tomlkit.TOMLDocument:
    """Parse an alias.toml; missing files start from the header comment,
    corrupt files from an empty document (reads tolerate them too)."""
    if path.exists():
        try:
            return tomlkit.parse(path.read_text(encoding="utf-8"))
        except tomlkit.exceptions.ParseError:
            return tomlkit.parse("")
    return tomlkit.parse(_HEADER)


def _write_doc(path: Path, doc: tomlkit.TOMLDocument) -> None:
    text = tomlkit.dumps(doc)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def upsert_student_aliases(assignment_root: Path, entries: dict[str, str]) -> None:
    """Add [student] entries to ``assignment_root/alias.toml`` (create if
    missing), only for keys that are absent — manual overrides and every
    other table/section/unknown key are left untouched."""
    path = assignment_root / "alias.toml"
    entries = {str(k): str(v) for k, v in entries.items()}
    doc = _open_alias_doc(path)
    if "student" not in doc:
        doc["student"] = {}
    student = doc["student"]
    for key, value in entries.items():
        if key not in student:
            student[key] = value
    _write_doc(path, doc)


# -- one-time migration: dir names -> assignment ids -----------------------

def _fetch_assignment_id(config_path: Path) -> int | None:
    try:
        fetch = load_root_section(config_path, "fetch", FetchSection)
    except (OSError, ValueError):
        return None
    if fetch is None:
        return None
    aid = fetch.assignment_id
    return aid if isinstance(aid, int) and not isinstance(aid, bool) else None


def _roster_entries(roster: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    try:
        rows = list(csv.reader(roster.read_text(encoding="utf-8").splitlines()))
    except OSError:
        return entries
    for fields in rows:
        if len(fields) >= _ROSTER_COLS and fields[0] != "user_id":
            entries[fields[0]] = fields[2] or fields[1] or fields[0]
    return entries


def _seed_section(alias_path: Path, table: str, key: str, value: str) -> None:
    """Add ``key = value`` to ``[table]`` in alias_path if the key is absent."""
    doc = _open_alias_doc(alias_path)
    if table not in doc:
        doc[table] = {}
    if key not in doc[table]:
        doc[table][key] = value
    _write_doc(alias_path, doc)


def migrate_course_to_ids(course_dir: Path, *, dry_run: bool = False) -> list[str]:
    """ONE-TIME migration: rename each child dir to its str(assignment_id),
    patch the course config's ``[[fetch.assignments]]`` ``out`` paths, seed
    the course alias.toml ``[assignment]`` and the assignment alias.toml
    ``[student]`` from roster.csv, then delete roster.csv.

    Returns one description string per intended action; with ``dry_run=True``
    it only reports and changes nothing. Idempotent: once every child dir is
    named after its id, the second run yields no actions.
    """
    course_config = course_dir / "config.toml"
    targets: list[tuple[Path, str, dict[str, str]]] = []
    for child in sorted(course_dir.iterdir()):
        if not child.is_dir():
            continue
        aid = _fetch_assignment_id(child / "config.toml")
        if aid is None or child.name == str(aid):
            continue
        new_name = str(aid)
        roster = child / "roster.csv"
        entries = _roster_entries(roster) if roster.is_file() else {}
        targets.append((child, new_name, entries))

    actions: list[str] = []
    for child, new_name, entries in targets:
        actions.extend(
            [
                f"rename {child} -> {child.with_name(new_name)}",
                f'patch "{child.name}/raw" -> "{new_name}/raw" in {course_config}',
                f'seed [assignment] "{new_name}" = "{child.name}" in '
                f"{course_dir / 'alias.toml'}",
            ]
        )
        if entries:
            actions.append(
                f"seed [student] from {child / 'roster.csv'} -> "
                f"{child.with_name(new_name) / 'alias.toml'} "
                f"({len(entries)} entries), delete roster.csv"
            )

    if dry_run:
        return actions

    for child, new_name, entries in targets:
        child.rename(child.with_name(new_name))
        migrated = child.with_name(new_name)
        if course_config.is_file():
            text = course_config.read_text(encoding="utf-8")
            patched = text.replace(f'"{child.name}/raw"', f'"{new_name}/raw"').replace(
                f'"{child.name}"', f'"{new_name}"'
            )
            if patched != text:
                course_config.write_text(patched, encoding="utf-8")
        _seed_section(course_dir / "alias.toml", "assignment", new_name, child.name)
        if entries:
            upsert_student_aliases(migrated, entries)
        (migrated / "roster.csv").unlink(missing_ok=True)
    return actions


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="python -m src.aliases",
        description="TATA alias.toml ops: one-time migrate of a course dir "
        "from display-name dirs to assignment-id dirs.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    mig = sub.add_parser(
        "migrate", help="migrate a course dir to id-named assignment dirs"
    )
    mig.add_argument("course_dir", help="path to the course directory")
    mig.add_argument("--dry-run", action="store_true", help="report only, no changes")
    opts = parser.parse_args(sys.argv[1:])
    reported = migrate_course_to_ids(Path(opts.course_dir), dry_run=opts.dry_run)
    for action in reported:
        print(action)
    if not reported:
        print(f"no actions for {opts.course_dir}" + (" (dry run)" if opts.dry_run else ""))
