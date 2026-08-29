# Canvas submission fetch: attachment downloads and text-entry bodies.
# Modes: attach (download attachment files), text (save submission body),
# auto (detect from submissions: attachments present -> attach, else text).
from __future__ import annotations

import csv
import json
import operator
import sys
from pathlib import Path
from typing import Literal

from canvasapi import Canvas

FetchMode = Literal["attach", "text", "auto"]

ROSTER_FIELDS = ["user_id", "user_name", "sortable_name", "file"]


def load_env() -> tuple[str, str]:
    for d in [Path.cwd(), *Path.cwd().parents]:
        env = d / ".env"
        if env.exists():
            vals = {}
            for line in env.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip()
            return vals["CANVAS_BASE_URL"], vals["CANVAS_ACCESS_TOKEN"]
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


def _fetch_attachments(subs: list[object], out: Path) -> list[dict]:
    cache_path = out / ".fetch-cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    rows = []
    for sub in subs:
        uid, name, sortable, late = _submitter(sub)
        atts = getattr(sub, "attachments", None) or []
        for i, att in enumerate(atts):
            ext = att.filename.rsplit(".", 1)[-1]
            suffix = f"_LATE_{i}" if late else (f"_{i}" if i else "")
            dest = out / f"{uid}{suffix}.{ext}"
            stamp = getattr(att, "updated_at", "") or ""
            # Skip re-downloading when the file exists and the attachment is unchanged.
            if dest.exists() and cache.get(dest.name) == stamp:
                pass
            else:
                att.download(dest)
                cache[dest.name] = stamp
            rows.append({
                "user_id": uid,
                "user_name": name,
                "sortable_name": sortable,
                "file": dest.name,
            })
        if not atts:
            rows.append({
                "user_id": uid,
                "user_name": name,
                "sortable_name": sortable,
                "file": "",
            })
    cache_path.write_text(json.dumps(cache))
    return rows


def _fetch_text(subs: list[object], out: Path) -> list[dict]:
    rows = []
    for sub in subs:
        uid, name, sortable, late = _submitter(sub)
        body = getattr(sub, "body", "") or ""
        if body:
            suffix = "_LATE_0" if late else ""
            dest = out / f"{uid}{suffix}.txt"
            dest.write_text(body, encoding="utf-8")
            file = dest.name
        else:
            file = ""
        rows.append({
            "user_id": uid,
            "user_name": name,
            "sortable_name": sortable,
            "file": file,
        })
    return rows


def _resolve_mode(subs: list[object], mode: str) -> str:
    if mode != "auto":
        return mode
    return "attach" if any(getattr(s, "attachments", None) for s in subs) else "text"


def fetch_assignment(
    canvas: Canvas,
    course_id: int,
    assignment_id: int,
    out: Path,
    mode: str = "auto",
) -> list[dict]:
    course = canvas.get_course(course_id)
    assignment = course.get_assignment(assignment_id)
    out.mkdir(parents=True, exist_ok=True)
    subs = list(assignment.get_submissions(include=["user", "attachments"]))
    mode_resolved = _resolve_mode(subs, mode)
    rows = (
        _fetch_attachments(subs, out)
        if mode_resolved == "attach"
        else _fetch_text(subs, out)
    )
    rows.sort(key=operator.itemgetter("sortable_name"))
    roster = out.parent / "roster.csv"
    with roster.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ROSTER_FIELDS)
        w.writeheader()
        w.writerows(rows)
    files = sum(1 for r in rows if r["file"])
    print(f"{mode_resolved}: {files} submissions -> {out}/ ; roster -> {roster}")
    return rows


def _fetch_section_bounds(lines: list[str]) -> tuple[int, int] | None:
    """(start, end) of the top-level [fetch] table: end is the first nested
    table heading (bare keys after a [[...]]/[x] line belong to that table)."""
    starts = [
        i
        for i, ln in enumerate(lines)
        if ln.strip().split("#", 1)[0].strip() == "[fetch]"
    ]
    if not starts:
        return None
    start = starts[0]
    end = start + 1
    while end < len(lines) and not lines[end].lstrip().startswith("["):
        end += 1
    return start, end


def _patch_fetch_section(
    lines: list[str], start: int, end: int, values: dict[str, str]
) -> None:
    """Update the [fetch] keys in place; nested tables ([[fetch.assignments]])
    and unknown keys stay untouched."""
    section = lines[start + 1 : end]
    done: set[str] = set()
    for i, ln in enumerate(section):
        key = ln.partition("=")[0].strip()
        if key in values:
            section[i] = f"{key} = {values[key]}"
            done.add(key)
    for key, val in values.items():
        if key not in done:
            section.append(f"{key} = {val}")
    lines[start + 1 : end] = section


def remember_fetch(
    config_path: Path,
    *,
    course_id: int | None = None,
    assignment_id: int | None = None,
    out_dir: str | None = None,
    mode: str | None = None,
) -> None:
    """Upsert [fetch] memory into a config.toml (field-level, never whole-block).

    Only provided fields are written, and only into the top-level [fetch]
    table; everything else — grading sections, a ``[[fetch.assignments]]``
    list — is preserved. Passing None leaves an existing value untouched; the
    file is created if missing. Course-level state goes to the course config,
    assignment-level state to the assignment config.
    """
    if not config_path.exists():
        text = "# TATA config: add [grading] with rubric/system_prompt/provider.\n"
    else:
        text = config_path.read_text(encoding="utf-8")

    # Defaults (mode=auto, out_dir=raw) stay omitted, as before.
    values: dict[str, str] = {}
    if course_id is not None:
        values["course_id"] = str(course_id)
    if assignment_id is not None:
        values["assignment_id"] = str(assignment_id)
    if mode is not None and mode != "auto":
        values["mode"] = f'"{mode}"'
    if out_dir is not None and out_dir != "raw":
        values["out_dir"] = f'"{out_dir}"'

    bounds = _fetch_section_bounds(text.splitlines())
    if bounds is None:
        block = "[fetch]\n" + "\n".join(f"{k} = {v}" for k, v in values.items()) + "\n"
        text = text.rstrip() + "\n\n" + block
    else:
        lines = text.splitlines()
        _patch_fetch_section(lines, *bounds, values)
        text = "\n".join(lines) + "\n"
    config_path.write_text(text, encoding="utf-8")
