from __future__ import annotations

import pytest
from src.rubric import (
    Binary,
    Criterion,
    CriterionResult,
    Grading,
    Likert,
    Rating,
    RubricDefinition,
    Ternary,
    generate_grading_model,
    slugify_criterion_name,
)


class TestBinaryEnum:
    def test_has_two_members(self) -> None:
        assert len(Binary) == 2

    def test_member_values(self) -> None:
        assert Binary.CORRECT.value == "correct"
        assert Binary.INCORRECT.value == "incorrect"

    def test_class_docstring_describes_binary_scale(self) -> None:
        doc = Binary.__doc__ or ""
        assert "binary" in doc.lower()


class TestTernaryEnum:
    def test_has_three_members(self) -> None:
        assert len(Ternary) == 3

    def test_member_values(self) -> None:
        assert Ternary.CORRECT.value == "correct"
        assert Ternary.PARTIAL.value == "partial"
        assert Ternary.INCORRECT.value == "incorrect"

    def test_class_docstring_describes_three_point_scale(self) -> None:
        doc = Ternary.__doc__ or ""
        assert "three" in doc.lower() or "3" in doc


class TestLikertEnum:
    def test_five_members(self) -> None:
        assert len(Likert) == 5

    def test_member_values(self) -> None:
        assert Likert.COMPLETELY_CORRECT.value == "completely correct"
        assert Likert.SOMEWHAT_CORRECT.value == "somewhat correct"
        assert Likert.NEUTRAL.value == "neutral"
        assert Likert.SOMEWHAT_INCORRECT.value == "somewhat incorrect"
        assert Likert.COMPLETELY_INCORRECT.value == "completely incorrect"

    def test_class_docstring_describes_likert_scale(self) -> None:
        doc = Likert.__doc__ or ""
        assert "likert" in doc.lower() or "five" in doc.lower()


class TestCriterionResult:
    def test_chain_of_thought_has_template(self) -> None:
        fields = CriterionResult.model_fields
        cot_field = fields["chain_of_thought"]
        desc = cot_field.description or ""
        assert "DIFFERENCES" in desc
        assert "FUNCTIONAL" in desc
        assert "COSMETIC" in desc
        assert "MISSING" in desc
        assert "RATING" in desc


class TestGenerateGradingModel:
    def test_generates_binary_model(self) -> None:
        rubric = RubricDefinition(
            criterion=[
                Criterion(
                    name="Test Criterion",
                    desc="Test description",
                    rating=Rating.BINARY,
                    grading=None,
                    pts=10,
                )
            ]
        )
        model = generate_grading_model(rubric)
        assert model.__name__ == "GradingResponse"
        field_name = slugify_criterion_name("Test Criterion")
        assert field_name in model.model_fields

    def test_generates_ternary_model(self) -> None:
        rubric = RubricDefinition(
            criterion=[
                Criterion(
                    name="Code Quality",
                    desc="Evaluate code quality",
                    rating=Rating.TERNARY,
                    grading=None,
                    pts=20,
                )
            ]
        )
        model = generate_grading_model(rubric)
        assert "code_quality" in model.model_fields

    def test_generates_likert_model(self) -> None:
        rubric = RubricDefinition(
            criterion=[
                Criterion(
                    name="Analysis",
                    desc="Evaluate analysis",
                    rating=Rating.LIKERT,
                    grading=None,
                    pts=30,
                )
            ]
        )
        model = generate_grading_model(rubric)
        assert "analysis" in model.model_fields

    def test_raises_on_empty_rubric(self) -> None:
        rubric = RubricDefinition(criterion=[])
        with pytest.raises(ValueError, match="no criterion"):
            generate_grading_model(rubric)

    def test_generates_custom_grading_model(self) -> None:
        rubric = RubricDefinition(
            criterion=[
                Criterion(
                    name="Custom Criterion",
                    desc="Custom test",
                    rating=Rating.TERNARY,
                    grading=Grading.CUSTOM,
                    custom_scale=[0.0, 0.3, 1.0],
                    pts=15,
                )
            ]
        )
        model = generate_grading_model(rubric)
        assert "custom_criterion" in model.model_fields

    def test_generates_multiple_criteria(self) -> None:
        rubric = RubricDefinition(
            criterion=[
                Criterion(
                    name="Part A",
                    desc="First part",
                    rating=Rating.BINARY,
                    grading=None,
                    pts=5,
                ),
                Criterion(
                    name="Part B",
                    desc="Second part",
                    rating=Rating.TERNARY,
                    grading=None,
                    pts=10,
                ),
                Criterion(
                    name="Part C",
                    desc="Third part",
                    rating=Rating.LIKERT,
                    grading=None,
                    pts=15,
                ),
            ]
        )
        model = generate_grading_model(rubric)
        assert "part_a" in model.model_fields
        assert "part_b" in model.model_fields
        assert "part_c" in model.model_fields


class TestSlugifyCriterionName:
    def test_simple_name(self) -> None:
        assert slugify_criterion_name("Code Quality") == "code_quality"

    def test_special_characters(self) -> None:
        assert slugify_criterion_name("Part 1: Setup") == "part_1_setup"

    def test_leading_digit(self) -> None:
        assert slugify_criterion_name("1. Introduction") == "c_1_introduction"

    def test_raises_on_empty_result(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            slugify_criterion_name("!!!")
