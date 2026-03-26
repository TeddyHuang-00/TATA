from __future__ import annotations

import re
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import BaseModel, Field, create_model, model_validator


class Binary(StrEnum):
    """Represents the correctness level on a binary scale."""

    CORRECT = "correct"
    """The answer is identical or adequately close to the reference answer."""
    INCORRECT = "incorrect"
    """The answer is giving completely wrong answer or does not have answer/code at all."""


class Ternary(StrEnum):
    """Represents the correctness level on a three-point scale."""

    CORRECT = "correct"
    """The answer is identical or adequately close to the reference answer."""
    PARTIAL = "partial"
    """The answer is partially correct but misses some important details. Such as wrong order of steps or missing minor steps."""
    INCORRECT = "incorrect"
    """The answer is giving completely wrong answer or does not have answer/code at all."""


class Likert(StrEnum):
    """Represents the correctness level on a five-point Likert scale."""

    COMPLETELY_INCORRECT = "completely incorrect"
    """The answer is giving completely wrong answer or does not have answer/code at all."""
    SOMEWHAT_INCORRECT = "somewhat incorrect"
    """The answer has some correct elements but also contains significant errors or omissions."""
    NEUTRAL = "neutral"
    """The answer is neither correct nor incorrect, or it is unclear and cannot be evaluated."""
    SOMEWHAT_CORRECT = "somewhat correct"
    """The answer is mostly correct but may have minor errors or omissions."""
    COMPLETELY_CORRECT = "completely correct"
    """The answer is identical or adequately close to the reference answer."""


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
        description="The chain of thought that led to the rating.",
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

    response_fields: dict[str, tuple[type[BaseModel], Field]] = {}
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

    schema_file = Path(__file__).parent / "config" / "rubric.schema.json"
    with schema_file.open("w") as f:
        json.dump(RubricDefinition.model_json_schema(), f, indent=4)

    print(
        get_rubric_definition(Path(__file__).parent / "rubrics" / "example_rubric.toml")
    )
