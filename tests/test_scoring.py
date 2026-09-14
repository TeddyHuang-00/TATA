"""Regression tests for criterion score calculation.

See plans/2026-09-14-code-audit.md: the custom scheme's label map was looked
up with the grading scheme ("custom") as key, so it always missed and likert
ratings fell through to a fallback that returned the scale maximum.
"""

from __future__ import annotations

import pytest
from src.shared.rubric import Criterion, Grading, Rating
from src.shared.scoring import calculate_criterion_score


class TestCustomScaleMapping:
    def test_likert_ratings_map_in_order(self) -> None:
        scale = [0.0, 2.0, 4.0, 6.0, 8.0]
        expected = {
            "completely incorrect": 0.0,
            "somewhat incorrect": 2.0,
            "neutral": 4.0,
            "somewhat correct": 6.0,
            "completely correct": 8.0,
        }
        for rating, want in expected.items():
            assert (
                calculate_criterion_score(8, rating, "custom", scale, "likert") == want
            ), rating

    def test_ternary_ratings_map_in_order(self) -> None:
        scale = [0.0, 3.0, 6.0]
        assert calculate_criterion_score(
            6, "incorrect", "custom", scale, "ternary"
        ) == pytest.approx(0.0)
        assert calculate_criterion_score(
            6, "partial", "custom", scale, "ternary"
        ) == pytest.approx(3.0)
        assert calculate_criterion_score(
            6, "correct", "custom", scale, "ternary"
        ) == pytest.approx(6.0)

    def test_binary_penalty_criterion(self) -> None:
        # Shape of the bundled penalty rubric: pts = 0, scale [-2, 0].
        assert calculate_criterion_score(
            0, "correct", "custom", [-2.0, 0.0], "binary"
        ) == pytest.approx(0.0)
        assert calculate_criterion_score(
            0, "incorrect", "custom", [-2.0, 0.0], "binary"
        ) == pytest.approx(-2.0)

    def test_rating_lookup_is_case_insensitive(self) -> None:
        assert calculate_criterion_score(
            8, "NEUTRAL", "custom", [0.0, 2.0, 4.0, 6.0, 8.0], "likert"
        ) == pytest.approx(4.0)

    def test_unknown_rating_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown rating"):
            calculate_criterion_score(8, "maybe", "custom", [0.0, 1.0, 2.0], "ternary")

    def test_custom_without_rating_scale_raises(self) -> None:
        # Without the criterion's rating scale the rating cannot be mapped:
        # fail loudly instead of fabricating a score.
        with pytest.raises(ValueError, match="Unknown rating"):
            calculate_criterion_score(8, "neutral", "custom", [0.0, 1.0, 2.0, 3.0, 4.0])


class TestRoundUp:
    def test_partial_rounds_up(self) -> None:
        # Documented example: ternary partial = 0.5 of pts, rounded up to 1.
        assert calculate_criterion_score(1, "partial", "round up") == pytest.approx(1.0)

    def test_exact_integer_not_lifted(self) -> None:
        assert calculate_criterion_score(4, "correct", "round up") == pytest.approx(4.0)

    def test_zero_stays_zero(self) -> None:
        assert calculate_criterion_score(2, "incorrect", "round up") == pytest.approx(
            0.0
        )


class TestStandardSchemes:
    def test_likert_standard_fractions(self) -> None:
        assert calculate_criterion_score(
            8, "completely correct", "standard"
        ) == pytest.approx(8.0)
        assert calculate_criterion_score(
            8, "somewhat correct", "standard"
        ) == pytest.approx(6.0)
        assert calculate_criterion_score(8, "neutral", "standard") == pytest.approx(2.0)
        assert calculate_criterion_score(
            8, "somewhat incorrect", "standard"
        ) == pytest.approx(0.0)
        assert calculate_criterion_score(
            8, "completely incorrect", "standard"
        ) == pytest.approx(0.0)

    def test_none_scheme_is_standard(self) -> None:
        assert calculate_criterion_score(4, "partial", None) == pytest.approx(2.0)


class TestCriterionValidation:
    def test_negative_pts_rejected(self) -> None:
        with pytest.raises(ValueError, match="greater than or equal to 0"):
            Criterion(name="X", desc="d", rating=Rating.TERNARY, pts=-1)

    def test_custom_scale_above_pts_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not exceed pts"):
            Criterion(
                name="X",
                desc="d",
                rating=Rating.TERNARY,
                grading=Grading.CUSTOM,
                custom_scale=[0.0, 3.0, 6.0],
                pts=5,
            )

    def test_penalty_scale_below_zero_allowed(self) -> None:
        c = Criterion(
            name="Penalty",
            desc="d",
            rating=Rating.BINARY,
            grading=Grading.CUSTOM,
            custom_scale=[-2.0, 0.0],
            pts=0,
        )
        assert c.pts == 0

    def test_grading_defaults_to_none(self) -> None:
        c = Criterion(name="X", desc="d", rating=Rating.BINARY, pts=1)
        assert c.grading is None
