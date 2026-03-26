from __future__ import annotations

from pathlib import Path

from grading import grade_assignment


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="TATA unified grading entrypoint.")
    parser.add_argument(
        "--stage",
        choices=["grade"],
        default="grade",
        help="Pipeline stage to run.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to assignment config TOML.",
    )
    args = parser.parse_args()

    if args.stage == "grade":
        grade_assignment(args.config)


if __name__ == "__main__":
    main()
