from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

from canvasapi import Canvas
from misc.score_review_tui import Viewer
from pydantic_settings import CliApp, get_subcommand
from src.analysis import analyze_assignment
from src.assignment_config import (
    FetchSection,
    is_root_config,
    load_assignment_file,
)
from src.canvas_fetch import (
    fetch_assignment,
    list_assignments,
    list_courses,
    load_env,
    remember_fetch,
)
from src.cli_options import (
    AnalyzeCliOptions,
    FetchCliOptions,
    GradeCliOptions,
    PlagiarismCliOptions,
    PreprocessCliOptions,
    SchemaCliOptions,
    ScoreCliOptions,
    ScoreReviewTuiCliOptions,
    TataCli,
    parse_cli_args,
)
from src.grading import grade_assignment
from src.plagiarism import detect_plagiarism
from src.processing import preprocess_assignment
from src.schema_tools import generate_all_schemas
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


def _root_fetch(cfg_path: Path) -> FetchSection | None:
    """Fetch state from a root config (course-level keys only)."""
    toml = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    root_fetch = toml.get("fetch")
    return FetchSection.model_validate(root_fetch) if root_fetch else None


def _load_config(
    config_arg: str | Path | None,
) -> tuple[Path | None, FetchSection | None]:
    if config_arg is not None:
        cfg_path = Path(config_arg).resolve()
        if is_root_config(cfg_path):
            # Root config has no [grading]; only its [fetch] state applies.
            return cfg_path, _root_fetch(cfg_path)
        return cfg_path, load_assignment_file(cfg_path).fetch
    cwd_config = Path.cwd() / "config.toml"
    if cwd_config.exists():
        if is_root_config(cwd_config):
            return cwd_config, _root_fetch(cwd_config)
        return cwd_config, load_assignment_file(cwd_config).fetch
    return None, None


def _resolve_out(
    out_arg: str | None, cfg: FetchSection | None, cfg_path: Path | None
) -> Path:
    out_str = (
        out_arg if out_arg is not None else (cfg.out_dir if cfg is not None else "raw")
    )
    return (
        (cfg_path.parent / out_str).resolve()
        if cfg_path is not None
        else Path(out_str).resolve()
    )


def _remember(
    out: Path,
    cfg_path: Path | None,
    course_id: int,
    assignment_id: int,
    mode: str,
) -> None:
    """Persist fetch state: course-level keys in assignments/config.toml,
    assignment-level keys in the assignment config.toml."""
    assignment_cfg = cfg_path if cfg_path is not None else out.parent / "config.toml"
    root_cfg = assignment_cfg.parent.parent / "config.toml"
    is_assignments_tree = assignment_cfg.parent.parent.name == "assignments"

    if root_cfg.exists() or is_assignments_tree:
        if not root_cfg.exists():
            root_cfg.write_text(
                "# TATA course-level config (gitignored): merged into every\n"
                "# assignment config under this directory.\n",
                encoding="utf-8",
            )
            print(f"[fetch] created {root_cfg}")
        remember_fetch(root_cfg, course_id=course_id, mode=mode)
        remember_fetch(
            assignment_cfg,
            assignment_id=assignment_id,
            out_dir=os.path.relpath(out, assignment_cfg.parent),
        )
        print(f"[fetch] remembered in {root_cfg} and {assignment_cfg}")
    else:
        remember_fetch(
            assignment_cfg,
            course_id=course_id,
            assignment_id=assignment_id,
            out_dir=os.path.relpath(out, assignment_cfg.parent),
            mode=mode,
        )
        print(f"[fetch] remembered in {assignment_cfg}")


def _fetch_entries(
    canvas: Canvas,
    course_id: int,
    cfg_path: Path,
    cfg: FetchSection,
    *,
    assignment_filter: int | None = None,
) -> None:
    """Fetch every [[fetch.assignments]] entry of a root config."""
    for entry in cfg.assignments:
        if assignment_filter is not None and entry.assignment_id != assignment_filter:
            continue
        out = (cfg_path.parent / entry.out).resolve()
        fetch_assignment(
            canvas,
            course_id,
            entry.assignment_id,
            out,
            entry.mode or cfg.mode,
        )


def _retry_fetch(course_filter: int | None, assignment_filter: int | None) -> None:
    root = Path(__file__).resolve().parent
    root_cfg = root / "assignments" / "config.toml"
    base_url, token = load_env()
    canvas = Canvas(base_url, token)

    # Root config list is the source of truth when present.
    if root_cfg.exists():
        root_fetch = _root_fetch(root_cfg)
        if (
            root_fetch is not None
            and root_fetch.course_id is not None
            and root_fetch.assignments
        ):
            if course_filter is not None and root_fetch.course_id != course_filter:
                sys.exit(f"no [[fetch.assignments]] entries for course {course_filter}")
            _fetch_entries(
                canvas,
                root_fetch.course_id,
                root_cfg,
                root_fetch,
                assignment_filter=assignment_filter,
            )
            return

    # Fallback: per-assignment configs recorded before the root list existed.
    targets = []
    for config_path in sorted(root.glob("assignments/*/config.toml")):
        try:
            cfg = load_assignment_file(config_path).fetch
        except (ValueError, FileNotFoundError):
            continue
        if cfg is None or cfg.course_id is None or cfg.assignment_id is None:
            continue
        if course_filter is not None and cfg.course_id != course_filter:
            continue
        if assignment_filter is not None and cfg.assignment_id != assignment_filter:
            continue
        targets.append((config_path, cfg))
    if not targets:
        sys.exit(
            "no assignment configs with a [fetch] section matched; "
            "add [[fetch.assignments]] entries to assignments/config.toml"
        )
    for config_path, cfg in targets:
        out = cfg.resolve_out_dir(config_path.parent)
        fetch_assignment(canvas, cfg.course_id, cfg.assignment_id, out, cfg.mode)


def _pick_interactive(out_default: Path, mode: str) -> None:
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
    out = Path(input(f"Output dir [{out_default}]: ") or out_default).resolve()
    fetch_assignment(canvas, course_id, assignment_id, out, mode)
    _remember(out, None, course_id, assignment_id, mode)


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


def _run_fetch(args: FetchCliOptions) -> None:
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
        if args.out is not None or args.mode != "auto":
            print(
                "warning: --out/--mode are ignored with a root "
                "[[fetch.assignments]] list; each entry defines its own",
                file=sys.stderr,
            )
        assert cfg_path is not None  # cfg non-None implies a config was found
        base_url, token = load_env()
        canvas = Canvas(base_url, token)
        _fetch_entries(canvas, cfg.course_id, cfg_path, cfg)
        return

    if args.course is not None or args.assignment is not None:
        course_id: int = args.course
        assignment_id: int = args.assignment
    elif cfg is not None and cfg.course_id is not None and cfg.assignment_id is not None:
        course_id = cfg.course_id
        assignment_id = cfg.assignment_id
    elif cfg is not None and cfg.course_id is not None:
        # Course known from the root config; pick the assignment interactively.
        base_url, token = load_env()
        canvas = Canvas(base_url, token)
        assignments = list_assignments(canvas, cfg.course_id)
        if not sys.stdin.isatty():
            _print_options("assignments", assignments)
            sys.exit("provide --assignment, or run in a terminal to pick interactively")
        course_id = cfg.course_id
        assignment_id = _ask_choice(assignments, "assignment")
        assert cfg_path is not None  # cfg non-None implies a config was found
        if args.out is None:
            out = (cfg_path.parent / str(assignment_id) / "raw").resolve()
        else:
            out = Path(args.out).resolve()
    else:
        _pick_interactive(_resolve_out(args.out, None, None), args.mode)
        return

    out = _resolve_out(args.out, cfg, cfg_path)
    mode = (
        args.mode if args.mode != "auto" else (cfg.mode if cfg is not None else "auto")
    )

    base_url, token = load_env()
    canvas = Canvas(base_url, token)
    fetch_assignment(canvas, course_id, assignment_id, out, mode)
    _remember(out, cfg_path, course_id, assignment_id, mode)


def main() -> None:
    cmd = parse_cli_args(TataCli)
    sub = get_subcommand(cmd, is_required=False)
    if sub is None:
        print(CliApp.format_help(TataCli), file=sys.stderr)
        raise SystemExit(2)

    if isinstance(sub, SchemaCliOptions):
        print("Generating schemas...")
        schema_files = generate_all_schemas(Path(__file__).parent)
        for schema_file in schema_files:
            print(f"[schema] {schema_file}")
        return

    if isinstance(sub, FetchCliOptions):
        _run_fetch(sub)
        return

    if isinstance(sub, ScoreReviewTuiCliOptions):
        Viewer(sub).run()
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
