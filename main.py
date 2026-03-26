from __future__ import annotations

from pathlib import Path
from typing import Literal

from analysis import analyze_assignment
from cli_options import CliOptions, parse_cli_args, validate_existing_file
from grading import grade_assignment
from plagiarism import detect_plagiarism
from processing import preprocess_assignment
from pydantic import Field, model_validator
from schema_tools import generate_all_schemas
from scoring import score_assignment

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
    stage: PipelineStage = Field(default="all", description="Pipeline stage to run.")
    config: Path | None = Field(default=None, description="Path to assignment config TOML.")
    force: bool = Field(
        default=False,
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


def _format_job_summary(summary: dict) -> str:
    """Format a job summary dict as a one-line summary string."""
    if summary is None:
        return ""
    stage = summary.get("stage", "unknown")
    success = summary.get("success", 0)
    errors = summary.get("errors", 0)
    rate = summary.get("success_rate", 0)
    return f"[{stage}] {success} success, {errors} error(s), {rate:.1f}% success rate"


def main() -> None:  # noqa: PLR0912
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
