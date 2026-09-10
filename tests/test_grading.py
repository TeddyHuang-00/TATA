from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from instructor import Mode
from src.shared import grading as grading_mod
from src.shared.assignment_config import load_assignment_file
from src.shared.caching import cache_file, save_cache_file
from src.shared.grading import (
    _GRADE_HOOK_MOUNTS,
    _build_grading_messages,
    build_client,
    grade_assignment,
    grading_pending,
    load_assignment_config,
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

    a_dir = tmp_path / "data" / "c1" / "a1"
    envelope = json.loads(cache_file(a_dir, "grading").read_text(encoding="utf-8"))
    assert set(envelope) == {"fmt", "data"}
    assert envelope["fmt"] == 1
    assert set(envelope["data"]["100001"]) == {"hash"}
    assert isinstance(envelope["data"]["100001"]["hash"], str)

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
    # force bypasses the decision but never deletes the cache file.
    assert cache_file(tmp_path / "data" / "c1" / "a1", "grading").is_file()


def test_broken_or_foreign_envelope_cache_regrades_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tolerance: corrupt JSON / wrong fmt envelope -> {} -> full regrade."""
    config_path = _setup_grade_env(tmp_path)
    calls: list[MagicMock] = []
    _patch_grade_deps(monkeypatch, calls)
    cache_path = cache_file(tmp_path / "data" / "c1" / "a1", "grading")

    grade_assignment(config_path)  # the cache is written
    cache_path.write_text("{not json", encoding="utf-8")
    grade_assignment(config_path)
    assert len(calls) == 2

    cache_path.write_text(json.dumps({"fmt": 999, "data": {}}), encoding="utf-8")
    grade_assignment(config_path)
    assert len(calls) == 3


def test_grade_writes_no_checkpoint_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The zombie checkpoint is gone: a successful run leaves logs/ empty
    and writes the cache to .cache/grading.json only."""
    config_path = _setup_grade_env(tmp_path)
    calls: list[MagicMock] = []
    _patch_grade_deps(monkeypatch, calls)
    a_dir = tmp_path / "data" / "c1" / "a1"

    grade_assignment(config_path)

    # No checkpoint and no legacy logs/ cache file (logs/ holds nothing
    # after a clean run: the error log only appears on failures).
    assert not list(a_dir.rglob("grading.checkpoint.json"))
    assert not list((a_dir / "logs").iterdir())
    assert cache_file(a_dir, "grading").is_file()


def test_pending_follows_hash_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item: "needs rerun" count must come from the grading hash cache.

    Display and run share the one rule (src.shared.grading.grading_pending):
    after a processed md changes, both say the submission regrades. Regression:
    the old display counted pending from grading.checkpoint.json (a done list
    that never shrinks), so it under-reported 0 to rerun while the run queued
    the submission by the hash cache.
    """
    from src.tui.scan import AssignmentInfo, Counts
    from src.tui.workspace import AssignmentScreen, _incremental_line, state_key

    config_path = _setup_grade_env(tmp_path)
    calls: list[MagicMock] = []
    _patch_grade_deps(monkeypatch, calls)
    a_dir = tmp_path / "data" / "c1" / "a1"

    grade_assignment(config_path)
    # No zombie checkpoint is produced by a run (single state source).
    assert not (a_dir / "logs" / "grading.checkpoint.json").exists()

    (a_dir / "processed" / "100001.md").write_text(
        "# changed answer\n", encoding="utf-8"
    )

    # The cache rule the run applies says this submission regrades.
    pending = pending_grade_submissions(config_path)
    assert [p.stem for p in pending] == ["100001"]

    # The incremental display agrees with the run: grade 1, not grade 0.
    info = AssignmentInfo(
        dir_name="a1",
        config_path=config_path,
        counts=Counts(raw=1, processed=1, graded=1, scored=0),
    )
    assert "grade 1" in _incremental_line(info)

    # The state badge must agree too: counts are full but the cache says
    # regrade -> Partial (with raw actually fetched and pre cached-valid).
    (a_dir / "raw").mkdir(exist_ok=True)
    save_cache_file(cache_file(a_dir, "fetch"), {})  # raw actually fetched
    (a_dir / "scored").mkdir(exist_ok=True)
    (a_dir / "scored" / "100001.txt").write_text(
        "Total Score: 90/100", encoding="utf-8"
    )
    full = AssignmentInfo(
        dir_name="a1",
        config_path=config_path,
        counts=Counts(raw=1, processed=1, graded=1, scored=1),
    )
    assert state_key(full) == "partial"

    # The grade progress bar polls the cache rule: while the cached hash is
    # stale it says 0 done, and goes back to 1 once the regrade lands.
    ws = AssignmentScreen.__new__(AssignmentScreen)
    ws._info = full
    assert ws._stage_done("grade") == 0
    grade_assignment(config_path)
    assert ws._stage_done("grade") == 1
    assert state_key(full) == "done"

    # The scan rides the same rule onto AssignmentInfo for the per-row badge.
    restored = a_dir / "processed" / "100001.md"
    restored.write_text("# student answer\n", encoding="utf-8")
    grade_assignment(config_path)  # cache now matches the restored content
    # A real raw file + a preprocess pass so the scan sees a full pipeline.
    (a_dir / "raw" / "100001.md").write_text("# student answer\n", encoding="utf-8")
    from src.shared.processing import preprocess_assignment

    preprocess_assignment(config_path)
    from src.tui.scan import scan_assignments

    scanned = scan_assignments(tmp_path / "data" / "c1")
    assert scanned[0].grade_pending == 0
    assert scanned[0].pre_pending == 0
    assert state_key(scanned[0]) == "done"


# --- B3b: grading hash completeness (screenshots + hooks, digest-based) -----

_HOOK_NOOP = (
    "import json, sys\n"
    "payload = json.loads(sys.stdin.read() or '{}')\n"
    "print(json.dumps(payload))\n"
)


def _seed_valid_grade_cache(config_path: Path) -> None:
    """Seed a fully-valid grading cache: graded JSON per submission plus hashes
    computed by the rule itself (never hardcoded — Pitfall 18)."""
    a_dir = config_path.parent
    cfg = load_assignment_config(config_path)
    cfg_model = load_assignment_file(config_path)
    _, hashes = grading_pending(cfg, cfg_model)
    for stem in hashes:
        (a_dir / "graded" / f"{stem}.json").write_text("{}", encoding="utf-8")
    save_cache_file(
        cache_file(a_dir, "grading"), {s: {"hash": h} for s, h in hashes.items()}
    )


def _add_grade_hook_mount(config_path: Path, script_name: str) -> None:
    """Append a before_grade_submission mount to the assignment config."""
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + f'[hooks.mounts]\nbefore_grade_submission = "{script_name}"\n',
        encoding="utf-8",
    )


def test_screenshot_rerender_invalidates_when_visual_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3b: md unchanged, only the screenshots change -> regrade (visual on)."""
    config_path = _setup_grade_env(tmp_path, visual_evaluation=True)
    _patch_grade_deps(monkeypatch, [])
    shots = config_path.parent / "processed" / "screenshots"
    shots.mkdir(parents=True)
    (shots / "100001_p1.png").write_bytes(b"page")
    (shots / "100001_i0.png").write_bytes(b"cell")
    _seed_valid_grade_cache(config_path)
    assert pending_grade_submissions(config_path) == []

    (shots / "100001_i0.png").write_bytes(b"cell-v2")  # notebook image changed
    assert [p.stem for p in pending_grade_submissions(config_path)] == ["100001"]

    _seed_valid_grade_cache(config_path)  # accept the new image state
    (shots / "100001_p1.png").write_bytes(b"page-v2")  # page render changed
    assert [p.stem for p in pending_grade_submissions(config_path)] == ["100001"]


def test_screenshot_change_ignored_when_visual_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3b: with visual_evaluation off, screenshots never enter the hash."""
    config_path = _setup_grade_env(tmp_path)
    _patch_grade_deps(monkeypatch, [])
    shots = config_path.parent / "processed" / "screenshots"
    shots.mkdir(parents=True)
    (shots / "100001_p1.png").write_bytes(b"page")
    _seed_valid_grade_cache(config_path)
    assert pending_grade_submissions(config_path) == []

    (shots / "100001_p1.png").write_bytes(b"page-v2")
    (shots / "100001_i0.png").write_bytes(b"new-image")
    assert pending_grade_submissions(config_path) == []


def test_hook_config_change_invalidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3b: editing a grade-stage [hooks.mounts] entry -> regrade."""
    config_path = _setup_grade_env(tmp_path)
    _patch_grade_deps(monkeypatch, [])
    hooks = tmp_path / "data" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "h1.py").write_text(_HOOK_NOOP, encoding="utf-8")
    (hooks / "h2.py").write_text(_HOOK_NOOP, encoding="utf-8")

    _add_grade_hook_mount(config_path, "h1.py")
    _seed_valid_grade_cache(config_path)
    assert pending_grade_submissions(config_path) == []

    # Same bytes, different script wired at the mount point.
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace('"h1.py"', '"h2.py"'),
        encoding="utf-8",
    )
    assert [p.stem for p in pending_grade_submissions(config_path)] == ["100001"]


def test_hook_script_byte_change_invalidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3b: same config, edited hook script bytes -> regrade."""
    config_path = _setup_grade_env(tmp_path)
    _patch_grade_deps(monkeypatch, [])
    script = tmp_path / "data" / "hooks" / "h1.py"
    script.parent.mkdir(parents=True)
    script.write_text(_HOOK_NOOP, encoding="utf-8")
    _add_grade_hook_mount(config_path, "h1.py")
    _seed_valid_grade_cache(config_path)
    assert pending_grade_submissions(config_path) == []

    script.write_text(_HOOK_NOOP + "\n# tweak\n", encoding="utf-8")
    assert [p.stem for p in pending_grade_submissions(config_path)] == ["100001"]


def test_grade_hook_mounts_list_covers_every_invoked_mount() -> None:
    """Invariant: every mount grading.py invokes is in _GRADE_HOOK_MOUNTS — a
    fifth ``hook_runtime.run("<mount>")`` call added without updating the
    static list would silently drop that hook's config/script bytes from the
    grading hash (cache never invalidates on the edit it should track).

    Textual scan on purpose (the call shape is fixed); the non-empty guard
    makes a rename of ``hook_runtime`` fail loudly instead of passing
    vacuously. Update this scan together with the rename.
    """
    source = Path(grading_mod.__file__).read_text(encoding="utf-8")
    invoked = set(re.findall(r'hook_runtime\.run\(\s*"([a-z_]+)"', source))
    assert invoked, "scan found no hook_runtime.run() calls — pattern drifted"
    assert invoked <= set(_GRADE_HOOK_MOUNTS)


def test_grading_hash_stable_across_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3b: same inputs -> identical sub_hashes on repeat (memo hits included)."""
    config_path = _setup_grade_env(tmp_path, visual_evaluation=True)
    _patch_grade_deps(monkeypatch, [])
    shots = config_path.parent / "processed" / "screenshots"
    shots.mkdir(parents=True)
    (shots / "100001_p1.png").write_bytes(b"page")
    cfg = load_assignment_config(config_path)
    cfg_model = load_assignment_file(config_path)

    _, first = grading_pending(cfg, cfg_model)
    _, second = grading_pending(cfg, cfg_model)

    assert set(first) == {"100001"}
    assert first == second


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
            patch("src.shared.provider.OpenAI") as mock_openai,
            patch("src.shared.provider.instructor.from_openai", mock_instructor),
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
            patch("src.shared.provider.OpenAI") as mock_openai,
            patch("src.shared.provider.instructor.from_openai", mock_instructor),
        ):
            mock_get.return_value = {"test": mock_provider}

            build_client("test")

            call_kwargs = mock_openai.call_args.kwargs
            assert "temperature" not in call_kwargs
