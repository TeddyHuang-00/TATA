from __future__ import annotations

from html import escape
import json
from pathlib import Path
from typing import Any

from assignment_config import (
    ensure_assignment_dirs,
    load_assignment_file,
    resolve_assignment_paths,
)
from hooks_runtime import HookRuntime
from rubric import get_rubric_definition


def calculate_criterion_score(
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
        standard_score = calculate_criterion_score(
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


def _generate_criterion_feedback_plain(
    criterion_name: str,
    criterion_pts: int | float,
    rating: str,
    feedback: str,
    score: float,
) -> str:
    """Generate plain-text feedback for a single criterion."""
    return "\n".join(
        [
            f"- {criterion_name} ({score:.1f}/{criterion_pts} pts)",
            f"  - Rating: {rating.upper()}",
            f"  - Feedback: {feedback}",
        ]
    )


def _generate_criterion_feedback_html(
    criterion_name: str,
    criterion_pts: int | float,
    rating: str,
    feedback: str,
    score: float,
) -> str:
    """Generate HTML feedback for a single criterion."""
    return (
        f"<section class=\"criterion\">"
        f"<h3>{escape(criterion_name)} ({score:.1f}/{criterion_pts} pts)</h3>"
        f"<p><strong>Rating:</strong> {escape(rating.upper())}</p>"
        f"<p><strong>Feedback:</strong> {escape(feedback)}</p>"
        f"</section>"
    )


def _summary_suffix_for_style(output_style: str) -> str:
    if output_style == "html":
        return ".html"
    if output_style == "plain":
        return ".txt"
    return ".md"


def _summary_subdir_for_style(output_style: str) -> str:
    if output_style == "html":
        return "html"
    if output_style == "plain":
        return "txt"
    return "md"


def score_submission(
    rubric_def_path: Path,
    grading_response: dict[str, Any],
    report_detail: str = "full",
    output_style: str = "markdown",
) -> tuple[float, str]:
    """Score a single submission and generate summary.

    Args:
        rubric_def_path: Path to the rubric TOML file
        grading_response: The grading response dict from LLM

    Returns:
        Tuple of (total_score, summary)
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
        score = calculate_criterion_score(
            criterion.pts,
            rating,
            criterion.grading,
            criterion.custom_scale,
        )
        total_score += score

        is_full_mark = score >= float(criterion.pts)
        if report_detail == "slim" and is_full_mark:
            continue

        if output_style == "plain":
            criterion_feedbacks.append(
                _generate_criterion_feedback_plain(
                    criterion.name,
                    criterion.pts,
                    rating,
                    feedback,
                    score,
                )
            )
        elif output_style == "html":
            criterion_feedbacks.append(
                _generate_criterion_feedback_html(
                    criterion.name,
                    criterion.pts,
                    rating,
                    feedback,
                    score,
                )
            )
        else:
            criterion_feedbacks.append(
                _generate_criterion_feedback(
                    criterion.name,
                    criterion.pts,
                    rating,
                    feedback,
                    score,
                )
            )

    max_score = sum(c.pts for c in rubric_def.criterion)
    submission_name = Path(grading_response.get("_filename", "submission")).stem

    if output_style == "plain":
        summary_lines = [f"Grading Summary: {submission_name}"]
        if report_detail == "slim" and not criterion_feedbacks:
            summary_lines.append("- All criteria received full marks.")
        else:
            summary_lines.extend(criterion_feedbacks)
        summary_lines.append(f"Total Score: {total_score:.1f}/{max_score:.1f}")
    elif output_style == "html":
        summary_lines = [
            "<!doctype html>",
            "<html lang=\"en\">",
            "<head>",
            "<meta charset=\"utf-8\">",
            "<title>Grading Summary</title>",
            "<style>body{font-family:Arial,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;line-height:1.5}h1,h2,h3{margin:0.6rem 0}section.criterion{border:1px solid #ddd;border-radius:8px;padding:0.8rem 1rem;margin:0.8rem 0}footer{margin-top:1.5rem;font-weight:700}</style>",
            "</head>",
            "<body>",
            f"<h1>Grading Summary: {escape(submission_name)}</h1>",
        ]
        if report_detail == "slim" and not criterion_feedbacks:
            summary_lines.append("<p>All criteria received full marks.</p>")
        else:
            summary_lines.extend(criterion_feedbacks)
        summary_lines.append(f"<footer>Total Score: {total_score:.1f}/{max_score:.1f}</footer>")
        summary_lines.append("</body>")
        summary_lines.append("</html>")
    else:
        summary_lines = [f"## Grading Summary: {submission_name}\n"]
        if report_detail == "slim" and not criterion_feedbacks:
            summary_lines.append("- All criteria received full marks.")
        else:
            summary_lines.extend(criterion_feedbacks)
        summary_lines.append(f"### Total Score: {total_score:.1f}/{max_score:.1f}")

    return total_score, "\n".join(summary_lines)


def score_assignment(assignment_config_path: Path) -> None:
    """Score all graded submissions for an assignment."""
    cfg = load_assignment_file(assignment_config_path)
    grading = cfg.grading
    scoring = cfg.scoring
    hook_runtime = HookRuntime.from_config(
        cfg,
        assignment_config_path=assignment_config_path,
    )
    paths = resolve_assignment_paths(cfg, assignment_config_path.parent)
    ensure_assignment_dirs(paths)

    # Determine paths
    graded_dir = paths.graded_dir
    scored_base_dir = (assignment_config_path.parent / "scored").resolve()
    scored_style_dir = scored_base_dir / _summary_subdir_for_style(scoring.output_style)
    scored_style_dir.mkdir(parents=True, exist_ok=True)
    rubric_file = (assignment_config_path.parents[2] / grading.rubric).resolve()

    if not rubric_file.exists():
        msg = (
            f"Rubric file not found: {rubric_file}\n"
            "Set [grading.rubric] to an existing TOML file, e.g. rubrics/example_rubric.toml."
        )
        raise FileNotFoundError(msg)

    # Process each graded JSON file
    graded_files = sorted(graded_dir.glob("*.json"))
    if not graded_files:
        print(
            f"No graded files found in: {graded_dir}\n"
            "Run grade stage first so graded/*.json is generated."
        )
        return

    if hook_runtime is not None:
        hook_runtime.run(
            "before_score",
            {
                "assignment_config": str(assignment_config_path),
                "graded_dir": str(graded_dir),
                "graded_count": len(graded_files),
            },
        )

    scored_count = 0
    error_count = 0

    for graded_file in graded_files:
        try:
            # Load grading response
            grading_data = json.loads(graded_file.read_text(encoding="utf-8"))

            # Add filename to response for summary
            grading_data["_filename"] = graded_file.name

            # Score the submission
            total_score, summary = score_submission(
                rubric_file,
                grading_data,
                report_detail=scoring.report_detail,
                output_style=scoring.output_style,
            )

            summary_file = scored_style_dir / (
                graded_file.stem + _summary_suffix_for_style(scoring.output_style)
            )
            summary_file.write_text(summary, encoding="utf-8")

            print(
                f"[scored] {graded_file.name} -> {summary_file.name} (Score: {total_score:.1f})"
            )
            scored_count += 1

        except Exception as exc:
            print(f"[error] Failed to score {graded_file.name}: {exc}")
            error_count += 1

    if hook_runtime is not None:
        hook_runtime.run(
            "after_score",
            {
                "assignment_config": str(assignment_config_path),
                "graded_dir": str(graded_dir),
                "scored_count": scored_count,
                "error_count": error_count,
            },
        )


# Backward compatibility for older imports.
_calculate_criterion_score = calculate_criterion_score


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
