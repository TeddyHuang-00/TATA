from __future__ import annotations

from pathlib import Path

from grading import grade_assignment
from processing import preprocess_assignment


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="TATA unified grading entrypoint.")
    parser.add_argument(
        "--stage",
        choices=["preprocess", "grade", "all"],
        default="all",
        help="Pipeline stage to run.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to assignment config TOML.",
    )
    args = parser.parse_args()

    if args.stage in ("preprocess", "all"):
        print("Running preprocessing...")
        preprocess_assignment(args.config)

    if args.stage in ("grade", "all"):
        print("Running grading...")
        grade_assignment(args.config)


if __name__ == "__main__":
    main()
