from __future__ import annotations

from pathlib import Path

from analysis import analyze_assignment
from grading import grade_assignment
from processing import preprocess_assignment
from schema_tools import generate_all_schemas
from scoring import score_assignment


def main() -> None:
    import argparse

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
    args = parser.parse_args()

    if args.stage == "schema":
        print("Generating schemas...")
        schema_files = generate_all_schemas(Path(__file__).parent)
        for schema_file in schema_files:
            print(f"[schema] {schema_file}")
        return

    if args.config is None:
        parser.error("--config is required for preprocess/grade/score/all stages")

    if args.stage in ("preprocess", "all"):
        print("Running preprocessing...")
        preprocess_assignment(args.config)

    if args.stage in ("grade", "all"):
        print("Running grading...")
        grade_assignment(args.config)

    if args.stage in ("score", "all"):
        print("Running scoring...")
        score_assignment(args.config)

    if args.stage == "analyze":
        print("Running meta analysis...")
        analyze_assignment(args.config)


if __name__ == "__main__":
    main()
