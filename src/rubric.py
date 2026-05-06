from __future__ import annotations

import re
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, Field, create_model, model_validator


class Binary(StrEnum):
    """Represents the correctness level on a binary scale."""

    CORRECT = "correct"
    """The answer is functionally equivalent to the reference answer.

    This includes solutions that differ only in COSMETIC ways that do NOT change behavior or output:
    - Different variable names (e.g., 'df' vs 'data')
    - Equivalent API calls (e.g., np.mean(arr) vs arr.mean())
    - Different but semantically equivalent code (e.g., list comprehension vs for-loop)
    - Minor formatting, spacing, or comment differences
    - Different ordering of independent statements

    Only mark INCORRECT when the change would produce a different result, error, or missing output."""

    INCORRECT = "incorrect"
    """The answer would produce a wrong result, uses a fundamentally wrong approach,
    produces an error, or is missing entirely (e.g., pass, NotImplementedError, empty cell)."""


class Ternary(StrEnum):
    """Represents the correctness level on a three-point scale."""

    CORRECT = "correct"
    """The answer is functionally equivalent to the reference answer.

    COSMETIC differences (different variable names, equivalent API calls, equivalent constructs,
    formatting, comment differences) are ACCEPTABLE and should be marked CORRECT."""

    PARTIAL = "partial"
    """The answer has some correct elements but has at least one FUNCTIONAL issue:
    - A minor mistake that would slightly change output (e.g., wrong plot title, off-by-one)
    - Missing a secondary required step while the core logic is correct
    - Using a different approach that mostly works but misses edge cases

    Do NOT mark PARTIAL for cosmetic differences alone."""

    INCORRECT = "incorrect"
    """The answer would produce a fundamentally wrong result, uses the wrong algorithm,
    produces an error, or is missing entirely (pass, NotImplementedError, empty)."""


class Likert(StrEnum):
    """Represents the correctness level on a five-point Likert scale."""

    COMPLETELY_CORRECT = "completely correct"
    """Identical to reference OR differs only in cosmetic ways (naming, formatting, equivalent constructs).
    Output/behavior is exactly the same as reference."""

    SOMEWHAT_CORRECT = "somewhat correct"
    """Core logic is correct but has one minor functional difference:
    - Small edge case not handled
    - Minor numerical precision difference that doesn't change interpretation
    - One secondary step slightly off while main result is correct"""

    NEUTRAL = "neutral"
    """Substantial parts are correct and substantial parts are wrong. The answer shows some
    understanding but also contains significant errors or omissions that affect output."""

    SOMEWHAT_INCORRECT = "somewhat incorrect"
    """Shows some awareness of the approach but the implementation is mostly wrong.
    Has significant functional errors but with traces of correct understanding."""

    COMPLETELY_INCORRECT = "completely incorrect"
    """The answer is fundamentally wrong, uses wrong approach, produces errors,
    or is missing entirely (pass, NotImplementedError, empty cell)."""


class Rating(StrEnum):
    """Represents the type of rating scale to use for evaluation."""

    BINARY = "binary"
    """Only two ratings: correct or incorrect."""
    TERNARY = "ternary"
    """Three ratings: correct, partially correct, or incorrect."""
    LIKERT = "likert"
    """A Likert scale with 5 levels, from completely incorrect to completely correct."""


class Grading(StrEnum):
    """Represents the type of grading to use for evaluation."""

    STANDARD = "standard"
    """Each rating scales evenly contributes to the final score.

    For example, in a binary scale, correct = 1 and incorrect = 0.
    In a ternary scale, correct = 1, partially correct = 0.5, and incorrect = 0."""
    STRICT = "strict"
    """Only the highest rating contributes to the final score.

    For example, in a ternary scale, correct = 1 and both partially correct and incorrect = 0."""
    ROUND_UP = "round up"
    """The final score is rounded up to the nearest whole number.

    For example, in a ternary scale, correct = 1, partially correct = 0.5 (rounded up to 1), and incorrect = 0."""
    CUSTOM = "custom"
    """Custom grading scheme defined by the user.

    Must be used in conjunction with a custom scale that exactly matches the rating scale from lowest to highest.

    For example, for a ternary scale, the custom scale could be defined as [0, 0.5, 1] where correct = 1, partially correct = 0.5, and incorrect = 0."""


class Criterion(BaseModel):
    """Data model for configurable criterion."""

    name: str = Field(..., description="Name of the criterion.")
    desc: str = Field(
        ...,
        description="Description of the criterion. Should be specific enough for the model to locate the part of answer. This also helps the model understand the evaluation criteria and how to apply them.",
    )
    rating: Rating = Field(..., description="Rating scale to use for evaluation.")
    grading: Grading | None = Field(
        ...,
        description="Grading scheme to use for evaluation. If not specified, it will default to STANDARD.",
    )
    custom_scale: list[float] | None = Field(
        None,
        description="Custom grading scale to use when grading is set to CUSTOM. The length of the custom scale must exactly match the number of ratings in the rating scale, and the values should be ordered from lowest to highest.",
    )
    pts: int | float = Field(
        ..., description="Total points allocated for this criterion."
    )

    @model_validator(mode="after")
    def validate_custom_scale(self) -> Self:
        """Validate the custom grading scale when grading is set to CUSTOM."""
        if self.grading == Grading.CUSTOM:
            if self.custom_scale is None:
                msg = f"Custom grading scale must be provided when grading is set to CUSTOM for criterion '{self.name}'."
                raise ValueError(msg)

            expected_length = len(RATING_ENUM_MAP[self.rating])
            if len(self.custom_scale) != expected_length:
                msg = f"Length of custom grading scale must match the number of ratings in the rating scale for criterion '{self.name}'. Expected {expected_length} but got {len(self.custom_scale)}."
                raise ValueError(msg)

            if not all(
                self.custom_scale[i] <= self.custom_scale[i + 1]
                for i in range(len(self.custom_scale) - 1)
            ):
                msg = f"Values in custom grading scale must be ordered from lowest to highest for criterion '{self.name}'."
                raise ValueError(msg)
        return self


class RubricDefinition(BaseModel):
    criterion: list[Criterion] = Field(default_factory=list)

    model_config = {
        "title": "Rubric Definition",
    }


def get_rubric_definition(file: Path) -> RubricDefinition:
    if not file.exists():
        msg = f"Rubric definition file not found: {file}"
        raise FileNotFoundError(msg)

    config = tomllib.loads(file.read_text(encoding="utf-8"))

    return RubricDefinition.model_validate(config)


RATING_ENUM_MAP: dict[Rating, type[StrEnum]] = {
    Rating.BINARY: Binary,
    Rating.TERNARY: Ternary,
    Rating.LIKERT: Likert,
}


def slugify_criterion_name(name: str) -> str:
    """Convert a criterion name to a valid snake_case field name."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name.strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    if not slug:
        msg = f"Criterion name '{name}' becomes empty after slugify."
        raise ValueError(msg)
    if slug[0].isdigit():
        slug = f"c_{slug}"
    return slug


class CriterionResult(BaseModel):
    """One criterion grading result returned by the model."""

    chain_of_thought: str = Field(
        ...,
        description=(
            "Your reasoning MUST follow this template:\n\n"
            "1. DIFFERENCES: List every difference between reference and student answer.\n"
            "   For each difference, classify as:\n"
            "   - FUNCTIONAL: would change output/behavior or cause an error\n"
            "   - COSMETIC: different naming, formatting, equivalent approach that produces same result\n"
            "   - MISSING: required element absent from student answer\n\n"
            "2. RATING: Based on the differences above, the rating is ___ because ___.\n"
            "   - If only COSMETIC differences → highest correctness level\n"
            "   - If some FUNCTIONAL or MISSING but core understanding shown → intermediate level\n"
            "   - If fundamentally wrong or empty → lowest correctness level\n\n"
            "3. FEEDBACK: Brief actionable feedback (null if only cosmetic differences and no improvement needed)."
        ),
    )
    feedback: str | None = Field(
        ...,
        description=(
            "Actionable feedback for this criterion. "
            "Can be null when the answer is correct and no major flaw is found."
        ),
    )


def generate_grading_model(rubric_def: RubricDefinition) -> type[BaseModel]:
    """Generate a dynamic Pydantic model used as grading response schema."""
    if not rubric_def.criterion:
        msg = "Rubric definition has no criterion."
        raise ValueError(msg)

    response_fields: dict[str, tuple[type[BaseModel], Any]] = {}
    used_names: set[str] = set()

    for criterion in rubric_def.criterion:
        field_name = slugify_criterion_name(criterion.name)
        if field_name in used_names:
            msg = f"Duplicated criterion field name after slugify: {field_name}"
            raise ValueError(msg)
        used_names.add(field_name)

        rating_enum = RATING_ENUM_MAP[criterion.rating]
        result_model = create_model(
            f"CriterionResult_{field_name}",
            __base__=CriterionResult,
            rating=(rating_enum, Field(..., description=criterion.desc)),
        )
        response_fields[field_name] = (
            result_model,
            Field(..., description=f"Result for criterion: {criterion.name}"),
        )

    return create_model(
        "GradingResponse",
        **response_fields,
    )


if __name__ == "__main__":
    import json
    from pathlib import Path

    project_root = Path(__file__).resolve().parents[1]
    schema_file = project_root / "config" / "rubric.schema.json"
    with schema_file.open("w") as f:
        json.dump(RubricDefinition.model_json_schema(), f, indent=4)

    print(get_rubric_definition(project_root / "rubrics" / "example_rubric.toml"))
