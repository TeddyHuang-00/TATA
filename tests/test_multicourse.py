from __future__ import annotations

import tomllib
from pathlib import Path
from unittest import mock

import pytest
from main import _classify_config, _fetch_course, _load_config, _remember
from src.assignment_config import (
    FetchSection,
    find_global_config,
    is_course_config,
    is_global_config,
    is_root_config,
    load_assignment_file,
)
from src.canvas_fetch import remember_fetch

GRADING = '[grading]\nrubric = "rubrics/x.toml"\nsystem_prompt = "prompt/system.md"\nprovider = "ollama"\n'


def _write(tree: Path, rel: str, text: str) -> Path:
    p = tree / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _write_three_level(tmp_path: Path) -> tuple[Path, Path, Path]:
    """data/config.toml (global) + data/271218/config.toml
    (course) + data/271218/hw1/config.toml (assignment)."""
    global_cfg = _write(
        tmp_path,
        "data/config.toml",
        '[fetch]\ncourse_id = 271218\nmode = "attach"\n',
    )
    course_cfg = _write(
        tmp_path,
        "data/271218/config.toml",
        "[fetch]\ncourse_id = 271218\n",
    )
    assignment_cfg = _write(
        tmp_path,
        "data/271218/hw1/config.toml",
        GRADING + "[fetch]\nassignment_id = 42\n",
    )
    return global_cfg, course_cfg, assignment_cfg


def test_is_course_config_three_level(tmp_path: Path) -> None:
    global_cfg, course_cfg, assignment_cfg = _write_three_level(tmp_path)
    assert is_course_config(course_cfg)
    assert not is_course_config(global_cfg)
    assert not is_course_config(assignment_cfg)


def test_is_course_config_two_level_root(tmp_path: Path) -> None:
    _write(tmp_path, "data/config.toml", "[fetch]\ncourse_id = 1\n")
    _write(tmp_path, "data/a/config.toml", GRADING)
    root = tmp_path / "data" / "config.toml"
    assert is_course_config(root)
    assert not is_global_config(root)


def test_is_global_config_three_level(tmp_path: Path) -> None:
    global_cfg, course_cfg, assignment_cfg = _write_three_level(tmp_path)
    assert is_global_config(global_cfg)
    assert not is_global_config(course_cfg)
    assert not is_global_config(assignment_cfg)


def test_is_course_config_missing_file_is_false(tmp_path: Path) -> None:
    _write(tmp_path, "data/a/config.toml", GRADING)
    root = tmp_path / "data" / "config.toml"
    assert not is_course_config(root)
    assert not is_global_config(root)


def test_find_global_config(tmp_path: Path) -> None:
    global_cfg, course_cfg, _ = _write_three_level(tmp_path)
    assert find_global_config(course_cfg) == global_cfg
    assert find_global_config(global_cfg) is None


def test_three_layer_merge_precedence(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "data/config.toml",
        "[plagiarism]\ncopydetect_weight = 0.9\nembedding_weight = 0.1\n",
    )
    _write(
        tmp_path,
        "data/271218/config.toml",
        "[plagiarism]\ncopydetect_weight = 0.85\n",
    )
    assignment_cfg = _write(
        tmp_path,
        "data/271218/hw1/config.toml",
        GRADING,
    )
    cfg = load_assignment_file(assignment_cfg)
    # Course overrides global per key.
    assert cfg.plagiarism.copydetect_weight == pytest.approx(0.85)
    assert cfg.plagiarism.embedding_weight == pytest.approx(0.1)

    # Assignment wins over both layers.
    _write(
        tmp_path,
        "data/271218/hw1/config.toml",
        GRADING + "[plagiarism]\ncopydetect_weight = 0.95\n",
    )
    cfg = load_assignment_file(assignment_cfg)
    assert cfg.plagiarism.copydetect_weight == pytest.approx(0.95)


def test_three_layer_fetch_assignments_do_not_leak(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "data/config.toml",
        "[fetch]\ncourse_id = 271218\n"
        '\n[[fetch.assignments]]\nassignment_id = 99\nout = "other/raw"\n',
    )
    course_cfg = _write(
        tmp_path,
        "data/271218/config.toml",
        '[fetch]\ncourse_id = 271218\nmode = "attach"\n'
        '\n[[fetch.assignments]]\nassignment_id = 43\nout = "hw2/raw"\n',
    )
    assignment_cfg = _write(
        tmp_path,
        "data/271218/hw1/config.toml",
        GRADING + "[fetch]\nassignment_id = 42\n",
    )

    cfg = load_assignment_file(assignment_cfg)
    assert cfg.fetch is not None
    assert cfg.fetch.course_id == 271218
    assert cfg.fetch.assignment_id == 42
    assert cfg.fetch.mode == "attach"
    assert cfg.fetch.assignments == []

    # The course config list still parses for course-level orchestration.
    course_fetch = FetchSection.model_validate(
        tomllib.loads(course_cfg.read_text())["fetch"]
    )
    assert [(e.assignment_id, e.out) for e in course_fetch.assignments] == [
        (43, "hw2/raw")
    ]


def test_remember_container_writes_own_config(tmp_path: Path) -> None:
    """C1 regression: -c points at a course config; course-level keys must
    land in that config itself (never climb up to the global) and its
    [[fetch.assignments]] list must survive."""
    course_cfg = _write(
        tmp_path,
        "data/271218/config.toml",
        '[fetch]\ncourse_id = 271218\nmode = "attach"\n'
        '\n[[fetch.assignments]]\nassignment_id = 43\nout = "hw2/raw"\n',
    )
    _write(tmp_path, "data/271218/hw1/config.toml", GRADING)
    out = tmp_path / "data" / "271218" / "hw1" / "raw"
    out.mkdir(parents=True)

    _remember(out, course_cfg, 271218, 42, "attach")

    course_fetch = tomllib.loads(course_cfg.read_text())["fetch"]
    assert course_fetch["course_id"] == 271218
    assert course_fetch["mode"] == "attach"
    # The course's [[fetch.assignments]] list was not wiped by the write.
    assert [(e["assignment_id"], e["out"]) for e in course_fetch["assignments"]] == [
        (43, "hw2/raw")
    ]
    assignment_fetch = tomllib.loads((out.parent / "config.toml").read_text())["fetch"]
    assert assignment_fetch["assignment_id"] == 42


def test_remember_fetch_upsert_preserves_assignments(tmp_path: Path) -> None:
    """Upsert: only provided fields change; existing keys and the
    [[fetch.assignments]] list stay."""
    cfg = _write(
        tmp_path,
        "data/271218/config.toml",
        '[fetch]\ncourse_id = 271218\nmode = "attach"\n'
        '\n[[fetch.assignments]]\nassignment_id = 43\nout = "hw2/raw"\n',
    )

    remember_fetch(cfg, course_id=999)

    fetch = tomllib.loads(cfg.read_text())["fetch"]
    assert fetch["course_id"] == 999
    assert fetch["mode"] == "attach"  # untouched field preserved
    assert [(e["assignment_id"], e["out"]) for e in fetch["assignments"]] == [
        (43, "hw2/raw")
    ]


def test_nested_config_does_not_break_container_detection(tmp_path: Path) -> None:
    """M1 regression: a nested config.toml (data/271218/a/solutions/)
    defeats the leaf heuristics (is_course_config/is_global_config both
    return False), but the container check (is_root_config) must stay True so
    _load_config keeps treating the global/course configs as containers."""
    global_cfg = _write(
        tmp_path,
        "data/config.toml",
        "[fetch]\ncourse_id = 271218\n",
    )
    course_cfg = _write(
        tmp_path,
        "data/271218/config.toml",
        "[fetch]\ncourse_id = 271218\n",
    )
    assignment_cfg = _write(
        tmp_path,
        "data/271218/a/config.toml",
        GRADING + "[fetch]\nassignment_id = 42\n",
    )
    _write(tmp_path, "data/271218/a/solutions/config.toml", GRADING)

    assert is_root_config(global_cfg)
    assert is_root_config(course_cfg)

    # _load_config must classify both as containers (fetch-only state), never
    # fall through to load_assignment_file and fail on missing [grading].
    path, fetch = _load_config(global_cfg)
    assert path == global_cfg
    assert fetch is not None
    assert fetch.course_id == 271218
    path, fetch = _load_config(course_cfg)
    assert path == course_cfg
    assert fetch is not None
    assert fetch.course_id == 271218

    # The assignment (even with its nested config) still loads as an assignment.
    cfg = load_assignment_file(assignment_cfg)
    assert cfg.fetch is not None
    assert cfg.fetch.assignment_id == 42


def test_load_config_fresh_course_self_evidence(tmp_path: Path) -> None:
    """MAJOR-1: a freshly created course config — only [fetch] state +
    [[fetch.assignments]], no [grading], no subdirectory configs — defeats
    every structural heuristic and must not fall through to
    load_assignment_file (which would raise Missing required [grading]); it
    is a self-evident container because it holds a [fetch] table."""
    course_cfg = _write(
        tmp_path,
        "data/271218/config.toml",
        '[fetch]\ncourse_id = 271218\nmode = "attach"\n'
        '\n[[fetch.assignments]]\nassignment_id = 43\nout = "hw1/raw"\n',
    )
    for detect in (is_root_config, is_course_config, is_global_config):
        assert not detect(course_cfg)

    path, fetch = _load_config(str(course_cfg))  # must not raise
    assert path == course_cfg
    assert fetch is not None
    assert fetch.course_id == 271218
    assert [(e.assignment_id, e.out) for e in fetch.assignments] == [
        (43, "hw1/raw")
    ]


def test_remember_nested_assignment_no_container_pollution(tmp_path: Path) -> None:
    """M1 regression: data/271218/a/config.toml has a nested
    subdirectory config (a/solutions/config.toml), which made the
    structural container heuristics classify it as a container — _remember
    then wrote course_id/mode into the ASSIGNMENT config (pollution) and
    the course config never got mode. Pure self-evidence: it loads as an
    assignment, so course keys go to the course config."""
    course_cfg = _write(
        tmp_path,
        "data/271218/config.toml",
        "[fetch]\ncourse_id = 271218\n",
    )
    assignment_cfg = _write(
        tmp_path,
        "data/271218/a/config.toml",
        GRADING + "[fetch]\nassignment_id = 7\n",
    )
    _write(tmp_path, "data/271218/a/solutions/config.toml", GRADING)
    out = tmp_path / "data" / "271218" / "a" / "raw"
    out.mkdir(parents=True)

    _remember(out, assignment_cfg, 271218, 42, "attach")

    assignment_fetch = tomllib.loads(assignment_cfg.read_text())[
        "fetch"
    ]
    assert assignment_fetch["assignment_id"] == 42
    assert "course_id" not in assignment_fetch
    assert "mode" not in assignment_fetch
    course_fetch = tomllib.loads(course_cfg.read_text())["fetch"]
    assert course_fetch["course_id"] == 271218
    assert course_fetch["mode"] == "attach"


def test_retry_fetch_dedups_shared_assignment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mixed-tree retry: global (data/config.toml) and course
    configs both list assignment 9901 — the shared seen set must fetch it
    exactly once (driven through _fetch_course, the same loop _retry_fetch
    runs, with fetch_assignment mocked)."""
    global_cfg = _write(
        tmp_path,
        "data/config.toml",
        "[fetch]\ncourse_id = 271218\n"
        '\n[[fetch.assignments]]\nassignment_id = 9901\nout = "hw1/raw"\n',
    )
    course_cfg = _write(
        tmp_path,
        "data/271218/config.toml",
        "[fetch]\ncourse_id = 271218\n"
        '\n[[fetch.assignments]]\nassignment_id = 9901\nout = "hw1/raw"\n',
    )
    seen: set[tuple[int, int]] = set()
    with mock.patch("main.fetch_assignment") as mock_fetch:
        assert _fetch_course(None, global_cfg, None, None, seen) is True
        assert _fetch_course(None, course_cfg, None, None, seen) is True
    assert mock_fetch.call_count == 1
    assert "skip 9901" in capsys.readouterr().out


def test_container_bad_toml_raises_guidance_not_bare_decode(
    tmp_path: Path,
) -> None:
    """COSMETIC-3: a container config with broken TOML (unclosed string)
    must surface load_assignment_file's 'Invalid TOML' ValueError with the
    tip — never a bare TOMLDecodeError from _root_fetch's own parse."""
    cont = _write(tmp_path, "cont/config.toml", '[fetch]\ncourse_id = "271218\n')
    _write(tmp_path, "cont/child/config.toml", GRADING)

    with pytest.raises(ValueError, match="Invalid TOML") as excinfo:
        _classify_config(cont)
    assert not isinstance(excinfo.value, tomllib.TOMLDecodeError)
    assert "Tip: start from data/example/config.toml" in str(
        excinfo.value
    )


def test_remember_fetch_inline_comment_table_header(tmp_path: Path) -> None:
    """MINOR-3: '[fetch] # ...' with an inline comment must still be
    recognized as the [fetch] table header; before the fix remember_fetch
    appended a second [fetch] block and produced invalid TOML."""
    cfg = _write(
        tmp_path,
        "data/271218/config.toml",
        "[fetch] # fetch state\ncourse_id = 1\n",
    )
    remember_fetch(cfg, assignment_id=2)
    text = cfg.read_text(encoding="utf-8")
    assert text.count("[fetch]") == 1
    data = tomllib.loads(text)  # must parse
    assert data["fetch"]["course_id"] == 1
    assert data["fetch"]["assignment_id"] == 2


def test_find_global_config_repo_root_poison_rejected(tmp_path: Path) -> None:
    """MINOR-4: in the two-level layout the course config is
    data/config.toml; find_global_config climbs parent.parent to the
    repo root. A config.toml there must NOT be merged as a global layer —
    it would silently poison every assignment (here: copydetect_weight and
    course_id)."""
    course_cfg = _write(
        tmp_path,
        "data/config.toml",
        "[fetch]\ncourse_id = 1\n",
    )
    assignment_cfg = _write(tmp_path, "data/a/config.toml", GRADING)
    # Poison at the repo root (parent of data/, one level above the
    # structural layout root).
    _write(
        tmp_path,
        "config.toml",
        "[fetch]\ncourse_id = 999\n[plagiarism]\ncopydetect_weight = 0.1\n",
    )
    assert find_global_config(course_cfg) is None

    cfg = load_assignment_file(assignment_cfg)
    assert cfg.plagiarism.copydetect_weight == pytest.approx(0.95)  # default
    assert cfg.fetch.course_id == 1


def test_fetch_course_without_list_returns_false(tmp_path: Path) -> None:
    """MAJOR-2 semantic gate: a config with course_id but no
    [[fetch.assignments]] list (fresh course) yields False without touching
    the canvas (None passed just to prove it)."""
    course_cfg = _write(
        tmp_path,
        "data/271218/config.toml",
        "[fetch]\ncourse_id = 271218\n",
    )
    assert _fetch_course(None, course_cfg, None, None) is False


def test_remember_fresh_course_container(tmp_path: Path) -> None:
    """MAJOR-B: a fresh course config — only [fetch] state + its
    [[fetch.assignments]] list, no subdirectory configs — defeats all three
    structural heuristics; _remember must still treat it as a container
    (self-evidence): course_id/mode stay in the course config, assignment
    keys go to out.parent/config.toml, and nothing climbs to (or creates) a
    shared data/config.toml."""
    course_cfg = _write(
        tmp_path,
        "data/271218/config.toml",
        '[fetch]\ncourse_id = 271218\nmode = "attach"\n'
        '\n[[fetch.assignments]]\nassignment_id = 43\nout = "hw1/raw"\n',
    )
    out = tmp_path / "data" / "271218" / "hw1" / "raw"
    out.mkdir(parents=True)

    _remember(out, course_cfg, 271218, 42, "attach")

    course_fetch = tomllib.loads(course_cfg.read_text())["fetch"]
    assert course_fetch["course_id"] == 271218
    assert course_fetch["mode"] == "attach"
    assert "assignment_id" not in course_fetch
    assert [(e["assignment_id"], e["out"]) for e in course_fetch["assignments"]] == [
        (43, "hw1/raw")
    ]
    assignment_fetch = tomllib.loads((out.parent / "config.toml").read_text())["fetch"]
    assert assignment_fetch["assignment_id"] == 42
    # No shared global config was created (the old bug climbed to it).
    assert not (tmp_path / "data" / "config.toml").exists()


def test_fetch_course_missing_config_returns_false(tmp_path: Path) -> None:
    """MAJOR-A: _retry_fetch must never hand _fetch_course a path that does
    not exist (fresh three-level layout has no data/config.toml);
    the existence guard makes a missing file return False, not raise."""
    missing = tmp_path / "data" / "config.toml"
    assert _fetch_course(None, missing, None, None) is False


def test_load_config_container_without_fetch(tmp_path: Path) -> None:
    """MINOR-1: container = cannot load as an assignment (no [grading]).
    A config with no [fetch] at all — a fresh course/global config — is
    still a container: _load_config returns (path, None) instead of
    falling through to 'Missing required config fields: grading'."""
    cont = _write(
        tmp_path,
        "data/271218/config.toml",
        "[plagiarism]\ncopydetect_weight = 0.1\n",
    )
    path, fetch = _load_config(cont)
    assert path == cont.resolve()
    assert fetch is None

    assignment = _write(
        tmp_path,
        "data/271218/hw1/config.toml",
        GRADING + "[fetch]\nassignment_id = 42\n",
    )
    path2, fetch2 = _load_config(assignment)
    assert path2 == assignment.resolve()
    assert fetch2 is not None
    assert fetch2.assignment_id == 42

    bad = _write(
        tmp_path,
        "bad/config.toml",
        '[plagiarism]\ncopydetect_weight = "0.1\n',
    )
    with pytest.raises(ValueError, match="Invalid TOML"):
        _load_config(bad)
