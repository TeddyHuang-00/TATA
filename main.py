from __future__ import annotations

import argparse
from pathlib import Path

from analysis import analyze_assignment
from grading import grade_assignment
from processing import preprocess_assignment
from schema_tools import generate_all_schemas
from scoring import score_assignment


def _format_job_summary(summary: dict) -> str:
    """Format a job summary dict as a one-line summary string."""
    if summary is None:
        return ""
    stage = summary.get("stage", "unknown")
    success = summary.get("success", 0)
    errors = summary.get("errors", 0)
    rate = summary.get("success_rate", 0)
    return f"[{stage}] {success} success, {errors} error(s), {rate:.1f}% success rate"


def main() -> None:
    parser = argparse.ArgumentParser(description="TATA unified grading entrypoint.")
    parser.add_argument(
        "--stage",
        choices=["preprocess", "grade", "score", "analyze", "all", "schema"],
        default="all",
        help="Pipeline stage to run.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=False,
        help="Path to assignment config TOML.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="For grade stage, ignore checkpoint and regrade all submissions.",
    )
    args = parser.parse_args()

    if args.stage == "schema":
        print("Generating schemas...")
        schema_files = generate_all_schemas(Path(__file__).parent)
        for schema_file in schema_files:
            print(f"[schema] {schema_file}")
        return

    if args.config is None:
        parser.error("--config is required for preprocess/grade/score/all stages")

    summaries = []

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

    if args.stage == "analyze":
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
