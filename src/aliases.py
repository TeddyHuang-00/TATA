"""Unified alias.toml configuration for display names.

Each alias.toml holds up to three optional tables — ``[course]``,
``[assignment]``, ``[student]`` — mapping string IDs to display names::

    [course]
    "111111" = "Example Course"
    [assignment]
    "222222" = "Example Assignment"
    [student]
    "100001" = "Example Student"

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
from collections.abc import MutableMapping, Sequence
from functools import lru_cache
from pathlib import Path

import tomlkit

SECTIONS = ("course", "assignment", "student")

# roster.csv columns: user_id, user_name, sortable_name, file (file optional)
_ROSTER_COLS = 3

_HEADER = (
    "# TATA alias.toml: optional [course]/[assignment]/[student] display names.\n"
    "# Keys are string IDs; values are display names. Merge order: global ->\n"
    "# course -> assignment (closer files win). Entries added by fetch only\n"
    "# fill missing keys (manual edits here are preserved).\n"
)


@lru_cache(maxsize=128)
def load_alias_file(path: Path) -> dict[str, dict[str, str]]:
    """Parse one alias.toml into {section: {id: name}}; {} on any problem.

    Cached by path: in-process writers (``_write_doc``) clear the cache, so
    reads (per row render) stay fast.  Note the cache can go stale: the file
    can also change via EXTERNAL processes while the app runs (hand edits,
    ``migrate_course_to_ids``) — the next read keeps the old entries until
    app restart.  Externally-edited files are picked up only on restart
    (or after an in-process write clears the entry).
    """
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
    aliases = load_alias_chain([
        assignments_dir / "alias.toml",
        assignments_dir / dir_name / "alias.toml",
    ])
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
    # Alias key: the assignment identity is the dir name (id-keyed aliases
    # only resolve when the dir name is numeric — the post-cleanup layout).
    key = str(assignment_id) if assignment_id is not None else dir_name
    name = lookup(aliases, "assignment", key)
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
    load_alias_file.cache_clear()  # the file changed on disk — drop stale reads


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


def _normalize_entries(doc: tomlkit.TOMLDocument) -> bool:
    """Rewrite ``[[fetch.assignments]]`` entries to the id-only shape:
    ``assignment_id`` -> ``id``, ``out`` dropped. Returns True when anything
    changed."""
    fetch = doc.get("fetch")
    if not isinstance(fetch, MutableMapping):
        return False
    entries = fetch.get("assignments")
    if not isinstance(entries, list):
        return False
    changed = False
    for entry in entries:
        if not isinstance(entry, MutableMapping):
            continue
        if "assignment_id" in entry:
            entry["id"] = entry.pop("assignment_id")
            changed = True
        if "out" in entry:
            del entry["out"]
            changed = True
    return changed


def _course_entries(course_config: Path) -> tuple[list[int], bool]:
    """(sorted ids, needs normalization) of a course config's
    ``[[fetch.assignments]]`` list; ([], False) on a missing/unreadable
    file. Ids accept either key shape (``id`` or legacy ``assignment_id``)."""
    try:
        doc = tomlkit.parse(course_config.read_text(encoding="utf-8"))
    except (OSError, tomlkit.exceptions.ParseError):
        return [], False
    fetch = doc.get("fetch")
    if not isinstance(fetch, MutableMapping):
        return [], False
    entries = fetch.get("assignments")
    if not isinstance(entries, list):
        return [], False
    ids: list[int] = []
    needs_norm = False
    for entry in entries:
        if not isinstance(entry, MutableMapping):
            continue
        value = entry.get("id") or entry.get("assignment_id")
        if isinstance(value, int) and not isinstance(value, bool):
            ids.append(value)
        if "assignment_id" in entry or "out" in entry:
            needs_norm = True
    return sorted(ids), needs_norm


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


def seed_course_alias(assignments_dir: Path, course_id: int, name: str) -> None:
    """Fill-missing ``[course] str(course_id) = name`` in the global
    ``assignments_dir/alias.toml`` (create file/table when absent; a
    pre-existing key — a manual edit — is never overwritten)."""
    _seed_section(assignments_dir / "alias.toml", "course", str(course_id), name)


def seed_assignment_alias(course_dir: Path, assignment_id: int, name: str) -> None:
    """Fill-missing ``[assignment] str(assignment_id) = name`` in the course
    ``course_dir/alias.toml`` (create file/table when absent; a pre-existing
    key — a manual edit — is never overwritten)."""
    _seed_section(course_dir / "alias.toml", "assignment", str(assignment_id), name)


def _resolve_renames(
    course_dir: Path,
    course_config: Path,
    pending_dirs: list[str],
    entry_ids: list[int],
) -> tuple[list[tuple[Path, str, dict[str, str]]], list[str]]:
    """Map non-numeric dirs to course-list ids by index order (sorted dirs
    vs sorted entries). Returns (targets, ambiguity messages): when the
    counts differ or an id already names a child dir, no targets and a
    report message (the dirs are left unchanged)."""
    if len(pending_dirs) != len(entry_ids):
        return [], [
            f"ambiguous: {len(pending_dirs)} named dirs vs {len(entry_ids)} "
            f"[[fetch.assignments]] entries in {course_config}; "
            "dirs left unchanged"
        ]
    occupied = {
        child.name
        for child in course_dir.iterdir()
        if child.is_dir() and child.name.isdigit()
    }
    if any(str(aid) in occupied for aid in entry_ids):
        return [], [
            f"ambiguous: an id in {course_config} already names a child dir; "
            "dirs left unchanged"
        ]
    targets = []
    for child_name, aid in zip(pending_dirs, entry_ids, strict=True):
        child = course_dir / child_name
        roster = child / "roster.csv"
        targets.append((
            child,
            str(aid),
            _roster_entries(roster) if roster.is_file() else {},
        ))
    return targets, []


def migrate_course_to_ids(course_dir: Path, *, dry_run: bool = False) -> list[str]:
    """ONE-TIME migration: rename each non-numeric child dir to its
    str(assignment_id), normalize the course config's ``[[fetch.assignments]]``
    entries (assignment_id -> id, drop out), seed the course alias.toml
    ``[assignment]`` and the assignment alias.toml ``[student]`` from
    roster.csv, then delete roster.csv.

    Ids come from the course config's list, matched to dirs by index order
    (sorted dirs vs sorted entries) — assignment configs no longer carry
    [fetch]. A count mismatch or a collision with an already-numeric dir is
    ambiguous: the dirs are left unchanged and reported. Returns one
    description string per intended action; with ``dry_run=True`` it only
    reports and changes nothing. Idempotent: once every child dir is named
    after its id, the second run yields no actions.
    """
    course_config = course_dir / "config.toml"
    entry_ids, needs_norm = _course_entries(course_config)
    pending = sorted(
        child.name
        for child in course_dir.iterdir()
        if child.is_dir()
        and not child.name.isdigit()
        and (child / "config.toml").is_file()
    )

    targets: list[tuple[Path, str, dict[str, str]]] = []
    if pending:
        targets, ambiguity = _resolve_renames(
            course_dir, course_config, pending, entry_ids
        )
        if ambiguity:
            return ambiguity

    actions: list[str] = []
    if needs_norm:
        actions.append(
            "normalize [[fetch.assignments]] (assignment_id -> id, drop out) "
            f"in {course_config}"
        )
    for child, new_name, entries in targets:
        actions.extend([
            f"rename {child} -> {child.with_name(new_name)}",
            f'seed [assignment] "{new_name}" = "{child.name}" in '
            f"{course_dir / 'alias.toml'}",
        ])
        if entries:
            actions.append(
                f"seed [student] from {child / 'roster.csv'} -> "
                f"{child.with_name(new_name) / 'alias.toml'} "
                f"({len(entries)} entries), delete roster.csv"
            )

    if dry_run:
        return actions

    if course_config.is_file():
        try:
            config_doc = tomlkit.parse(course_config.read_text(encoding="utf-8"))
            if _normalize_entries(config_doc):
                _write_doc(course_config, config_doc)
        except (OSError, tomlkit.exceptions.ParseError):
            # ponytail: unreadable/corrupt course config -> leave it alone
            # (the old blind str.replace would have rewritten a corrupt file
            # anyway).
            pass

    for child, new_name, entries in targets:
        child.rename(child.with_name(new_name))
        migrated = child.with_name(new_name)
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
        print(
            f"no actions for {opts.course_dir}" + (" (dry run)" if opts.dry_run else "")
        )
