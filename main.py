from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal

from canvasapi import Canvas
from misc.score_review_tui import ScoreReviewTuiCliOptions, Viewer
from pydantic import AliasChoices, Field, field_validator, model_validator
from src.analysis import analyze_assignment
from src.assignment_config import FetchSection, load_assignment_file
from src.canvas_fetch import (
    fetch_assignment,
    list_assignments,
    list_courses,
    load_env,
    remember_fetch,
)
from src.cli_options import CliOptions, parse_cli_args, validate_existing_file
from src.grading import grade_assignment
from src.plagiarism import detect_plagiarism
from src.processing import preprocess_assignment
from src.schema_tools import generate_all_schemas
from src.scoring import score_assignment

PipelineStage = Literal[
    "preprocess",
    "plagiarism",
    "grade",
    "score",
    "analyze",
    "all",
    "schema",
]


class MainCliOptions(CliOptions):
    stage: PipelineStage = Field(
        default="all",
        validation_alias=AliasChoices("stage", "s"),
        description="Pipeline stage to run.",
    )
    config: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("config", "c"),
        description="Path to assignment config TOML.",
    )
    force: bool = Field(
        default=False,
        validation_alias=AliasChoices("force", "f"),
        description="For grade stage, ignore checkpoint and regrade all submissions.",
    )

    @model_validator(mode="after")
    def _validate_config_requirement(self) -> MainCliOptions:
        if self.stage != "schema" and self.config is None:
            msg = "--config is required for running stages other than 'schema'."
            raise ValueError(msg)
        if self.stage != "schema" and self.config is not None:
            self.config = validate_existing_file(self.config)
        return self


class FetchCliOptions(CliOptions):
    course: int | None = Field(default=None, description="Canvas course ID.")
    assignment: int | None = Field(default=None, description="Canvas assignment ID.")
    out: str | None = Field(default=None, description="Output directory.")
    mode: Literal["attach", "text", "auto"] = Field(
        default="auto",
        description="Submission type (default: auto-detect from Canvas).",
    )
    config: Path | None = Field(
        default=None,
        description="Assignment config.toml holding [fetch] memory; "
        "defaults to ./config.toml when run from an assignment dir.",
    )
    retry: bool = Field(
        default=False,
        description="Re-fetch all assignments recorded in configs "
        "(filter with --course/--assignment).",
    )

    @field_validator("config")
    @classmethod
    def _validate_config(cls, value: Path | None) -> Path | None:
        if value is None:
            return value
        return validate_existing_file(value)

    @model_validator(mode="after")
    def _validate_fetch_args(self) -> FetchCliOptions:
        if self.retry and self.out is not None:
            msg = "--out is ignored with --retry; output dirs come from the configs."
            raise ValueError(msg)
        # The together-check only applies to the non-retry path; in retry mode
        # --course/--assignment act as independent config filters (original behavior).
        if not self.retry and (self.course is None) != (self.assignment is None):
            msg = "--course and --assignment must be given together."
            raise ValueError(msg)
        return self


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


def _load_config(config_arg: str | Path | None) -> tuple[Path | None, FetchSection | None]:
    if config_arg is not None:
        cfg_path = Path(config_arg).resolve()
        return cfg_path, load_assignment_file(cfg_path).fetch
    cwd_config = Path.cwd() / "config.toml"
    if cwd_config.exists():
        return cwd_config, load_assignment_file(cwd_config).fetch
    return None, None


def _resolve_out(out_arg: str | None, cfg: FetchSection | None, cfg_path: Path | None) -> Path:
    out_str = out_arg if out_arg is not None else (cfg.out_dir if cfg is not None else "raw")
    return (cfg_path.parent / out_str).resolve() if cfg_path is not None else Path(out_str).resolve()


def _remember(
    out: Path,
    cfg_path: Path | None,
    course_id: int,
    assignment_id: int,
    mode: str,
) -> None:
    candidates = ([cfg_path] if cfg_path is not None else []) + [
        Path.cwd() / "config.toml",
        out.parent / "config.toml",
    ]
    for cand in candidates:
        if not cand.exists():
            continue
        try:
            load_assignment_file(cand)
        except (ValueError, FileNotFoundError):
            continue
        rel_out = os.path.relpath(out, cand.parent)
        remember_fetch(
            cand,
            course_id=course_id,
            assignment_id=assignment_id,
            out_dir=rel_out,
            mode=mode,
        )
        print(f"[fetch] remembered in {cand}")
        return


def _retry_fetch(course_filter: int | None, assignment_filter: int | None) -> None:
    root = Path(__file__).resolve().parent
    targets = []
    for config_path in sorted(root.glob("assignments/*/config.toml")):
        try:
            cfg = load_assignment_file(config_path).fetch
        except (ValueError, FileNotFoundError):
            continue
        if cfg is None:
            continue
        if course_filter is not None and cfg.course_id != course_filter:
            continue
        if assignment_filter is not None and cfg.assignment_id != assignment_filter:
            continue
        targets.append((config_path, cfg))
    if not targets:
        sys.exit(
            "no assignment configs with a [fetch] section matched; "
            "run `main.py fetch` once to record one"
        )
    base_url, token = load_env()
    canvas = Canvas(base_url, token)
    for config_path, cfg in targets:
        out = cfg.resolve_out_dir(config_path.parent)
        fetch_assignment(canvas, cfg.course_id, cfg.assignment_id, out, cfg.mode)


def _pick_interactive(out_default: Path, mode: str) -> None:
    base_url, token = load_env()
    canvas = Canvas(base_url, token)

    courses = list_courses(canvas)
    if not sys.stdin.isatty():
        _print_options("courses", courses)
        sys.exit("provide --course/--assignment, or run in a terminal to pick interactively")
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

    if args.course is not None or args.assignment is not None:
        course_id: int = args.course
        assignment_id: int = args.assignment
    elif cfg is not None:
        course_id = cfg.course_id
        assignment_id = cfg.assignment_id
    else:
        _pick_interactive(_resolve_out(args.out, None, None), args.mode)
        return

    out = _resolve_out(args.out, cfg, cfg_path)
    mode = args.mode if args.mode != "auto" else (cfg.mode if cfg is not None else "auto")

    base_url, token = load_env()
    canvas = Canvas(base_url, token)
    fetch_assignment(canvas, course_id, assignment_id, out, mode)
    _remember(out, cfg_path, course_id, assignment_id, mode)


# --- view subcommand ---


def _run_view(argv: list[str]) -> None:
    args = parse_cli_args(ScoreReviewTuiCliOptions, argv=argv)
    Viewer(args).run()


def main() -> None:  # ruff: ignore[too-many-branches, too-many-statements]
    argv = sys.argv[1:]
    if argv and argv[0] in {"fetch", "view"}:
        sub = argv.pop(0)
        if sub == "fetch":
            args = parse_cli_args(FetchCliOptions, argv=argv)
            _run_fetch(args)
        else:
            _run_view(argv)
        return

    args = parse_cli_args(MainCliOptions)

    if args.stage == "schema":
        print("Generating schemas...")
        schema_files = generate_all_schemas(Path(__file__).parent)
        for schema_file in schema_files:
            print(f"[schema] {schema_file}")
        return

    assert args.config is not None  # validated by model_validator
    summaries = []

    if args.stage in {"plagiarism", "all"}:
        print("Running plagiarism detection...")
        summary = detect_plagiarism(args.config)
        if summary is not None:
            summaries.append(summary)
            print(_format_job_summary(summary))

    if args.stage in {"preprocess", "all"}:
        print("Running preprocessing...")
        summary = preprocess_assignment(args.config)
        if summary is not None:
            summaries.append(summary)
            print(_format_job_summary(summary))

    if args.stage in {"grade", "all"}:
        print("Running grading...")
        summary = grade_assignment(args.config, force=args.force)
        if summary is not None:
            summaries.append(summary)
            print(_format_job_summary(summary))

    if args.stage in {"score", "all"}:
        print("Running scoring...")
        summary = score_assignment(args.config)
        if summary is not None:
            summaries.append(summary)
            print(_format_job_summary(summary))

    if args.stage in {"analyze", "all"}:
        print("Running meta analysis...")
        summary = analyze_assignment(args.config)
        if summary is not None:
            summaries.append(summary)
            print(_format_job_summary(summary))

    # Print aggregate summary if multiple stages ran
    if args.stage == "all" and len(summaries) > 1:
        total_success = sum(s.get("success", 0) for s in summaries)
        total_errors = sum(s.get("errors", 0) for s in summaries)
        total_items = sum(s.get("total", 0) for s in summaries)
        aggregate_rate = (total_success / total_items * 100) if total_items > 0 else 0
        print(
            f"[summary] {total_success} total success, {total_errors} total error(s), "
            f"{aggregate_rate:.1f}% overall success rate"
        )


if __name__ == "__main__":
    main()
