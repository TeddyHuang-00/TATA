from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from src.shared.grading import _build_client, _build_grading_messages


class TestBuildGradingMessages:
    def test_includes_system_prompt(self) -> None:
        messages = _build_grading_messages(
            system_prompt="You are a TA.",
            reference_text="ref",
            student_text="stu",
        )
        assert messages[0] == {"role": "system", "content": "You are a TA."}

    def test_includes_reference_when_provided(self) -> None:
        messages = _build_grading_messages(
            system_prompt="TA",
            reference_text="ref answer",
            student_text="stu answer",
        )
        assert len(messages) == 3
        assert messages[1]["role"] == "user"
        assert "Reference Answer" in messages[1]["content"]
        assert "ref answer" in messages[1]["content"]

    def test_skips_reference_when_empty(self) -> None:
        messages = _build_grading_messages(
            system_prompt="TA",
            reference_text="",
            student_text="stu answer",
        )
        assert len(messages) == 2

    def test_student_answer_is_last(self) -> None:
        messages = _build_grading_messages(
            system_prompt="TA",
            reference_text="ref",
            student_text="stu",
        )
        assert messages[-1]["role"] == "user"
        assert "Student Answer" in messages[-1]["content"]


class TestBuildClient:
    def test_passes_temperature_when_set(self) -> None:
        mock_provider = MagicMock()
        mock_provider.base_url = "http://test"
        mock_provider.api_key = "sk-test"
        mock_provider.model = "test-model"
        mock_provider.mode = "TOOLS"
        mock_provider.temperature = 0.0

        mock_instance = MagicMock()
        mock_instructor = MagicMock(return_value=mock_instance)

        with (
            patch("src.shared.grading.get_providers") as mock_get,
            patch("src.shared.grading.OpenAI") as mock_openai,
            patch("src.shared.grading.instructor.from_openai", mock_instructor),
        ):
            mock_get.return_value = {"test": mock_provider}

            _build_client("test")

            call_kwargs = mock_openai.call_args.kwargs
            assert call_kwargs["temperature"] == pytest.approx(0.0)

    def test_omits_temperature_when_none(self) -> None:
        mock_provider = MagicMock()
        mock_provider.base_url = "http://test"
        mock_provider.api_key = "sk-test"
        mock_provider.model = "test-model"
        mock_provider.mode = "TOOLS"
        mock_provider.temperature = None

        mock_instance = MagicMock()
        mock_instructor = MagicMock(return_value=mock_instance)

        with (
            patch("src.shared.grading.get_providers") as mock_get,
            patch("src.shared.grading.OpenAI") as mock_openai,
            patch("src.shared.grading.instructor.from_openai", mock_instructor),
        ):
            mock_get.return_value = {"test": mock_provider}

            _build_client("test")

            call_kwargs = mock_openai.call_args.kwargs
            assert "temperature" not in call_kwargs
