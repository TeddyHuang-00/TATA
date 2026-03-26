from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from assignment_config import (
    AssignmentPaths,
    ensure_assignment_dirs,
    load_assignment_file,
    resolve_assignment_paths,
)
from hooks_runtime import HookRuntime
from rubric import RubricDefinition, get_rubric_definition, slugify_criterion_name
from scoring import calculate_criterion_score

EPS = 1e-9
THRESHOLD_60 = 60.0
THRESHOLD_70 = 70.0
THRESHOLD_80 = 80.0
THRESHOLD_90 = 90.0
BucketLabel = Literal["0-59", "60-69", "70-79", "80-89", "90-100"]
BUCKET_ORDER: tuple[BucketLabel, ...] = ("0-59", "60-69", "70-79", "80-89", "90-100")


@dataclass
class CriterionAccumulator:
    pts: float
    count: int = 0
    score_sum: float = 0.0
    full_credit_count: int = 0
    zero_credit_count: int = 0
    missing_count: int = 0
    rating_counts: dict[str, int] = field(default_factory=dict)


def _inc_rating_count(counter: dict[str, int], rating: str) -> None:
    counter[rating] = counter.get(rating, 0) + 1


def _init_criterion_stats(
    rubric_def: RubricDefinition,
) -> dict[str, CriterionAccumulator]:
    stats: dict[str, CriterionAccumulator] = {}
    for criterion in rubric_def.criterion:
        stats[criterion.name] = CriterionAccumulator(pts=float(criterion.pts))
    return stats


def _build_overall_stats(
    submission_scores: list[float],
    submission_pcts: list[float],
    total_possible: float,
) -> dict[str, float | int]:
    return {
        "submission_count": len(submission_scores),
        "total_possible": total_possible,
        "score_min": min(submission_scores),
        "score_max": max(submission_scores),
        "score_avg": statistics.mean(submission_scores),
        "score_median": statistics.median(submission_scores),
        "score_stdev": statistics.stdev(submission_scores)
        if len(submission_scores) > 1
        else 0.0,
        "pct_min": min(submission_pcts),
        "pct_max": max(submission_pcts),
        "pct_avg": statistics.mean(submission_pcts),
        "pct_median": statistics.median(submission_pcts),
    }


def _build_criterion_summary(
    rubric_def: RubricDefinition,
    stats: dict[str, CriterionAccumulator],
) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for criterion in rubric_def.criterion:
        stat = stats[criterion.name]
        count = stat.count
        pts = stat.pts
        avg_score = (stat.score_sum / count) if count > 0 else 0.0
        avg_pct = (avg_score / pts * 100.0) if pts > 0 else 0.0

        summary.append({
            "criterion": criterion.name,
            "points": pts,
            "count": count,
            "avg_score": avg_score,
            "avg_pct": avg_pct,
            "full_credit_rate": (stat.full_credit_count / count * 100.0)
            if count > 0
            else 0.0,
            "zero_credit_rate": (stat.zero_credit_count / count * 100.0)
            if count > 0
            else 0.0,
            "missing_count": stat.missing_count,
            "rating_counts": dict(sorted(stat.rating_counts.items())),
        })

    summary.sort(key=lambda item: float(item["avg_pct"]))
    return summary


def _bucket_label(pct: float) -> BucketLabel:
    if pct < THRESHOLD_60:
        return "0-59"
    if pct < THRESHOLD_70:
        return "60-69"
    if pct < THRESHOLD_80:
        return "70-79"
    if pct < THRESHOLD_90:
        return "80-89"
    return "90-100"


def _load_rubric_and_files(
    assignment_config_path: Path,
) -> tuple[RubricDefinition | None, list[Path], AssignmentPaths]:
    cfg = load_assignment_file(assignment_config_path)
    paths = resolve_assignment_paths(cfg, assignment_config_path.parent)
    ensure_assignment_dirs(paths)

    rubric_file = (assignment_config_path.parents[2] / cfg.grading.rubric).resolve()
    if not rubric_file.exists():
        msg = (
            f"Rubric file not found: {rubric_file}\n"
            "Set [grading.rubric] to an existing TOML file, e.g. rubrics/example_rubric.toml."
        )
        raise FileNotFoundError(msg)

    graded_files = sorted(paths.graded_dir.glob("*.json"))
    if not graded_files:
        print(
            f"No graded files found in: {paths.graded_dir}\n"
            "Run grade stage first so graded/*.json is generated."
        )
        return None, [], paths

    return get_rubric_definition(rubric_file), graded_files, paths


def _analyze_graded_files(
    rubric_def: RubricDefinition,
    graded_files: list[Path],
) -> dict[str, Any]:
    total_possible = float(sum(float(c.pts) for c in rubric_def.criterion))
    submission_scores: list[float] = []
    submission_pcts: list[float] = []
    score_buckets: dict[BucketLabel, int] = dict.fromkeys(BUCKET_ORDER, 0)
    criterion_stats = _init_criterion_stats(rubric_def)

    for graded_file in graded_files:
        data = json.loads(graded_file.read_text(encoding="utf-8"))
        submission_total = 0.0

        for criterion in rubric_def.criterion:
            field_name = slugify_criterion_name(criterion.name)
            stat = criterion_stats[criterion.name]

            if field_name not in data:
                stat.missing_count += 1
                continue

            result = data[field_name]
            rating = str(result["rating"])
            score = calculate_criterion_score(
                criterion.pts,
                rating,
                criterion.grading,
                criterion.custom_scale,
            )

            submission_total += score
            stat.count += 1
            stat.score_sum += score
            _inc_rating_count(stat.rating_counts, rating)

            if abs(score - float(criterion.pts)) < EPS:
                stat.full_credit_count += 1
            if abs(score) < EPS:
                stat.zero_credit_count += 1

        submission_scores.append(submission_total)
        pct = (submission_total / total_possible * 100.0) if total_possible > 0 else 0.0
        submission_pcts.append(pct)
        score_buckets[_bucket_label(pct)] += 1

    overall = _build_overall_stats(submission_scores, submission_pcts, total_possible)
    criterion_summary = _build_criterion_summary(rubric_def, criterion_stats)

    return {
        "overall": overall,
        "distribution": {bucket: score_buckets[bucket] for bucket in BUCKET_ORDER},
        "criterion_summary": criterion_summary,
    }


def _build_markdown_report(result: dict[str, Any]) -> str:
    overall = result["overall"]
    distribution: dict[BucketLabel, int] = result["distribution"]
    criterion_summary: list[dict[str, Any]] = result["criterion_summary"]

    md_lines = [
        "# Post-Scoring Meta Analysis",
        "",
        "## Overall",
        f"- Submissions: {overall['submission_count']}",
        f"- Score range: {overall['score_min']:.1f} - {overall['score_max']:.1f} / {overall['total_possible']:.1f}",
        f"- Score average: {overall['score_avg']:.2f}",
        f"- Score median: {overall['score_median']:.2f}",
        f"- Score stdev: {overall['score_stdev']:.2f}",
        f"- Percentage average: {overall['pct_avg']:.2f}%",
        "",
        "## Distribution",
    ]

    md_lines.extend([f"- {bucket}: {distribution[bucket]}" for bucket in BUCKET_ORDER])
    md_lines.extend(["", "## Per-Criterion Statistics"])

    for criterion in criterion_summary:
        md_lines.extend([
            f"### {criterion['criterion']} ({criterion['points']:.1f} pts)",
            f"- Avg score: {criterion['avg_score']:.2f}",
            f"- Avg percentage: {criterion['avg_pct']:.2f}%",
            f"- Full-credit rate: {criterion['full_credit_rate']:.2f}%",
            f"- Zero-credit rate: {criterion['zero_credit_rate']:.2f}%",
            f"- Missing count: {criterion['missing_count']}",
            f"- Rating counts: {criterion['rating_counts']}",
            "",
        ])

    return "\n".join(md_lines)


def analyze_assignment(assignment_config_path: Path) -> None:
    cfg = load_assignment_file(assignment_config_path)
    hook_runtime = HookRuntime.from_config(
        cfg,
        assignment_config_path=assignment_config_path,
    )

    if hook_runtime is not None:
        hook_runtime.run(
            "before_analyze",
            {
                "assignment_config": str(assignment_config_path),
            },
        )

    rubric_def, graded_files, paths = _load_rubric_and_files(assignment_config_path)
    if not graded_files or rubric_def is None:
        return

    result = _analyze_graded_files(rubric_def, graded_files)

    json_output = paths.logs_dir / "meta_analysis.json"
    json_output.write_text(json.dumps(result, indent=2), encoding="utf-8")

    md_output = paths.logs_dir / "meta_analysis.md"
    md_output.write_text(_build_markdown_report(result), encoding="utf-8")

    print(f"[analysis] {json_output}")
    print(f"[analysis] {md_output}")

    if hook_runtime is not None:
        hook_runtime.run(
            "after_analyze",
            {
                "assignment_config": str(assignment_config_path),
                "json_output": str(json_output),
                "md_output": str(md_output),
            },
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run meta analysis on graded outputs.")
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to assignment config TOML.",
    )
    args = parser.parse_args()

    analyze_assignment(args.config)


if __name__ == "__main__":
    main()
