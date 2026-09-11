"""Fetch pipeline: the CLI/TUI-shared fetch orchestration layer.

Moved out of ``src.cli.main`` so the TUI can call it without importing
the CLI's module-private fetch functions (cross-package privates).
All functions here behave exactly as in the CLI: ``run_fetch`` drives the
whole ``fetch`` subcommand (retry / root-config list / single fetch /
interactive pick), and the separate helpers are the config-classification
and course-list primitives it is built from.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from canvasapi import Canvas

from src import REPO_ROOT
from src.shared.assignment_config import (
    FetchSection,
    find_root_config,
    load_assignment_file,
    load_root_section,
)
from src.shared.canvas_fetch import (
    fetch_assignment,
    list_assignments,
    list_courses,
    load_env,
    make_canvas_client,
    remember_course_fetch,
)
from src.shared.cli_options import FetchCliOptions


def format_job_summary(summary: dict) -> str:
    """Format a job summary dict as a one-line summary string."""
    if summary is None:
        return ""
    stage = summary.get("stage", "unknown")
    success = summary.get("success", 0)
    errors = summary.get("errors", 0)
    rate = summary.get("success_rate", 0)
    return f"[{stage}] {success} success, {errors} error(s), {rate:.1f}% success rate"


def repo_root() -> Path:
    """Repo root: anchored at ``src/__init__.py``."""
    return REPO_ROOT


def root_fetch(cfg_path: Path) -> FetchSection | None:
    """Fetch state from a course/global config (course-level keys only)."""
    return load_root_section(cfg_path, "fetch", FetchSection)


def is_container(cfg_path: Path | None) -> bool:
    """Container = a config that cannot load as an assignment: it has no
    [grading] (course/global config). Presence of [fetch] is irrelevant —
    a fresh course config before first fetch is still a container."""
    if cfg_path is None or not cfg_path.is_file():
        return False
    try:
        load_assignment_file(cfg_path)
        return False
    except ValueError as err:
        # Bad TOML: surface load_assignment_file's guidance, not a bare
        # TOMLDecodeError from root_fetch.
        try:
            root_fetch(cfg_path)
        except ValueError:
            raise err from None
        return True


def classify_config(cfg_path: Path) -> tuple[Path, FetchSection | None]:
    """Classify one config.toml: a container (self-evidence: cannot load as
    an assignment — no [grading]) yields its course-level [fetch] state; any
    other config is an assignment config."""
    if is_container(cfg_path):
        return cfg_path, root_fetch(cfg_path)
    return cfg_path, load_assignment_file(cfg_path).fetch


def load_config(
    config_arg: str | Path | None,
) -> tuple[Path | None, FetchSection | None]:
    if config_arg is not None:
        return classify_config(Path(config_arg).resolve())
    cwd_config = Path.cwd() / "config.toml"
    if cwd_config.exists():
        return classify_config(cwd_config)
    return None, None


def remember(
    cfg_path: Path | None,
    course_id: int,
    assignment_id: int,
) -> None:
    """Persist fetch state into the course config only: a container config
    (course/global) is its own course config, anything else climbs to the
    course config above it. Without a course config nothing is written —
    the fetch was ad-hoc and a hint points where the [[fetch.assignments]]
    entry would go. Never writes assignment configs; ``[fetch].mode`` no
    longer exists (legacy keys in existing configs are left untouched)."""
    course_cfg = None
    if cfg_path is not None:
        cfg_path = cfg_path.resolve()
        course_cfg = cfg_path if is_container(cfg_path) else find_root_config(cfg_path)
    if course_cfg is not None:
        remember_course_fetch(
            course_cfg,
            course_id=course_id,
            assignment_id=assignment_id,
        )
        print(f"[fetch] remembered in {course_cfg}")
    else:
        print(
            "[fetch] not remembered: no course config found — add "
            "[[fetch.assignments]] to data/<course>/config.toml"
        )


def fetch_entries(  # ruff: ignore[too-many-arguments]
    canvas: Canvas,
    course_id: int,
    cfg_path: Path,
    cfg: FetchSection,
    *,
    assignment_filter: int | None = None,
    seen: set[tuple[int, int]] | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Fetch every [[fetch.assignments]] entry of a root config. With a
    shared ``seen`` set (retry loop) an entry already fetched for its
    (course_id, assignment_id) is skipped — global and course configs may
    both carry the same assignment in a mixed tree.

    A set ``cancel_event`` (TUI jobs) stops before the next entry:
    ``fetch_assignment`` is one Canvas call, so the item boundary is the
    honest cancellation point.
    """
    for entry in cfg.assignments:
        if cancel_event is not None and cancel_event.is_set():
            print("[cancelled] fetch stopped — remaining assignments skipped")
            return
        if assignment_filter is not None and entry.id != assignment_filter:
            continue
        if seen is not None:
            key = (course_id, entry.id)
            if key in seen:
                print(
                    f"[fetch] skip {entry.id} (already fetched by course {course_id})"
                )
                continue
            seen.add(key)
        out = (cfg_path.parent / str(entry.id) / "raw").resolve()
        fetch_assignment(canvas, course_id, entry.id, out)


def fetch_course(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
    canvas: Canvas,
    config_path: Path,
    course_filter: int | None,
    assignment_filter: int | None,
    seen: set[tuple[int, int]] | None = None,
    cancel_event: threading.Event | None = None,
) -> bool:
    """Fetch one course config's [[fetch.assignments]] list; False when it
    does not carry a usable list (or was filtered out by course_filter).
    The gate is semantic only (MAJOR-2): any config holding a [fetch] table
    with a course_id and a [[fetch.assignments]] list is fetchable — the
    structural is_course_config heuristic flips under nested configs (M1)."""
    if not config_path.exists():
        return False
    cfg = root_fetch(config_path)
    if cfg is None or cfg.course_id is None or not cfg.assignments:
        return False
    if course_filter is not None and cfg.course_id != course_filter:
        return False
    fetch_entries(
        canvas,
        cfg.course_id,
        config_path,
        cfg,
        assignment_filter=assignment_filter,
        seen=seen,
        cancel_event=cancel_event,
    )
    return True


def retry_fetch(
    course_filter: int | None,
    assignment_filter: int | None,
    cancel_event: threading.Event | None = None,
) -> None:
    root = repo_root()
    base_url, token = load_env()
    canvas = make_canvas_client(base_url, token)

    # Primary: course-level configs, each with its own [[fetch.assignments]]
    # list. Three-level layout: data/<course>/config.toml; legacy
    # two-level: data/config.toml (the only course-level file).
    root_cfg = root / "data" / "config.toml"
    # Fresh three-level layout has no data/config.toml — only feed
    # fetch_course paths that exist.
    course_configs = [root_cfg] if root_cfg.exists() else []
    course_configs += sorted(root.glob("data/*/config.toml"))
    seen: set[tuple[int, int]] = set()
    fetched_any = False
    for config_path in course_configs:
        if fetch_course(
            canvas,
            config_path,
            course_filter,
            assignment_filter,
            seen,
            cancel_event=cancel_event,
        ):
            fetched_any = True
    if fetched_any:
        # Course-level lists are the source of truth.
        return
    sys.exit(
        "no course configs with a [[fetch.assignments]] list matched; "
        "add [[fetch.assignments]] entries to a course config "
        "(data/<course>/config.toml)"
    )


def pick_interactive() -> None:
    base_url, token = load_env()
    canvas = make_canvas_client(base_url, token)

    courses = list_courses(canvas)
    if not sys.stdin.isatty():
        print_options("courses", courses)
        sys.exit(
            "provide --course/--assignment, or run in a terminal to pick interactively"
        )
    course_id = ask_choice(courses, "course")
    assignments = list_assignments(canvas, course_id)
    assignment_id = ask_choice(assignments, "assignment")
    out = (Path.cwd() / str(assignment_id) / "raw").resolve()
    fetch_assignment(canvas, course_id, assignment_id, out)
    remember(None, course_id, assignment_id)


def ask_choice(items: list[tuple[int, str]], title: str) -> int:
    print_options(title, items)
    num = ask_number(f"Choose {title} [1-{len(items)}]", len(items), 1)
    return items[num - 1][0]


def print_options(title: str, items: list[tuple[int, str]]) -> None:
    print(f"{title}:")
    for i, (item_id, name) in enumerate(items, 1):
        print(f"  {i}. {item_id} — {name}")


def ask_number(prompt: str, count: int, default: int) -> int:
    while True:
        try:
            raw = input(f"{prompt} [{default}] ").strip()
            num = int(raw) if raw else default
        except ValueError:
            print("Not a number, try again.")
            continue
        if 1 <= num <= count:
            return num
        print(f"Enter a number between 1 and {count}.")


def run_fetch(
    args: FetchCliOptions, *, cancel_event: threading.Event | None = None
) -> None:
    """Run the ``fetch`` subcommand (retry / root-config list / single fetch /
    interactive pick). ``cancel_event`` (TUI jobs) is checked at entry-list
    boundaries; a single fetch checks it once before its one Canvas call."""
    if args.retry:
        retry_fetch(args.course, args.assignment, cancel_event=cancel_event)
        return

    cfg_path, cfg = load_config(args.config)

    # Root config list: fetch every [[fetch.assignments]] entry in one shot.
    if (
        args.course is None
        and args.assignment is None
        and cfg is not None
        and cfg.course_id is not None
        and cfg.assignments
    ):
        assert cfg_path is not None  # cfg non-None implies a config was found
        base_url, token = load_env()
        canvas = make_canvas_client(base_url, token)
        fetch_entries(canvas, cfg.course_id, cfg_path, cfg, cancel_event=cancel_event)
        return

    course_id = (
        args.course
        if args.course is not None
        else (cfg.course_id if cfg is not None else None)
    )
    if course_id is None:
        pick_interactive()
        return

    # The out dir for a single fetch: assignment config -> <dir>/raw;
    # course config (or no config) -> <course (cwd)>/<aid>/raw.
    is_container_path = cfg_path is not None and is_container(cfg_path)

    # Assignment id: positional > numeric assignment dir name > interactive.
    # Interactive only with no config or a container (course/global) config —
    # an assignment config with a non-numeric dir name has no id to derive,
    # and reading the terminal hangs under the TUI worker.
    if args.assignment is not None:
        assignment_id = args.assignment
    elif (
        cfg_path is not None
        and not is_container_path
        and cfg_path.parent.name.isdigit()
    ):
        assignment_id = int(cfg_path.parent.name)
    elif cfg_path is None or is_container_path:
        base_url, token = load_env()
        canvas = make_canvas_client(base_url, token)
        assignments = list_assignments(canvas, course_id)
        if not sys.stdin.isatty():
            print_options("assignments", assignments)
            sys.exit("provide --assignment, or run in a terminal to pick interactively")
        assignment_id = ask_choice(assignments, "assignment")
    else:
        sys.exit(
            "assignment dir name is not a numeric id — pass "
            "--course/--assignment (or migrate dirs to assignment ids "
            "with python -m src.shared.aliases migrate <course_dir>)"
        )

    if cfg_path is None:
        out = (Path.cwd() / str(assignment_id) / "raw").resolve()
    elif is_container_path:
        out = (cfg_path.parent / str(assignment_id) / "raw").resolve()
    else:
        out = (cfg_path.parent / "raw").resolve()

    if cancel_event is not None and cancel_event.is_set():
        print("[cancelled] fetch stopped — nothing fetched")
        return

    base_url, token = load_env()
    canvas = make_canvas_client(base_url, token)
    fetch_assignment(canvas, course_id, assignment_id, out)
    remember(cfg_path, course_id, assignment_id)
