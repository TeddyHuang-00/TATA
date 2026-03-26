from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rubric import get_rubric_definition


def _calculate_criterion_score(
    criterion_pts: int | float,
    rating: str,
    grading_scheme: str | None,
    custom_scale: list[float] | None = None,
) -> float:
    """Calculate score for a single criterion based on rating and grading scheme."""
    if grading_scheme == "custom":
        if custom_scale is None:
            msg = f"Custom grading scheme requires custom_scale, but none provided"
            raise ValueError(msg)

        # Map rating to index in custom scale
        rating_map = {
            "binary": {"correct": 1, "incorrect": 0},
            "ternary": {"correct": 2, "partial": 1, "incorrect": 0},
            "likert": {
                "completely incorrect": 0,
                "somewhat incorrect": 1,
                "neutral": 2,
                "somewhat correct": 3,
                "completely correct": 4,
            },
        }

        if rating.lower() not in rating_map.get(grading_scheme, {}):
            # Fallback for custom scale - assume order matches scale length
            rating_options = [
                "incorrect",
                "partial",
                "correct",
                "somewhat correct",
                "completely correct",
            ]
            try:
                rating_index = rating_options.index(rating.lower())
                if rating_index < len(custom_scale):
                    return custom_scale[rating_index]
                else:
                    return custom_scale[-1]  # Use last value if out of range
            except ValueError:
                return custom_scale[-1]  # Default to last value

        rating_index = rating_map[grading_scheme][rating.lower()]
        if rating_index < len(custom_scale):
            return custom_scale[rating_index]
        else:
            return custom_scale[-1]

    # Standard grading schemes
    if grading_scheme in ("standard", None):
        if rating.lower() == "correct":
            return float(criterion_pts)
        elif rating.lower() == "partial":
            return float(criterion_pts) * 0.5
        elif rating.lower() == "somewhat correct":
            return float(criterion_pts) * 0.75
        elif rating.lower() == "neutral":
            return float(criterion_pts) * 0.25
        else:  # incorrect, somewhat incorrect, completely incorrect
            return 0.0

    elif grading_scheme == "strict":
        if rating.lower() == "correct":
            return float(criterion_pts)
        else:
            return 0.0

    elif grading_scheme == "round up":
        standard_score = _calculate_criterion_score(
            criterion_pts, rating, "standard", custom_scale
        )
        return float(round(standard_score))

    else:
        msg = f"Unknown grading scheme: {grading_scheme}"
        raise ValueError(msg)


def _generate_criterion_feedback(
    criterion_name: str,
    criterion_pts: int | float,
    rating: str,
    feedback: str,
    score: float,
) -> str:
    """Generate markdown feedback for a single criterion."""
    return f"""### {criterion_name} ({score:.1f}/{criterion_pts} pts)
**Rating**: {rating.upper()}
**Feedback**: {feedback}
"""


def score_submission(
    rubric_def_path: Path,
    grading_response: dict[str, Any],
) -> tuple[float, str]:
    """Score a single submission and generate markdown summary.

    Args:
        rubric_def_path: Path to the rubric TOML file
        grading_response: The grading response dict from LLM

    Returns:
        Tuple of (total_score, markdown_summary)
    """
    rubric_def = get_rubric_definition(rubric_def_path)

    total_score = 0.0
    criterion_feedbacks = []

    for criterion in rubric_def.criterion:
        # Get the slugified field name
        from rubric import slugify_criterion_name

        field_name = slugify_criterion_name(criterion.name)

        if field_name not in grading_response:
            msg = f"Criterion '{criterion.name}' (field '{field_name}') not found in grading response"
            raise ValueError(msg)

        criterion_result = grading_response[field_name]
        rating = criterion_result["rating"]
        feedback = criterion_result["feedback"]

        # Calculate score
        score = _calculate_criterion_score(
            criterion.pts,
            rating,
            criterion.grading,
            criterion.custom_scale,
        )
        total_score += score

        # Generate feedback
        criterion_feedbacks.append(
            _generate_criterion_feedback(
                criterion.name,
                criterion.pts,
                rating,
                feedback,
                score,
            )
        )

    # Generate final summary
    summary_lines = [
        f"## Grading Summary: {Path(grading_response.get('_filename', 'submission')).stem}\n",
        *criterion_feedbacks,
        f"### Total Score: {total_score:.1f}/{sum(c.pts for c in rubric_def.criterion):.1f}",
    ]

    return total_score, "\n".join(summary_lines)


def score_assignment(assignment_config_path: Path) -> None:
    """Score all graded submissions for an assignment."""
    if not assignment_config_path.exists():
        msg = f"Assignment config not found: {assignment_config_path}"
        raise FileNotFoundError(msg)

    # Load config
    import tomllib

    cfg = tomllib.loads(assignment_config_path.read_text(encoding="utf-8"))

    assignment = cfg.get("assignment", {})
    grading = cfg.get("grading", {})

    # Determine paths
    graded_dir = (
        assignment_config_path.parent / assignment.get("graded_dir", "graded")
    ).resolve()
    rubric_file = (assignment_config_path.parents[2] / grading["rubric"]).resolve()

    if not graded_dir.exists():
        msg = f"Graded directory not found: {graded_dir}"
        raise FileNotFoundError(msg)

    if not rubric_file.exists():
        msg = f"Rubric file not found: {rubric_file}"
        raise FileNotFoundError(msg)

    # Process each graded JSON file
    graded_files = sorted(graded_dir.glob("*.json"))
    if not graded_files:
        print(f"No graded files found in: {graded_dir}")
        return

    for graded_file in graded_files:
        try:
            # Load grading response
            grading_data = json.loads(graded_file.read_text(encoding="utf-8"))

            # Add filename to response for summary
            grading_data["_filename"] = graded_file.name

            # Score the submission
            total_score, summary = score_submission(rubric_file, grading_data)

            # Write summary to markdown file
            summary_file = graded_file.with_suffix(".md")
            summary_file.write_text(summary, encoding="utf-8")

            print(
                f"[scored] {graded_file.name} -> {summary_file.name} (Score: {total_score:.1f})"
            )

        except Exception as exc:
            print(f"[error] Failed to score {graded_file.name}: {exc}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run scoring pipeline.")
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to assignment config TOML.",
    )
    args = parser.parse_args()

    score_assignment(args.config)


if __name__ == "__main__":
    main()
