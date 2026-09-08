from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from instructor import Mode
from src.shared.grading import (
    _build_grading_messages,
    build_client,
    grade_assignment,
    pending_grade_submissions,
)
from src.shared.provider import ProviderInfo, ProviderList


def _setup_grade_env(tmp_path: Path, *, visual_evaluation: bool = False) -> Path:
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
    config = (
        '[grading]\nrubric = "rubrics/r.toml"\n'
        'system_prompt = ["prompt/system.md"]\nprovider = "test"\n'
    )
    if visual_evaluation:
        config += "[processing]\nvisual_evaluation = true\n"
    (a_dir / "config.toml").write_text(config, encoding="utf-8")
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
    monkeypatch.setattr("src.shared.grading.build_client", lambda name: (client, "m1"))
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


def test_pending_follows_hash_cache_not_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item: "needs rerun" count must come from the grading hash cache.

    Regression: the workspace display counted pending from
    grading.checkpoint.json (a done list that never shrinks), while the run
    queued tasks by the hash cache. After a processed md changes, the cache
    says "regrade" (run queues it) although the checkpoint still says all
    done — the display then under-reported 0 to rerun.
    """
    from src.tui.scan import AssignmentInfo, Counts
    from src.tui.workspace import _incremental_line

    config_path = _setup_grade_env(tmp_path)
    calls: list[MagicMock] = []
    _patch_grade_deps(monkeypatch, calls)

    grade_assignment(config_path)
    a_dir = tmp_path / "data" / "c1" / "a1"
    checkpoint = json.loads(
        (a_dir / "logs" / "grading.checkpoint.json").read_text(encoding="utf-8")
    )
    assert checkpoint["done"] == ["100001.md"]

    (a_dir / "processed" / "100001.md").write_text(
        "# changed answer\n", encoding="utf-8"
    )

    # Checkpoint (the old display source) still says "all done"...
    assert len(checkpoint["done"]) == 1
    # ...but the cache rule the run applies says this submission regrades.
    pending = pending_grade_submissions(config_path)
    assert [p.stem for p in pending] == ["100001"]

    # The incremental display agrees with the run: grade 1, not grade 0.
    info = AssignmentInfo(
        dir_name="a1",
        config_path=config_path,
        counts=Counts(raw=1, processed=1, graded=1, scored=0),
    )
    assert "grade 1" in _incremental_line(info)


def _image_payloads(content: list[dict]) -> list[bytes]:
    """Decoded bytes of every image_url part, in message order."""
    return [
        base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
        for part in content
        if part.get("type") == "image_url"
    ]


class TestGradingVisualEvaluation:
    def test_mixed_p_and_i_ordered_pages_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Item: _images_for consumes both _p* and _i*; pages first, then extracted."""
        config_path = _setup_grade_env(tmp_path, visual_evaluation=True)
        calls: list[MagicMock] = []
        _patch_grade_deps(monkeypatch, calls)
        shots = tmp_path / "data" / "c1" / "a1" / "processed" / "screenshots"
        shots.mkdir()
        (shots / "100001_p2.png").write_bytes(b"page2")
        (shots / "100001_p1.png").write_bytes(b"page1")
        (shots / "100001_i0.png").write_bytes(b"img0")

        grade_assignment(config_path)

        content = calls[0]["messages"][-1]["content"]
        assert isinstance(content, list)
        assert _image_payloads(content) == [b"page1", b"page2", b"img0"]

    def test_only_i_images(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Item: ipynb-only student (only _i*) still gets its extracted images."""
        config_path = _setup_grade_env(tmp_path, visual_evaluation=True)
        calls: list[MagicMock] = []
        _patch_grade_deps(monkeypatch, calls)
        shots = tmp_path / "data" / "c1" / "a1" / "processed" / "screenshots"
        shots.mkdir()
        (shots / "100001_i0.png").write_bytes(b"notebook-img")

        grade_assignment(config_path)

        content = calls[0]["messages"][-1]["content"]
        assert isinstance(content, list)
        assert _image_payloads(content) == [b"notebook-img"]

    def test_visual_enabled_but_no_matching_files_falls_back_to_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Item: empty glob -> [] -> plain text content (no list/dict)."""
        config_path = _setup_grade_env(tmp_path, visual_evaluation=True)
        calls: list[MagicMock] = []
        _patch_grade_deps(monkeypatch, calls)
        (tmp_path / "data" / "c1" / "a1" / "processed" / "screenshots").mkdir()

        grade_assignment(config_path)

        content = calls[0]["messages"][-1]["content"]
        assert isinstance(content, str)
        assert "Student Answer" in content

    def test_visual_disabled_plain_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Item: visual_evaluation off -> no image parts at all."""
        config_path = _setup_grade_env(tmp_path)
        calls: list[MagicMock] = []
        _patch_grade_deps(monkeypatch, calls)

        grade_assignment(config_path)

        content = calls[0]["messages"][-1]["content"]
        assert isinstance(content, str)
        assert "Student Answer" in content


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

    def test_images_become_image_url_parts(self) -> None:
        messages = _build_grading_messages(
            system_prompt="TA",
            reference_text="",
            student_text="stu",
            images=["aGVsbG8=", "d29ybGQ="],
        )
        content = messages[-1]["content"]
        assert isinstance(content, list)
        assert content[0]["type"] == "text"
        assert "Student Answer" in content[0]["text"]
        image_parts = [p for p in content if p["type"] == "image_url"]
        assert len(image_parts) == 2
        assert all(
            p["image_url"]["url"].startswith("data:image/png;base64,")
            for p in image_parts
        )
        assert image_parts[0]["image_url"]["url"] == "data:image/png;base64,aGVsbG8="
        assert image_parts[1]["image_url"]["url"] == "data:image/png;base64,d29ybGQ="

    def test_no_images_plain_text_content(self) -> None:
        messages = _build_grading_messages(
            system_prompt="TA",
            reference_text="",
            student_text="stu",
            images=None,
        )
        assert isinstance(messages[-1]["content"], str)


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

            build_client("test")

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

            build_client("test")

            call_kwargs = mock_openai.call_args.kwargs
            assert "temperature" not in call_kwargs
