#!/usr/bin/env python3
"""Aggregate cross-assignment plagiarism using robust longitudinal statistics.

Pairewise Method:
1) Pairwise deletion for missing assignment pairs.
2) Logit transform on similarity scores (with lower and upper caps).
3) Per-assignment z-score normalization.
4) Longitudinal Stouffer aggregation across shared assignments.
5) Flag pairs by alpha threshold and rank by combined Z for manual review.

Individual Method:
1) Extract the maximum similarity score for each student.
2) Fit these maximum scores to a Gumbel distribution.
3) Calculate the exact p-value for each student's maximum score under the fitted distribution CDF.
4) Convert p-values to Z-scores and follow the same Stouffer aggregation and flagging as the pairwise method.

Usage examples:
  uv run python misc/plagiarism_report_aggregate.py

  uv run python misc/plagiarism_report_aggregate.py \
        --alpha 0.01 \
    --output misc/plagiarism_summary.md

  uv run python misc/plagiarism_report_aggregate.py \
    --format json \
    --output misc/plagiarism_summary.json
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import NormalDist, mean, median
from typing import Literal

from pydantic import Field, model_validator, AliasChoices

try:
    from cli_options import CliOptions, parse_cli_args
except ModuleNotFoundError:
    sys.path.append(str(Path(__file__).resolve().parents[1]))
    from cli_options import CliOptions, parse_cli_args

MIN_PARTS_FOR_ASSIGNMENT_RELATIVE_PATH = 3
MAX_PERCENTAGE = 100.0
MIN_STUDENT_ID_TOKEN_LENGTH = 5
MIN_STD_FOR_ZSCORE = 1e-12
DEFAULT_ALPHA = 0.01
DEFAULT_SCORE_CAP = 0.999
DEFAULT_SCORE_FLOOR = 0.001
MIN_VALUES_FOR_STD = 2
SCORE_FLOOR_UPPER_BOUND = 0.5
SCORE_CAP_LOWER_BOUND = 0.5
DEFAULT_PAIRS_GLOB = "**/plagiarism/all_pairs.json"
MIN_GUMBEL_BETA = 1e-9
MIN_PROBABILITY = 1e-15
MAX_PROBABILITY = 1.0 - MIN_PROBABILITY
EULER_MASCHERONI = 0.5772156649015329
SQRT_SIX_OVER_PI = 0.779696801233676
STANDARD_NORMAL = NormalDist()


@dataclass(frozen=True)
class MatchRecord:
    assignment: str
    test_file: str
    reference_file: str
    test_similarity: float
    reference_similarity: float
    token_overlap: int

    @property
    def max_similarity(self) -> float:
        return max(self.test_similarity, self.reference_similarity)


@dataclass(frozen=True)
class PairAssignmentScore:
    assignment: str
    student_a: str
    student_b: str
    raw_similarity_pct: float
    logit_similarity: float
    z_score: float
    one_sided_p_value: float


@dataclass(frozen=True)
class AssignmentDistributionStat:
    assignment: str
    pair_count: int
    raw_mean: float
    raw_median: float
    raw_std: float
    logit_mean: float
    logit_std: float


@dataclass(frozen=True)
class PairCombinedStat:
    student_a: str
    student_b: str
    shared_assignments: int
    combined_z: float
    one_sided_p_value: float
    assignment_scores: list[PairAssignmentScore]


@dataclass(frozen=True)
class IndividualAssignmentScore:
    assignment: str
    student: str
    raw_max_similarity_pct: float
    gumbel_location: float
    gumbel_scale: float
    one_sided_p_value: float
    z_score: float


@dataclass(frozen=True)
class IndividualCombinedStat:
    student: str
    shared_assignments: int
    combined_z: float
    one_sided_p_value: float
    assignment_scores: list[IndividualAssignmentScore]


@dataclass(frozen=True)
class AggregatePayload:
    assignments_root: str
    pairs_glob: str
    pair_data_files_found: int
    pair_data_files_parsed: int
    parse_errors: int
    pairwise_alpha: float
    individual_alpha: float
    score_floor: float
    score_cap: float
    total_pair_assignment_points: int
    assignment_stats: list[AssignmentDistributionStat]
    tested_pairs: int
    significant_pairs: list[PairCombinedStat]
    tested_students: int
    significant_students: list[IndividualCombinedStat]


@dataclass(frozen=True)
class BuildConfig:
    assignments_root: Path
    pairs_glob: str
    pairwise_alpha: float
    individual_alpha: float
    score_floor: float
    score_cap: float


class PlagiarismAggregateCliOptions(CliOptions):
    assignments_root: Path = Field(
        default=Path("assignments"),
        validation_alias=AliasChoices("assignments-root", "root"),
        description="Root directory containing assignment folders.",
    )
    pairs_glob: str = Field(
        default=DEFAULT_PAIRS_GLOB,
        validation_alias=AliasChoices("pairs-glob", "glob"),
        description="Glob pattern (relative to assignments_root) for full pair data JSON files.",
    )
    pairwise_alpha: float = Field(
        default=DEFAULT_ALPHA,
        validation_alias=AliasChoices("pairwise-alpha", "palpha", "pa"),
        description="One-sided significance threshold for pairwise combined z-score p-values.",
    )
    individual_alpha: float = Field(
        default=DEFAULT_ALPHA,
        validation_alias=AliasChoices("individual-alpha", "ialpha", "ia"),
        description="One-sided significance threshold for individual combined z-score p-values.",
    )
    score_floor: float = Field(
        default=DEFAULT_SCORE_FLOOR,
        validation_alias=AliasChoices("score-floor", "floor"),
        description="Lower decimal clamp before logit transform.",
    )
    score_cap: float = Field(
        default=DEFAULT_SCORE_CAP,
        validation_alias=AliasChoices("score-cap", "cap"),
        description="Upper decimal clamp before logit transform.",
    )
    format: Literal["text", "json"] = Field(
        default="text",
        validation_alias=AliasChoices("format", "f"),
        description="Output format.",
    )
    output: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("output", "o"),
        description="Optional output file path. If omitted, prints to stdout.",
    )

    @model_validator(mode="after")
    def _validate_options(self) -> PlagiarismAggregateCliOptions:
        root = self.assignments_root.resolve()
        if not root.exists() or not root.is_dir():
            msg = f"--assignments-root not found or not a directory: {root}"
            raise ValueError(msg)
        self.assignments_root = root

        if not 0 < self.pairwise_alpha <= 1:
            msg = "--pairwise-alpha must be in (0, 1]"
            raise ValueError(msg)

        if not 0 < self.individual_alpha <= 1:
            msg = "--individual-alpha must be in (0, 1]"
            raise ValueError(msg)

        if not 0 < self.score_floor < SCORE_FLOOR_UPPER_BOUND:
            msg = "--score-floor must be in (0, 0.5)"
            raise ValueError(msg)

        if not SCORE_CAP_LOWER_BOUND < self.score_cap < 1:
            msg = "--score-cap must be in (0.5, 1)"
            raise ValueError(msg)

        if self.score_floor >= self.score_cap:
            msg = "--score-floor must be smaller than --score-cap"
            raise ValueError(msg)

        return self


def _extract_assignment_name(report_file: Path, assignments_root: Path) -> str:
    try:
        relative = report_file.relative_to(assignments_root)
    except ValueError:
        return report_file.parents[1].name

    if len(relative.parts) < MIN_PARTS_FOR_ASSIGNMENT_RELATIVE_PATH:
        return report_file.parents[1].name
    return relative.parts[0]


def _student_identity(file_name: str) -> tuple[str, str]:
    normalized = file_name.replace("\\", "/")
    stem = Path(normalized).stem.lower()
    tokens = [token for token in stem.split("_") if token]
    if not tokens:
        return stem, stem

    name_token = tokens[0]
    id_token = next(
        (
            token
            for token in tokens[1:]
            if token.isdigit() and len(token) >= MIN_STUDENT_ID_TOKEN_LENGTH
        ),
        None,
    )
    if id_token is not None:
        key = f"id:{id_token}"
        return key, f"{name_token}({id_token})"
    return f"name:{name_token}", name_token


def _pair_key(student_a: str, student_b: str) -> tuple[str, str]:
    if student_a <= student_b:
        return student_a, student_b
    return student_b, student_a


def _safe_std(values: list[float]) -> float:
    if len(values) < MIN_VALUES_FOR_STD:
        return 0.0
    mu = mean(values)
    variance = sum((value - mu) ** 2 for value in values) / len(values)
    return math.sqrt(variance)


def _normal_sf(z_value: float) -> float:
    # One-sided upper-tail p-value for standard normal.
    return 0.5 * math.erfc(z_value / math.sqrt(2.0))


def _logit_from_percent(score_pct: float, floor: float, cap: float) -> float:
    decimal_score = score_pct / 100.0
    bounded = min(max(decimal_score, floor), cap)
    return math.log(bounded / (1.0 - bounded))


def _clamp_probability(probability: float) -> float:
    return min(max(probability, MIN_PROBABILITY), MAX_PROBABILITY)


def _fit_gumbel_from_sample(values: list[float]) -> tuple[float, float]:
    sample_mean = mean(values)
    sample_std = _safe_std(values)
    scale = max(sample_std * SQRT_SIX_OVER_PI, MIN_GUMBEL_BETA)
    location = sample_mean - EULER_MASCHERONI * scale
    return location, scale


def _gumbel_cdf(value: float, location: float, scale: float) -> float:
    standardized = (value - location) / scale
    return math.exp(-math.exp(-standardized))


def _parse_pair_data_file(
    pair_data_file: Path, assignments_root: Path
) -> list[MatchRecord]:
    payload = json.loads(pair_data_file.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        msg = f"Invalid pair data payload (not an object): {pair_data_file}"
        raise ValueError(msg)

    pairs = payload.get("pairs")
    if not isinstance(pairs, list):
        msg = f"Invalid pair data payload (missing list 'pairs'): {pair_data_file}"
        raise ValueError(msg)

    assignment = _extract_assignment_name(pair_data_file, assignments_root)
    records: list[MatchRecord] = []
    for pair in pairs:
        if not isinstance(pair, dict):
            continue

        test_file = pair.get("test_file")
        reference_file = pair.get("reference_file")
        test_similarity = pair.get("test_similarity_pct")
        reference_similarity = pair.get("reference_similarity_pct")
        token_overlap = pair.get("token_overlap")

        if not isinstance(test_file, str) or not isinstance(reference_file, str):
            continue
        if not isinstance(test_similarity, int | float):
            continue
        if not isinstance(reference_similarity, int | float):
            continue
        if not isinstance(token_overlap, int):
            continue

        records.append(
            MatchRecord(
                assignment=assignment,
                test_file=test_file,
                reference_file=reference_file,
                test_similarity=float(test_similarity),
                reference_similarity=float(reference_similarity),
                token_overlap=token_overlap,
            )
        )

    return records


def _collect_best_pair_scores(
    records: list[MatchRecord],
) -> tuple[
    dict[str, dict[tuple[str, str], float]],
    dict[str, str],
]:
    # assignment -> (pair -> best max similarity in that assignment)
    per_assignment_pair_scores: dict[str, dict[tuple[str, str], float]] = defaultdict(
        dict
    )
    student_labels: dict[str, str] = {}

    for record in records:
        student_a, student_a_label = _student_identity(record.test_file)
        student_b, student_b_label = _student_identity(record.reference_file)
        student_labels[student_a] = student_a_label
        student_labels[student_b] = student_b_label

        if student_a == student_b:
            continue

        pair = _pair_key(student_a, student_b)
        score = record.max_similarity
        current = per_assignment_pair_scores[record.assignment].get(pair)
        if current is None or score > current:
            per_assignment_pair_scores[record.assignment][pair] = score

    return per_assignment_pair_scores, student_labels


def _build_assignment_stats_and_zscores(
    per_assignment_pair_scores: dict[str, dict[tuple[str, str], float]],
    floor: float,
    cap: float,
) -> tuple[
    list[AssignmentDistributionStat],
    dict[str, dict[tuple[str, str], PairAssignmentScore]],
]:
    assignment_stats: list[AssignmentDistributionStat] = []
    assignment_pair_details: dict[str, dict[tuple[str, str], PairAssignmentScore]] = {}

    for assignment, pair_scores in per_assignment_pair_scores.items():
        if not pair_scores:
            continue

        raw_values = list(pair_scores.values())
        logit_values = [_logit_from_percent(value, floor, cap) for value in raw_values]
        logit_mu = mean(logit_values)
        logit_std = _safe_std(logit_values)

        pair_detail_map: dict[tuple[str, str], PairAssignmentScore] = {}
        for pair, raw_score in pair_scores.items():
            logit_score = _logit_from_percent(raw_score, floor, cap)
            if logit_std <= MIN_STD_FOR_ZSCORE:
                z_score = 0.0
            else:
                z_score = (logit_score - logit_mu) / logit_std
            pair_detail_map[pair] = PairAssignmentScore(
                assignment=assignment,
                student_a=pair[0],
                student_b=pair[1],
                raw_similarity_pct=raw_score,
                logit_similarity=logit_score,
                z_score=z_score,
                one_sided_p_value=_normal_sf(z_score),
            )

        assignment_pair_details[assignment] = pair_detail_map
        assignment_stats.append(
            AssignmentDistributionStat(
                assignment=assignment,
                pair_count=len(raw_values),
                raw_mean=mean(raw_values),
                raw_median=median(raw_values),
                raw_std=_safe_std(raw_values),
                logit_mean=logit_mu,
                logit_std=logit_std,
            )
        )

    assignment_stats.sort(key=lambda item: item.assignment)
    return assignment_stats, assignment_pair_details


def _combine_pairs_stouffer(
    assignment_pair_details: dict[str, dict[tuple[str, str], PairAssignmentScore]],
    student_labels: dict[str, str],
) -> list[PairCombinedStat]:
    pair_history: dict[tuple[str, str], list[PairAssignmentScore]] = defaultdict(list)

    for pair_details in assignment_pair_details.values():
        for pair, detail in pair_details.items():
            pair_history[pair].append(detail)

    combined: list[PairCombinedStat] = []
    for pair, history in pair_history.items():
        ordered = sorted(history, key=lambda item: item.assignment)
        z_sum = sum(item.z_score for item in ordered)
        combined_z = z_sum / math.sqrt(len(ordered))
        p_value = _normal_sf(combined_z)

        combined.append(
            PairCombinedStat(
                student_a=student_labels.get(pair[0], pair[0]),
                student_b=student_labels.get(pair[1], pair[1]),
                shared_assignments=len(ordered),
                combined_z=combined_z,
                one_sided_p_value=p_value,
                assignment_scores=ordered,
            )
        )

    return sorted(
        combined,
        key=lambda item: (
            -item.combined_z,
            item.one_sided_p_value,
            -item.shared_assignments,
        ),
    )


def _build_individual_assignment_scores(
    per_assignment_pair_scores: dict[str, dict[tuple[str, str], float]],
) -> dict[str, dict[str, IndividualAssignmentScore]]:
    assignment_student_details: dict[str, dict[str, IndividualAssignmentScore]] = {}

    for assignment, pair_scores in per_assignment_pair_scores.items():
        if not pair_scores:
            continue

        student_max_scores: dict[str, float] = {}
        for (student_a, student_b), raw_score in pair_scores.items():
            student_max_scores[student_a] = max(
                student_max_scores.get(student_a, 0.0), raw_score
            )
            student_max_scores[student_b] = max(
                student_max_scores.get(student_b, 0.0), raw_score
            )

        if not student_max_scores:
            continue

        raw_max_values = list(student_max_scores.values())
        location, scale = _fit_gumbel_from_sample(raw_max_values)

        assignment_map: dict[str, IndividualAssignmentScore] = {}
        for student, raw_max in student_max_scores.items():
            cdf_value = _clamp_probability(_gumbel_cdf(raw_max, location, scale))
            p_value = _clamp_probability(1.0 - cdf_value)
            z_score = STANDARD_NORMAL.inv_cdf(1.0 - p_value)
            assignment_map[student] = IndividualAssignmentScore(
                assignment=assignment,
                student=student,
                raw_max_similarity_pct=raw_max,
                gumbel_location=location,
                gumbel_scale=scale,
                one_sided_p_value=p_value,
                z_score=z_score,
            )

        assignment_student_details[assignment] = assignment_map

    return assignment_student_details


def _combine_students_stouffer(
    assignment_student_details: dict[str, dict[str, IndividualAssignmentScore]],
    student_labels: dict[str, str],
) -> list[IndividualCombinedStat]:
    student_history: dict[str, list[IndividualAssignmentScore]] = defaultdict(list)

    for student_scores in assignment_student_details.values():
        for student, detail in student_scores.items():
            student_history[student].append(detail)

    combined: list[IndividualCombinedStat] = []
    for student, history in student_history.items():
        ordered = sorted(history, key=lambda item: item.assignment)
        z_sum = sum(item.z_score for item in ordered)
        combined_z = z_sum / math.sqrt(len(ordered))
        p_value = _normal_sf(combined_z)
        combined.append(
            IndividualCombinedStat(
                student=student_labels.get(student, student),
                shared_assignments=len(ordered),
                combined_z=combined_z,
                one_sided_p_value=p_value,
                assignment_scores=ordered,
            )
        )

    return sorted(
        combined,
        key=lambda item: (
            -item.combined_z,
            item.one_sided_p_value,
            -item.shared_assignments,
        ),
    )


def _load_assignment_records(
    config: BuildConfig,
) -> tuple[list[Path], int, int, dict[str, list[MatchRecord]]]:
    pair_data_files = sorted(config.assignments_root.glob(config.pairs_glob))
    pair_data_parsed = 0
    parse_errors = 0
    per_assignment_records: dict[str, list[MatchRecord]] = {}

    for pair_data_file in pair_data_files:
        try:
            records = _parse_pair_data_file(pair_data_file, config.assignments_root)
            assignment = _extract_assignment_name(
                pair_data_file, config.assignments_root
            )
            per_assignment_records[assignment] = records
            pair_data_parsed += 1
        except Exception:
            parse_errors += 1

    return pair_data_files, pair_data_parsed, parse_errors, per_assignment_records


def _build_payload(config: BuildConfig) -> AggregatePayload:
    pair_data_files, pair_data_parsed, parse_errors, per_assignment_records = (
        _load_assignment_records(config)
    )
    per_assignment_pair_scores, student_labels = _collect_best_pair_scores(
        [
            record
            for assignment_records in per_assignment_records.values()
            for record in assignment_records
        ],
    )
    assignment_stats, assignment_pair_details = _build_assignment_stats_and_zscores(
        per_assignment_pair_scores,
        floor=config.score_floor,
        cap=config.score_cap,
    )
    combined_pairs = _combine_pairs_stouffer(
        assignment_pair_details,
        student_labels,
    )
    combined_students = _combine_students_stouffer(
        _build_individual_assignment_scores(per_assignment_pair_scores),
        student_labels,
    )

    return AggregatePayload(
        assignments_root=str(config.assignments_root),
        pairs_glob=config.pairs_glob,
        pair_data_files_found=len(pair_data_files),
        pair_data_files_parsed=pair_data_parsed,
        parse_errors=parse_errors,
        pairwise_alpha=config.pairwise_alpha,
        individual_alpha=config.individual_alpha,
        score_floor=config.score_floor,
        score_cap=config.score_cap,
        total_pair_assignment_points=sum(
            len(pair_scores) for pair_scores in per_assignment_pair_scores.values()
        ),
        assignment_stats=assignment_stats,
        tested_pairs=len(combined_pairs),
        significant_pairs=[
            item
            for item in combined_pairs
            if item.one_sided_p_value <= config.pairwise_alpha
        ],
        tested_students=len(combined_students),
        significant_students=[
            item
            for item in combined_students
            if item.one_sided_p_value <= config.individual_alpha
        ],
    )


def _to_text(payload: AggregatePayload) -> str:
    lines: list[str] = []
    lines.extend([
        "Plagiarism Cross-Assignment Aggregate Report (Robust Mode)",
        "=" * 58,
        f"Assignments root: {payload.assignments_root}",
        f"Pairs glob: {payload.pairs_glob}",
        f"Pair data files found: {payload.pair_data_files_found}",
        f"Pair data files parsed: {payload.pair_data_files_parsed}",
        f"Parse errors: {payload.parse_errors}",
        f"Logit score floor/cap (decimal): {payload.score_floor:.3f} / {payload.score_cap:.3f}",
        f"Pairwise alpha (one-sided): {payload.pairwise_alpha:.4f}",
        f"Individual alpha (one-sided): {payload.individual_alpha:.4f}",
        "",
        "Global Summary",
        "-" * 14,
        f"Total pair-assignment points: {payload.total_pair_assignment_points}",
        f"Tested cross-assignment pairs: {payload.tested_pairs}",
        f"Significant pairs (p <= pairwise alpha): {len(payload.significant_pairs)}",
        f"Tested students: {payload.tested_students}",
        f"Significant students (p <= individual alpha): {len(payload.significant_students)}",
        "",
        "Assignment Distributions (after logit transform)",
        "-" * 43,
    ])

    if payload.assignment_stats:
        lines.extend(
            f"- {item.assignment}: pairs={item.pair_count}, "
            f"raw_mean={item.raw_mean:.2f}%, raw_median={item.raw_median:.2f}%, raw_std={item.raw_std:.2f}, "
            f"logit_mean={item.logit_mean:.4f}, logit_std={item.logit_std:.4f}"
            for item in payload.assignment_stats
        )
    else:
        lines.append("- No parseable assignment pair scores found.")

    lines.extend(["", "Significant Pairs by Stouffer Combined Z", "-" * 38])
    if payload.significant_pairs:
        for index, item in enumerate(payload.significant_pairs, start=1):
            lines.extend(
                [
                    (
                        f"{index}. {item.student_a} vs {item.student_b} | "
                        f"K={item.shared_assignments}, combined_z={item.combined_z:.4f}, "
                        f"p={item.one_sided_p_value:.6g}"
                    ).strip()
                ]
                + [
                    f"   - {score.assignment}: raw={score.raw_similarity_pct:.2f}%, "
                    f"logit={score.logit_similarity:.4f}, z={score.z_score:.4f}, "
                    f"p={score.one_sided_p_value:.6g}"
                    for score in item.assignment_scores
                ]
                + [""]
            )
            # per_assignment = ", ".join(
            #     (
            #         f"{score.assignment}: raw={score.raw_similarity_pct:.2f}% "
            #         f"logit={score.logit_similarity:.4f} z={score.z_score:.4f}"
            #     )
            #     for score in item.assignment_scores
            # )
            # lines.append(f"   details: {per_assignment}")
    else:
        lines.append("- No significant pairs found under current alpha.")

    lines.extend(["", "Significant Students by Stouffer Combined Z", "-" * 41])
    if payload.significant_students:
        for index, item in enumerate(payload.significant_students, start=1):
            lines.extend(
                [
                    (
                        f"{index}. {item.student} | "
                        f"K={item.shared_assignments}, combined_z={item.combined_z:.4f}, "
                        f"p={item.one_sided_p_value:.6g}"
                    ).strip()
                ]
                + [
                    f"   - {score.assignment}: raw_max={score.raw_max_similarity_pct:.2f}%, "
                    f"gumbel_loc={score.gumbel_location:.4f}, gumbel_scale={score.gumbel_scale:.4f}, "
                    f"z={score.z_score:.4f}, p={score.one_sided_p_value:.6g}"
                    for score in item.assignment_scores
                ]
                + [""]
            )
    else:
        lines.append("- No significant students found under current alpha.")

    lines.extend([
        "",
        "Interpretation Note:",
        "- High combined Z is a triage signal, not automatic proof of misconduct.",
        "- Perform manual review for shared logic flaws, uncommon naming patterns, and identical mistakes.",
    ])
    return "\n".join(lines)


def _to_json(payload: AggregatePayload) -> str:
    return json.dumps(asdict(payload), indent=2)


def main() -> None:
    args = parse_cli_args(PlagiarismAggregateCliOptions)

    payload = _build_payload(
        BuildConfig(
            assignments_root=args.assignments_root,
            pairs_glob=args.pairs_glob,
            pairwise_alpha=args.pairwise_alpha,
            individual_alpha=args.individual_alpha,
            score_floor=args.score_floor,
            score_cap=args.score_cap,
        )
    )

    output = _to_json(payload) if args.format == "json" else _to_text(payload)

    if args.output is None:
        print(output)
        return

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output + "\n", encoding="utf-8")
    print(f"Wrote report to: {args.output}")


if __name__ == "__main__":
    main()
