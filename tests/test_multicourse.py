from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import pytest
from src.assignment_config import (
    FetchSection,
    find_global_config,
    is_course_config,
    is_global_config,
    is_root_config,
    load_assignment_file,
)
from src.canvas_fetch import remember_course_fetch
from src.cli import _classify_config, _fetch_course, _load_config, _remember


def _write_three_level(
    tmp_path: Path,
    write_tree: Callable[[Path, str, str], Path],
    grading_config: str,
) -> tuple[Path, Path, Path]:
    """data/config.toml (global) + data/111111/config.toml
    (course) + data/111111/hw1/config.toml (assignment)."""
    global_cfg = write_tree(
        tmp_path,
        "data/config.toml",
        "[fetch]\ncourse_id = 111111\n",
    )
    course_cfg = write_tree(
        tmp_path,
        "data/111111/config.toml",
        "[fetch]\ncourse_id = 111111\n",
    )
    assignment_cfg = write_tree(
        tmp_path,
        "data/111111/hw1/config.toml",
        grading_config,
    )
    return global_cfg, course_cfg, assignment_cfg


def test_is_course_config_three_level(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path], grading_config: str
) -> None:
    global_cfg, course_cfg, assignment_cfg = _write_three_level(
        tmp_path, write_tree, grading_config
    )
    assert is_course_config(course_cfg)
    assert not is_course_config(global_cfg)
    assert not is_course_config(assignment_cfg)


def test_is_course_config_two_level_root(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path], grading_config: str
) -> None:
    write_tree(tmp_path, "data/config.toml", "[fetch]\ncourse_id = 1\n")
    write_tree(tmp_path, "data/a/config.toml", grading_config)
    root = tmp_path / "data" / "config.toml"
    assert is_course_config(root)
    assert not is_global_config(root)


def test_is_global_config_three_level(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path], grading_config: str
) -> None:
    global_cfg, course_cfg, assignment_cfg = _write_three_level(
        tmp_path, write_tree, grading_config
    )
    assert is_global_config(global_cfg)
    assert not is_global_config(course_cfg)
    assert not is_global_config(assignment_cfg)


def test_is_course_config_missing_file_is_false(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path], grading_config: str
) -> None:
    write_tree(tmp_path, "data/a/config.toml", grading_config)
    root = tmp_path / "data" / "config.toml"
    assert not is_course_config(root)
    assert not is_global_config(root)


def test_find_global_config(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path], grading_config: str
) -> None:
    global_cfg, course_cfg, _ = _write_three_level(tmp_path, write_tree, grading_config)
    assert find_global_config(course_cfg) == global_cfg
    assert find_global_config(global_cfg) is None


def test_three_layer_merge_precedence(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path], grading_config: str
) -> None:
    write_tree(
        tmp_path,
        "data/config.toml",
        "[plagiarism]\ncopydetect_weight = 0.9\nembedding_weight = 0.1\n",
    )
    write_tree(
        tmp_path,
        "data/111111/config.toml",
        "[plagiarism]\ncopydetect_weight = 0.85\n",
    )
    assignment_cfg = write_tree(
        tmp_path,
        "data/111111/hw1/config.toml",
        grading_config,
    )
    cfg = load_assignment_file(assignment_cfg)
    # Course overrides global per key.
    assert cfg.plagiarism.copydetect_weight == pytest.approx(0.85)
    assert cfg.plagiarism.embedding_weight == pytest.approx(0.1)

    # Assignment wins over both layers.
    write_tree(
        tmp_path,
        "data/111111/hw1/config.toml",
        grading_config + "[plagiarism]\ncopydetect_weight = 0.95\n",
    )
    cfg = load_assignment_file(assignment_cfg)
    assert cfg.plagiarism.copydetect_weight == pytest.approx(0.95)


def test_three_layer_fetch_assignments_do_not_leak(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path], grading_config: str
) -> None:
    write_tree(
        tmp_path,
        "data/config.toml",
        "[fetch]\ncourse_id = 111111\n\n[[fetch.assignments]]\nid = 99\n",
    )
    course_cfg = write_tree(
        tmp_path,
        "data/111111/config.toml",
        "[fetch]\ncourse_id = 111111\n\n[[fetch.assignments]]\nid = 43\n",
    )
    assignment_cfg = write_tree(
        tmp_path,
        "data/111111/hw1/config.toml",
        grading_config,
    )

    cfg = load_assignment_file(assignment_cfg)
    assert cfg.fetch is not None
    assert cfg.fetch.course_id == 111111
    assert cfg.fetch.assignments == []

    # The course config list still parses for course-level orchestration.
    course_fetch = FetchSection.model_validate(
        tomllib.loads(course_cfg.read_text())["fetch"]
    )
    assert [e.id for e in course_fetch.assignments] == [43]


def test_remember_container_writes_own_config(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path], grading_config: str
) -> None:
    """C1 regression: -c points at a course config; course-level keys must
    land in that config itself (never climb up to the global) and its
    [[fetch.assignments]] list must survive."""
    course_cfg = write_tree(
        tmp_path,
        "data/111111/config.toml",
        "[fetch]\ncourse_id = 111111\n\n[[fetch.assignments]]\nid = 43\n",
    )
    write_tree(tmp_path, "data/111111/hw1/config.toml", grading_config)
    out = tmp_path / "data" / "111111" / "hw1" / "raw"
    out.mkdir(parents=True)

    _remember(course_cfg, 111111, 42)

    course_fetch = tomllib.loads(course_cfg.read_text())["fetch"]
    assert course_fetch["course_id"] == 111111
    assert "mode" not in course_fetch  # never writes [fetch].mode
    # [[fetch.assignments]] was not wiped by the write; the fetched entry
    # was appended (id-only).
    assert [e["id"] for e in course_fetch["assignments"]] == [43, 42]
    # The assignment config was never written.
    assignment_data = tomllib.loads((out.parent / "config.toml").read_text())
    assert "fetch" not in assignment_data


def test_remember_course_fetch_upsert_preserves_assignments(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path]
) -> None:
    """Upsert: only provided fields change; existing keys and the
    [[fetch.assignments]] list stay."""
    cfg = write_tree(
        tmp_path,
        "data/111111/config.toml",
        "[fetch]\ncourse_id = 111111\n\n[[fetch.assignments]]\nid = 43\n",
    )

    remember_course_fetch(cfg, course_id=999)

    fetch = tomllib.loads(cfg.read_text())["fetch"]
    assert fetch["course_id"] == 999
    assert "mode" not in fetch  # never writes [fetch].mode
    assert [e["id"] for e in fetch["assignments"]] == [43]


def test_nested_config_does_not_break_container_detection(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path], grading_config: str
) -> None:
    """M1 regression: a nested config.toml (data/111111/a/solutions/)
    defeats the leaf heuristics (is_course_config/is_global_config both
    return False), but the container check (is_root_config) must stay True so
    _load_config keeps treating the global/course configs as containers."""
    global_cfg = write_tree(
        tmp_path,
        "data/config.toml",
        "[fetch]\ncourse_id = 111111\n",
    )
    course_cfg = write_tree(
        tmp_path,
        "data/111111/config.toml",
        "[fetch]\ncourse_id = 111111\n",
    )
    assignment_cfg = write_tree(
        tmp_path,
        "data/111111/a/config.toml",
        grading_config,
    )
    write_tree(tmp_path, "data/111111/a/solutions/config.toml", grading_config)

    assert is_root_config(global_cfg)
    assert is_root_config(course_cfg)

    # _load_config must classify both as containers (fetch-only state), never
    # fall through to load_assignment_file and fail on missing [grading].
    path, fetch = _load_config(global_cfg)
    assert path == global_cfg
    assert fetch is not None
    assert fetch.course_id == 111111
    path, fetch = _load_config(course_cfg)
    assert path == course_cfg
    assert fetch is not None
    assert fetch.course_id == 111111

    # The assignment (even with its nested config) still loads as an assignment.
    cfg = load_assignment_file(assignment_cfg)
    assert cfg.fetch is not None
    assert cfg.fetch.course_id == 111111


def test_load_config_fresh_course_self_evidence(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path]
) -> None:
    """MAJOR-1: a freshly created course config — only [fetch] state +
    [[fetch.assignments]], no [grading], no subdirectory configs — defeats
    every structural heuristic and must not fall through to
    load_assignment_file (which would raise Missing required [grading]); it
    is a self-evident container because it holds a [fetch] table."""
    course_cfg = write_tree(
        tmp_path,
        "data/111111/config.toml",
        "[fetch]\ncourse_id = 111111\n\n[[fetch.assignments]]\nid = 43\n",
    )
    for detect in (is_root_config, is_course_config, is_global_config):
        assert not detect(course_cfg)

    path, fetch = _load_config(str(course_cfg))  # must not raise
    assert path == course_cfg
    assert fetch is not None
    assert fetch.course_id == 111111
    assert [e.id for e in fetch.assignments] == [43]


def test_remember_nested_assignment_no_container_pollution(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path], grading_config: str
) -> None:
    """M1 regression: data/111111/a/config.toml has a nested
    subdirectory config (a/solutions/config.toml), which made the
    structural container heuristics classify it as a container — _remember
    then wrote course_id/mode into the ASSIGNMENT config (pollution) and
    the course config never got mode. Pure self-evidence: it loads as an
    assignment, so course keys go to the course config."""
    course_cfg = write_tree(
        tmp_path,
        "data/111111/config.toml",
        "[fetch]\ncourse_id = 111111\n",
    )
    assignment_cfg = write_tree(
        tmp_path,
        "data/111111/a/config.toml",
        grading_config,
    )
    write_tree(tmp_path, "data/111111/a/solutions/config.toml", grading_config)
    out = tmp_path / "data" / "111111" / "a" / "raw"
    out.mkdir(parents=True)

    _remember(assignment_cfg, 111111, 42)

    assignment_data = tomllib.loads(assignment_cfg.read_text())
    assert "fetch" not in assignment_data  # assignment config never written
    course_fetch = tomllib.loads(course_cfg.read_text())["fetch"]
    assert course_fetch["course_id"] == 111111
    assert "mode" not in course_fetch  # never writes [fetch].mode
    assert [e["id"] for e in course_fetch["assignments"]] == [42]


def test_retry_fetch_dedups_shared_assignment(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    write_tree: Callable[[Path, str, str], Path],
) -> None:
    """Mixed-tree retry: global (data/config.toml) and course
    configs both list assignment 9901 — the shared seen set must fetch it
    exactly once (driven through _fetch_course, the same loop _retry_fetch
    runs, with fetch_assignment mocked)."""
    global_cfg = write_tree(
        tmp_path,
        "data/config.toml",
        "[fetch]\ncourse_id = 111111\n\n[[fetch.assignments]]\nid = 9901\n",
    )
    course_cfg = write_tree(
        tmp_path,
        "data/111111/config.toml",
        "[fetch]\ncourse_id = 111111\n\n[[fetch.assignments]]\nid = 9901\n",
    )
    seen: set[tuple[int, int]] = set()
    with mock.patch("src.cli.fetch_assignment") as mock_fetch:
        assert _fetch_course(None, global_cfg, None, None, seen) is True
        assert _fetch_course(None, course_cfg, None, None, seen) is True
    assert mock_fetch.call_count == 1
    assert "skip 9901" in capsys.readouterr().out


def test_container_bad_toml_raises_guidance_not_bare_decode(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path], grading_config: str
) -> None:
    """COSMETIC-3: a container config with broken TOML (unclosed string)
    must surface load_assignment_file's 'Invalid TOML' ValueError with the
    tip — never a bare TOMLDecodeError from _root_fetch's own parse."""
    cont = write_tree(tmp_path, "cont/config.toml", '[fetch]\ncourse_id = "111111\n')
    write_tree(tmp_path, "cont/child/config.toml", grading_config)

    with pytest.raises(ValueError, match="Invalid TOML") as excinfo:
        _classify_config(cont)
    assert not isinstance(excinfo.value, tomllib.TOMLDecodeError)
    assert "Tip: start from data/example/config.toml" in str(excinfo.value)


def test_remember_course_fetch_inline_comment_table_header(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path]
) -> None:
    """MINOR-3: '[fetch] # ...' with an inline comment must still be
    recognized as the [fetch] table header; before the fix
    remember_course_fetch appended a second [fetch] block and produced
    invalid TOML."""
    cfg = write_tree(
        tmp_path,
        "data/111111/config.toml",
        "[fetch] # fetch state\ncourse_id = 1\n",
    )
    remember_course_fetch(cfg, course_id=1, assignment_id=2)
    text = cfg.read_text(encoding="utf-8")
    assert text.count("[fetch]") == 1
    data = tomllib.loads(text)  # must parse
    assert data["fetch"]["course_id"] == 1
    assert data["fetch"]["assignments"][0]["id"] == 2


def test_find_global_config_repo_root_poison_rejected(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path], grading_config: str
) -> None:
    """MINOR-4: in the two-level layout the course config is
    data/config.toml; find_global_config climbs parent.parent to the
    repo root. A config.toml there must NOT be merged as a global layer —
    it would silently poison every assignment (here: copydetect_weight and
    course_id)."""
    course_cfg = write_tree(
        tmp_path,
        "data/config.toml",
        "[fetch]\ncourse_id = 1\n",
    )
    assignment_cfg = write_tree(tmp_path, "data/a/config.toml", grading_config)
    # Poison at the repo root (parent of data/, one level above the
    # structural layout root).
    write_tree(
        tmp_path,
        "config.toml",
        "[fetch]\ncourse_id = 999\n[plagiarism]\ncopydetect_weight = 0.1\n",
    )
    assert find_global_config(course_cfg) is None

    cfg = load_assignment_file(assignment_cfg)
    assert cfg.plagiarism.copydetect_weight == pytest.approx(0.95)  # default
    assert cfg.fetch.course_id == 1


def test_fetch_course_without_list_returns_false(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path]
) -> None:
    """MAJOR-2 semantic gate: a config with course_id but no
    [[fetch.assignments]] list (fresh course) yields False without touching
    the canvas (None passed just to prove it)."""
    course_cfg = write_tree(
        tmp_path,
        "data/111111/config.toml",
        "[fetch]\ncourse_id = 111111\n",
    )
    assert _fetch_course(None, course_cfg, None, None) is False


def test_remember_fresh_course_container(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path]
) -> None:
    """MAJOR-B: a fresh course config — only [fetch] state + its
    [[fetch.assignments]] list, no subdirectory configs — defeats all three
    structural heuristics; _remember must still treat it as a container
    (self-evidence): course_id stays in the course config, assignment
    keys go to out.parent/config.toml, and nothing climbs to (or creates) a
    shared data/config.toml."""
    course_cfg = write_tree(
        tmp_path,
        "data/111111/config.toml",
        "[fetch]\ncourse_id = 111111\n\n[[fetch.assignments]]\nid = 43\n",
    )
    out = tmp_path / "data" / "111111" / "hw1" / "raw"
    out.mkdir(parents=True)

    _remember(course_cfg, 111111, 42)

    course_fetch = tomllib.loads(course_cfg.read_text())["fetch"]
    assert course_fetch["course_id"] == 111111
    assert "mode" not in course_fetch  # never writes [fetch].mode
    assert "assignment_id" not in course_fetch
    assert [e["id"] for e in course_fetch["assignments"]] == [43, 42]
    # Nothing is written to the (missing) assignment config.
    assert not (out.parent / "config.toml").exists()
    # No shared global config was created (the old bug climbed to it).
    assert not (tmp_path / "data" / "config.toml").exists()


def test_fetch_course_missing_config_returns_false(tmp_path: Path) -> None:
    """MAJOR-A: _retry_fetch must never hand _fetch_course a path that does
    not exist (fresh three-level layout has no data/config.toml);
    the existence guard makes a missing file return False, not raise."""
    missing = tmp_path / "data" / "config.toml"
    assert _fetch_course(None, missing, None, None) is False


def test_load_config_container_without_fetch(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path], grading_config: str
) -> None:
    """MINOR-1: container = cannot load as an assignment (no [grading]).
    A config with no [fetch] at all — a fresh course/global config — is
    still a container: _load_config returns (path, None) instead of
    falling through to 'Missing required config fields: grading'."""
    cont = write_tree(
        tmp_path,
        "data/111111/config.toml",
        "[plagiarism]\ncopydetect_weight = 0.1\n",
    )
    path, fetch = _load_config(cont)
    assert path == cont.resolve()
    assert fetch is None

    assignment = write_tree(
        tmp_path,
        "data/111111/hw1/config.toml",
        grading_config,
    )
    path2, fetch2 = _load_config(assignment)
    assert path2 == assignment.resolve()
    # Assignment configs carry no [fetch] anymore; without a course config
    # there is no fetch state.
    assert fetch2 is None

    bad = write_tree(
        tmp_path,
        "bad/config.toml",
        '[plagiarism]\ncopydetect_weight = "0.1\n',
    )
    with pytest.raises(ValueError, match="Invalid TOML"):
        _load_config(bad)
