from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from instructor import Mode
from src.shared.grading import (
    _build_client,
    _build_grading_messages,
    grade_assignment,
)
from src.shared.provider import ProviderInfo, ProviderList


def _setup_grade_env(tmp_path: Path) -> Path:
    """Course/assignment layout so grading config paths resolve like real data."""
    a_dir = tmp_path / "data" / "c1" / "a1"
    (a_dir / "processed").mkdir(parents=True)
    (a_dir / "processed" / "100001.md").write_text(
        "# student answer\n", encoding="utf-8"
    )
    (tmp_path / "data" / "rubrics").mkdir(parents=True)
    (tmp_path / "data" / "rubrics" / "r.toml").write_text(
        '[[criterion]]\nname = "C1"\ndesc = "d"\npts = 10\nrating = "binary"\n'
        'grading = "standard"\n',
        encoding="utf-8",
    )
    (tmp_path / "data" / "prompt").mkdir(parents=True)
    (tmp_path / "data" / "prompt" / "system.md").write_text(
        "You are a TA.\n", encoding="utf-8"
    )
    (a_dir / "config.toml").write_text(
        '[grading]\nrubric = "rubrics/r.toml"\n'
        'system_prompt = ["prompt/system.md"]\nprovider = "test"\n',
        encoding="utf-8",
    )
    return a_dir / "config.toml"


def _fake_client(calls: list) -> MagicMock:
    result = MagicMock()
    result.model_dump_json.return_value = json.dumps({"C1": {"rating": "correct"}})

    def create(**kwargs: object) -> MagicMock:
        calls.append(kwargs)
        return result

    client = MagicMock()
    client.chat.completions.create.side_effect = create
    return client


def _patch_grade_deps(monkeypatch: pytest.MonkeyPatch, calls: list[MagicMock]) -> None:
    client = _fake_client(calls)
    monkeypatch.setattr("src.shared.grading._build_client", lambda name: (client, "m1"))
    monkeypatch.setattr(
        "src.shared.grading.get_providers",
        lambda: ProviderList(
            providers={
                "test": ProviderInfo(
                    base_url="http://test",
                    api_key="sk-test",
                    model="m1",
                    mode=Mode.TOOLS,
                    temperature=0.0,
                )
            }
        ),
    )


def test_grade_cache_skips_second_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 2: same hashes -> second run makes no LLM call; cache written."""
    config_path = _setup_grade_env(tmp_path)
    calls: list[MagicMock] = []
    _patch_grade_deps(monkeypatch, calls)

    result = grade_assignment(config_path)
    assert result is not None
    assert result["success"] == 1
    assert len(calls) == 1

    cache_file = tmp_path / "data" / "c1" / "a1" / "logs" / "grading.cache.json"
    cache = json.loads(cache_file.read_text(encoding="utf-8"))
    assert "100001" in cache
    assert cache["100001"]["fmt"] == 1
    assert isinstance(cache["100001"]["hash"], str)

    result2 = grade_assignment(config_path)
    assert result2 is not None
    assert result2["total"] == 0
    assert len(calls) == 1  # no LLM call on cache hit


def test_grade_cache_regrades_on_input_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 2: changed processed md -> hash mismatch -> regrade."""
    config_path = _setup_grade_env(tmp_path)
    calls: list[MagicMock] = []
    _patch_grade_deps(monkeypatch, calls)

    grade_assignment(config_path)
    (tmp_path / "data" / "c1" / "a1" / "processed" / "100001.md").write_text(
        "# changed answer\n", encoding="utf-8"
    )
    grade_assignment(config_path)

    assert len(calls) == 2


def test_grade_force_reqrades_despite_valid_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 2: force=True ignores the cache and regrades all submissions."""
    config_path = _setup_grade_env(tmp_path)
    calls: list[MagicMock] = []
    _patch_grade_deps(monkeypatch, calls)

    grade_assignment(config_path)
    grade_assignment(config_path, force=True)

    assert len(calls) == 2


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
