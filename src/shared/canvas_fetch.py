# Canvas submission fetch: attachment downloads and text-entry bodies.
# Everything is collected per submission: non-empty bodies as <uid>.html and
# every attachment as <uid>{_LATE_i|_i}.{ext}. One file -> flat out/<name>;
# multi-file -> out/<uid>/<name>.
from __future__ import annotations

import json
import operator
import re
import shutil
import sys
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import dotenv
import tomlkit
from canvasapi import Canvas

from src.shared.aliases import upsert_student_aliases


def load_env() -> tuple[str, str]:
    for d in [Path.cwd(), *Path.cwd().parents]:
        env = d / ".env"
        if env.exists():
            vals = dotenv.dotenv_values(env, interpolate=False)
            url, token = vals["CANVAS_BASE_URL"], vals["CANVAS_ACCESS_TOKEN"]
            return url or "", token or ""
    sys.exit("No .env with CANVAS_BASE_URL/CANVAS_ACCESS_TOKEN found")


def list_courses(canvas: Canvas) -> list[tuple[int, str]]:
    # Without enrollment_state the API returns slim objects (id only).
    return [(c.id, c.name) for c in canvas.get_courses(enrollment_state="active")]


def list_assignments(canvas: Canvas, course_id: int) -> list[tuple[int, str]]:
    course = canvas.get_course(course_id)
    return [(a.id, a.name) for a in course.get_assignments()]


def _submitter(sub: object) -> tuple[int, str, str, bool]:
    uid = sub.user_id
    user = getattr(sub, "user", None) or {}
    name = user.get("name") or "?"
    sortable = user.get("sortable_name") or name
    return uid, name, sortable, bool(getattr(sub, "late", False))


def fetch_assignment(  # ruff: ignore[too-many-locals, too-many-branches, too-many-statements]
    canvas: Canvas,
    course_id: int,
    assignment_id: int,
    out: Path,
) -> list[dict]:
    """Fetch every submission of an assignment: non-empty text bodies as
    ``<uid>{_LATE_0}.html`` plus each attachment as
    ``<uid>{_LATE_i|_i}.{ext}``. A submission with a single file lands flat
    in ``out/``; with more than one file it lands in ``out/<uid>/``. The
    plain filename is the ``.fetch-cache.json`` key either way."""
    course = canvas.get_course(course_id)
    assignment = course.get_assignment(assignment_id)
    out.mkdir(parents=True, exist_ok=True)
    cache_path = out / ".fetch-cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    subs = list(assignment.get_submissions(include=["user", "attachments"]))
    rows = []
    flat_names: set[str] = set()
    folder_names: set[str] = set()
    flat_uids: set[int] = set()
    folder_uids: set[int] = set()
    for sub in subs:
        uid, name, sortable, late = _submitter(sub)
        entries: list[tuple[str, str, Any]] = []  # (filename, stamp, att|None)
        body = getattr(sub, "body", "") or ""
        if body:
            suffix = "_LATE_0" if late else ""
            entries.append((
                f"{uid}{suffix}.html",
                getattr(sub, "updated_at", "") or "",
                None,
            ))
        for i, att in enumerate(getattr(sub, "attachments", None) or []):
            ext = att.filename.rsplit(".", 1)[-1]
            if body and i == 0:
                suffix = "_0"  # avoid clashing with the body html
            elif late:
                suffix = f"_LATE_{i}"
            else:
                suffix = f"_{i}" if i else ""
            entries.append((
                f"{uid}{suffix}.{ext}",
                getattr(att, "updated_at", "") or "",
                att,
            ))
        if not entries:
            rows.append({
                "user_id": uid,
                "user_name": name,
                "sortable_name": sortable,
                "file": "",
            })
            continue
        layout = (out / str(uid)) if len(entries) > 1 else out
        layout.mkdir(parents=True, exist_ok=True)
        if layout == out:
            flat_uids.add(uid)
        else:
            folder_uids.add(uid)
        (flat_names if layout == out else folder_names).update(
            fname for fname, _, _ in entries
        )
        for fname, stamp, att in entries:
            dest = layout / fname
            # Skip re-downloading when the file exists and the item is unchanged.
            if dest.exists() and cache.get(fname) == stamp:
                pass
            elif att is None:
                dest.write_text(body, encoding="utf-8")
                cache[fname] = stamp
            else:
                att.download(dest)
                cache[fname] = stamp
        rows.append({
            "user_id": uid,
            "user_name": name,
            "sortable_name": sortable,
            "file": entries[0][0],
        })
    # Prune stale layout leftovers after the download loop (cache stamps
    # are final). A flat file not produced flat this run is an obsolete
    # copy: either the file moved into a per-student folder — keep the
    # re-stamped plain-name key, the folder copy reuses it — or it is a
    # truly deleted name — unlink it and drop its cache entry. Never drop
    # a cache key for a name that IS part of this run's output.
    produced = flat_names | folder_names
    for p in out.iterdir():
        if not p.is_file() or p.name.startswith(".") or p.name in flat_names:
            continue
        p.unlink()
        if p.name not in produced:
            cache.pop(p.name, None)
    # Full folder cleanup. A top-level dir NOT produced this run is stale
    # (2->0 unsubmit, folder->flat shrink): rmtree it and pop cache keys
    # for its files (keys of names produced flat this run are re-used and
    # survive). A produced dir keeps only the files produced this run
    # (folder->folder rename: stale in-folder members are unlinked). A uid
    # appearing in both layouts this run keeps its folder — a dir written
    # this run is never rmtree'd (the folder wins).
    produced_folders = {out / str(uid) for uid in folder_uids}
    for d in out.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        files = [p for p in d.iterdir() if p.is_file()]
        if d not in produced_folders:
            for p in files:
                if p.name not in produced:
                    cache.pop(p.name, None)
            shutil.rmtree(d)
        else:
            for p in files:
                if p.name not in produced:
                    p.unlink()
                    cache.pop(p.name, None)
    # Same uid in both layouts this run (duplicated submission entries):
    # the folder is canonical — drop the flat copies (preprocess's
    # mixed-layout guard skips them anyway) and their cache keys unless
    # the folder carries the same name.
    for uid in flat_uids & folder_uids:
        for p in out.iterdir():
            if not p.is_file() or p.name.startswith("."):
                continue
            if re.sub(r"_(?:LATE_)?\d+$", "", p.stem) == str(uid):
                p.unlink()
                if p.name not in folder_names:
                    cache.pop(p.name, None)
    cache_path.write_text(json.dumps(cache))
    rows.sort(key=operator.itemgetter("sortable_name"))
    aliases = {
        r["user_id"]: (
            r["sortable_name"]
            if r["sortable_name"] not in {"", "?"}
            else r["user_name"]
            if r["user_name"] not in {"", "?"}
            else r["user_id"]
        )
        for r in rows
    }
    upsert_student_aliases(out.parent, aliases)
    alias_path = out.parent / "alias.toml"
    files = sum(1 for r in rows if r["file"])
    print(f"auto: {files} submissions -> {out}/ ; alias -> {alias_path}")
    return rows


def remember_course_fetch(
    config_path: Path,
    *,
    course_id: int | None = None,
    assignment_id: int | None = None,
) -> None:
    """Upsert [fetch] memory into a course config.toml (field-level, never
    whole-block).

    Only [fetch].course_id is written when given; existing keys/tables —
    grading sections, a ``[[fetch.assignments]]`` list — are preserved.
    ``[fetch].mode`` no longer exists: legacy ``mode`` keys in existing
    configs are tolerated but never written. ``assignment_id`` appends
    ``{id = aid}`` to ``[[fetch.assignments]]`` (AOT), deduped by id (a
    legacy ``assignment_id`` key counts as the same id): an entry already
    present leaves the list untouched. The file is created if missing.
    """
    if config_path.exists():
        try:
            doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
        except (OSError, tomlkit.exceptions.ParseError):
            doc = tomlkit.parse("")
    else:
        doc = tomlkit.parse(
            "# TATA config: add [grading] with rubric/system_prompt/provider.\n"
        )

    fetch = doc.get("fetch")
    if not isinstance(fetch, MutableMapping):
        doc["fetch"] = {}
        fetch = doc["fetch"]
    if course_id is not None:
        fetch["course_id"] = course_id
    if assignment_id is not None:
        aot = fetch.get("assignments")
        if not isinstance(aot, list):
            aot = tomlkit.aot()
            fetch["assignments"] = aot
        existing_ids = {
            e.get("id") or e.get("assignment_id")
            if isinstance(e, MutableMapping)
            else None
            for e in aot
        }
        if assignment_id not in existing_ids:
            item = tomlkit.table()
            item["id"] = assignment_id
            aot.append(item)
    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
