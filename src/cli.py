from __future__ import annotations

import sys
from pathlib import Path

from canvasapi import Canvas
from pydantic_settings import CliApp, get_subcommand

from src.analysis import analyze_assignment
from src.assignment_config import (
    FetchSection,
    find_root_config,
    load_assignment_file,
    load_root_section,
)
from src.canvas_fetch import (
    fetch_assignment,
    list_assignments,
    list_courses,
    load_env,
    remember_course_fetch,
)
from src.cli_options import (
    AnalyzeCliOptions,
    ConfigCliOptions,
    ConfigSetCliOptions,
    FetchCliOptions,
    GradeCliOptions,
    PlagiarismCliOptions,
    PreprocessCliOptions,
    SchemaCliOptions,
    ScoreCliOptions,
    ScoreReviewCliOptions,
    TataCli,
    parse_cli_args,
)
from src.config_edit import edit_config, validate_config_edits
from src.grading import grade_assignment
from src.plagiarism import detect_plagiarism
from src.processing import preprocess_assignment
from src.schema_tools import generate_all_schemas
from src.score_review import run as run_score_viewer
from src.scoring import score_assignment

# Stage subcommands: type -> (label, pipeline function).
_STAGES = {
    PreprocessCliOptions: ("preprocessing", preprocess_assignment),
    PlagiarismCliOptions: ("plagiarism detection", detect_plagiarism),
    GradeCliOptions: ("grading", grade_assignment),
    ScoreCliOptions: ("scoring", score_assignment),
    AnalyzeCliOptions: ("meta analysis", analyze_assignment),
}


def _format_job_summary(summary: dict) -> str:
    """Format a job summary dict as a one-line summary string."""
    if summary is None:
        return ""
    stage = summary.get("stage", "unknown")
    success = summary.get("success", 0)
    errors = summary.get("errors", 0)
    rate = summary.get("success_rate", 0)
    return f"[{stage}] {success} success, {errors} error(s), {rate:.1f}% success rate"


# --- fetch subcommand ---


def _repo_root() -> Path:
    """Repo root: this module lives in ``src/``, so one level up."""
    return Path(__file__).resolve().parent.parent


def _root_fetch(cfg_path: Path) -> FetchSection | None:
    """Fetch state from a course/global config (course-level keys only)."""
    return load_root_section(cfg_path, "fetch", FetchSection)


def _is_container(cfg_path: Path | None) -> bool:
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
        # TOMLDecodeError from _root_fetch.
        try:
            _root_fetch(cfg_path)
        except ValueError:
            raise err from None
        return True


def _classify_config(cfg_path: Path) -> tuple[Path, FetchSection | None]:
    """Classify one config.toml: a container (self-evidence: cannot load as
    an assignment — no [grading]) yields its course-level [fetch] state; any
    other config is an assignment config."""
    if _is_container(cfg_path):
        return cfg_path, _root_fetch(cfg_path)
    return cfg_path, load_assignment_file(cfg_path).fetch


def _load_config(
    config_arg: str | Path | None,
) -> tuple[Path | None, FetchSection | None]:
    if config_arg is not None:
        return _classify_config(Path(config_arg).resolve())
    cwd_config = Path.cwd() / "config.toml"
    if cwd_config.exists():
        return _classify_config(cwd_config)
    return None, None


def _entry_mode(
    cfg_path: Path | None, cfg: FetchSection | None, aid: int
) -> str | None:
    """Per-entry mode from the course config's [[fetch.assignments]] list.

    ``cfg`` carries the list when the config is a container; when it is an
    assignment config the list was stripped by ``load_assignment_file``, so
    climb to the course config above it. None when the entry (or any course
    config) is absent — callers fall back to the course mode.
    """
    entries = cfg.assignments if cfg is not None else []
    if not entries and cfg_path is not None and not _is_container(cfg_path):
        course_cfg = find_root_config(cfg_path)
        if course_cfg is not None:
            course_fetch = load_root_section(course_cfg, "fetch", FetchSection)
            entries = course_fetch.assignments if course_fetch is not None else []
    for entry in entries:
        if entry.id == aid:
            return entry.mode
    return None


def _remember(
    cfg_path: Path | None,
    course_id: int,
    assignment_id: int,
    mode: str,
) -> None:
    """Persist fetch state into the course config only: a container config
    (course/global) is its own course config, anything else climbs to the
    course config above it. Without a course config nothing is written —
    the fetch was ad-hoc and a hint points where the [[fetch.assignments]]
    entry would go. Never writes assignment configs or [fetch].mode."""
    course_cfg = None
    if cfg_path is not None:
        cfg_path = cfg_path.resolve()
        course_cfg = cfg_path if _is_container(cfg_path) else find_root_config(cfg_path)
    if course_cfg is not None:
        # Don't bake the course-default mode into the entry: an entry whose
        # mode equals the course config's [fetch].mode records no mode, so
        # later fetches keep following the course default (mode != course
        # mode or no course config -> record per the old rule).
        course_fetch = load_root_section(course_cfg, "fetch", FetchSection)
        entry_mode = mode
        if course_fetch is not None and course_fetch.mode == mode:
            entry_mode = None
        remember_course_fetch(
            course_cfg,
            course_id=course_id,
            entry=(assignment_id, entry_mode),
        )
        print(f"[fetch] remembered in {course_cfg}")
    else:
        print(
            "[fetch] not remembered: no course config found — add "
            "[[fetch.assignments]] to data/<course>/config.toml"
        )


def _fetch_entries(  # ruff: ignore[too-many-arguments]
    canvas: Canvas,
    course_id: int,
    cfg_path: Path,
    cfg: FetchSection,
    *,
    assignment_filter: int | None = None,
    seen: set[tuple[int, int]] | None = None,
) -> None:
    """Fetch every [[fetch.assignments]] entry of a root config. With a
    shared ``seen`` set (retry loop) an entry already fetched for its
    (course_id, assignment_id) is skipped — global and course configs may
    both carry the same assignment in a mixed tree."""
    for entry in cfg.assignments:
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
        fetch_assignment(
            canvas,
            course_id,
            entry.id,
            out,
            entry.mode or cfg.mode,
        )


def _fetch_course(
    canvas: Canvas,
    config_path: Path,
    course_filter: int | None,
    assignment_filter: int | None,
    seen: set[tuple[int, int]] | None = None,
) -> bool:
    """Fetch one course config's [[fetch.assignments]] list; False when it
    does not carry a usable list (or was filtered out by course_filter).
    The gate is semantic only (MAJOR-2): any config holding a [fetch] table
    with a course_id and a [[fetch.assignments]] list is fetchable — the
    structural is_course_config heuristic flips under nested configs (M1)."""
    if not config_path.exists():
        return False
    cfg = _root_fetch(config_path)
    if cfg is None or cfg.course_id is None or not cfg.assignments:
        return False
    if course_filter is not None and cfg.course_id != course_filter:
        return False
    _fetch_entries(
        canvas,
        cfg.course_id,
        config_path,
        cfg,
        assignment_filter=assignment_filter,
        seen=seen,
    )
    return True


def _retry_fetch(course_filter: int | None, assignment_filter: int | None) -> None:
    root = _repo_root()
    base_url, token = load_env()
    canvas = Canvas(base_url, token)

    # Primary: course-level configs, each with its own [[fetch.assignments]]
    # list. Three-level layout: data/<course>/config.toml; legacy
    # two-level: data/config.toml (the only course-level file).
    root_cfg = root / "data" / "config.toml"
    # Fresh three-level layout has no data/config.toml — only feed
    # _fetch_course paths that exist.
    course_configs = [root_cfg] if root_cfg.exists() else []
    course_configs += sorted(root.glob("data/*/config.toml"))
    seen: set[tuple[int, int]] = set()
    fetched_any = False
    for config_path in course_configs:
        if _fetch_course(canvas, config_path, course_filter, assignment_filter, seen):
            fetched_any = True
    if fetched_any:
        # Course-level lists are the source of truth.
        return
    sys.exit(
        "no course configs with a [[fetch.assignments]] list matched; "
        "add [[fetch.assignments]] entries to a course config "
        "(data/<course>/config.toml)"
    )


def _pick_interactive(mode: str) -> None:
    base_url, token = load_env()
    canvas = Canvas(base_url, token)

    courses = list_courses(canvas)
    if not sys.stdin.isatty():
        _print_options("courses", courses)
        sys.exit(
            "provide --course/--assignment, or run in a terminal to pick interactively"
        )
    course_id = _ask_choice(courses, "course")
    assignments = list_assignments(canvas, course_id)
    assignment_id = _ask_choice(assignments, "assignment")
    out = (Path.cwd() / str(assignment_id) / "raw").resolve()
    fetch_assignment(canvas, course_id, assignment_id, out, mode)
    _remember(None, course_id, assignment_id, mode)


def _ask_choice(items: list[tuple[int, str]], title: str) -> int:
    _print_options(title, items)
    num = _ask_number(f"Choose {title} [1-{len(items)}]", len(items), 1)
    return items[num - 1][0]


def _print_options(title: str, items: list[tuple[int, str]]) -> None:
    print(f"{title}:")
    for i, (item_id, name) in enumerate(items, 1):
        print(f"  {i}. {item_id} — {name}")


def _ask_number(prompt: str, count: int, default: int) -> int:
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


def _run_fetch(args: FetchCliOptions) -> None:  # ruff: ignore[PLR0912]
    if args.retry:
        _retry_fetch(args.course, args.assignment)
        return

    cfg_path, cfg = _load_config(args.config)

    # Root config list: fetch every [[fetch.assignments]] entry in one shot.
    if (
        args.course is None
        and args.assignment is None
        and cfg is not None
        and cfg.course_id is not None
        and cfg.assignments
    ):
        if args.mode != "auto":
            print(
                "warning: --mode is ignored with a root "
                "[[fetch.assignments]] list; each entry defines its own",
                file=sys.stderr,
            )
        assert cfg_path is not None  # cfg non-None implies a config was found
        base_url, token = load_env()
        canvas = Canvas(base_url, token)
        _fetch_entries(canvas, cfg.course_id, cfg_path, cfg)
        return

    course_id = (
        args.course
        if args.course is not None
        else (cfg.course_id if cfg is not None else None)
    )
    if course_id is None:
        _pick_interactive(args.mode)
        return

    # The out dir for a single fetch: assignment config -> <dir>/raw;
    # course config (or no config) -> <course (cwd)>/<aid>/raw.
    is_container = cfg_path is not None and _is_container(cfg_path)

    # Assignment id: positional > numeric assignment dir name > interactive.
    # Interactive only with no config or a container (course/global) config —
    # an assignment config with a non-numeric dir name has no id to derive,
    # and reading the terminal hangs under the TUI worker.
    if args.assignment is not None:
        assignment_id = args.assignment
    elif cfg_path is not None and not is_container and cfg_path.parent.name.isdigit():
        assignment_id = int(cfg_path.parent.name)
    elif cfg_path is None or is_container:
        base_url, token = load_env()
        canvas = Canvas(base_url, token)
        assignments = list_assignments(canvas, course_id)
        if not sys.stdin.isatty():
            _print_options("assignments", assignments)
            sys.exit("provide --assignment, or run in a terminal to pick interactively")
        assignment_id = _ask_choice(assignments, "assignment")
    else:
        sys.exit(
            "assignment dir name is not a numeric id — pass "
            "--course/--assignment (or migrate dirs to assignment ids "
            "with python -m src.aliases migrate <course_dir>)"
        )

    if cfg_path is None:
        out = (Path.cwd() / str(assignment_id) / "raw").resolve()
    elif is_container:
        out = (cfg_path.parent / str(assignment_id) / "raw").resolve()
    else:
        out = (cfg_path.parent / "raw").resolve()
    mode = args.mode
    if mode == "auto":
        mode = _entry_mode(cfg_path, cfg, assignment_id) or (
            cfg.mode if cfg is not None else "auto"
        )

    base_url, token = load_env()
    canvas = Canvas(base_url, token)
    fetch_assignment(canvas, course_id, assignment_id, out, mode)
    _remember(cfg_path, course_id, assignment_id, mode)


# --- config set subcommand ---


def _coerce_config_value(raw: str) -> object:
    """TOML-style coercion for ``config set`` values (tomlkit serializes)."""
    if raw == "true":
        return True
    if raw == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _run_config_set(args: ConfigSetCliOptions) -> None:
    """Edit one dotted ``section.key`` in a config.toml (validated, then write)."""
    if args.key.count(".") != 1 or not all(args.key.split(".", 1)):
        sys.exit(f"error: key must be section.key (exactly one dot): {args.key!r}")
    section, key = args.key.split(".", 1)
    edits = {section: {key: _coerce_config_value(args.value)}}
    try:
        validate_config_edits(args.config, edits)
    except ValueError as exc:
        sys.exit(f"error: {exc}")
    edit_config(args.config, edits)
    print(f"[config] wrote {section}.{key} in {args.config}")


def main() -> None:
    cmd = parse_cli_args(TataCli)
    sub = get_subcommand(cmd, is_required=False)
    if sub is None:
        print(CliApp.format_help(TataCli), file=sys.stderr)
        raise SystemExit(2)

    if isinstance(sub, SchemaCliOptions):
        print("Generating schemas...")
        schema_files = generate_all_schemas(_repo_root())
        for schema_file in schema_files:
            print(f"[schema] {schema_file}")
        return

    if isinstance(sub, FetchCliOptions):
        _run_fetch(sub)
        return

    if isinstance(sub, ConfigCliOptions):
        if sub.set is None:
            sys.exit(
                "error: config requires a subcommand: config set -c PATH section.key VALUE"
            )
        _run_config_set(sub.set)
        return

    if isinstance(sub, ScoreReviewCliOptions):
        run_score_viewer(sub)
        return

    label, fn = _STAGES[type(sub)]
    print(f"Running {label}...")
    kwargs = {}
    if isinstance(sub, GradeCliOptions):
        kwargs = {"force": sub.force}
    elif isinstance(sub, PlagiarismCliOptions):
        kwargs = {"aggregate": sub.aggregate, "output": sub.output}
    summary = fn(sub.config, **kwargs)
    if summary is not None:
        print(_format_job_summary(summary))


if __name__ == "__main__":
    main()
