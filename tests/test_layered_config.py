from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest
from src.assignment_config import (
    FetchSection,
    find_root_config,
    is_root_config,
    load_assignment_file,
)


def test_root_config_detection(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path], grading_config: str
) -> None:
    write_tree(tmp_path, "data/config.toml", "[fetch]\ncourse_id = 1\n")
    write_tree(tmp_path, "data/a/config.toml", grading_config)
    root = tmp_path / "data" / "config.toml"
    assignment = tmp_path / "data" / "a" / "config.toml"
    assert is_root_config(root)
    assert not is_root_config(assignment)
    assert find_root_config(assignment) == root
    assert find_root_config(root) is None


def test_layered_merge_root_defaults_assignment_overrides(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path], grading_config: str
) -> None:
    write_tree(
        tmp_path,
        "data/config.toml",
        '[fetch]\ncourse_id = 111111\nmode = "attach"\n',
    )
    write_tree(tmp_path, "data/a/config.toml", grading_config)
    cfg = load_assignment_file(tmp_path / "data" / "a" / "config.toml")
    assert cfg.fetch is not None
    assert cfg.fetch.course_id == 111111
    assert cfg.fetch.mode == "attach"
    # Assignment identity is the dir name, not a [fetch] key; the course
    # assignment list stays out of merged assignment configs.
    assert not cfg.fetch.assignments

    # Per-assignment modes live on [[fetch.assignments]] entries of the
    # course config (an entry's mode overrides the course default).
    write_tree(
        tmp_path,
        "data/config.toml",
        '[fetch]\ncourse_id = 111111\nmode = "attach"\n\n'
        '[[fetch.assignments]]\nid = 43\nmode = "text"\n',
    )
    cfg = load_assignment_file(tmp_path / "data" / "a" / "config.toml")
    assert cfg.fetch is not None
    assert cfg.fetch.mode == "attach"  # course default unchanged
    assert cfg.fetch.assignments == []  # list stripped from merged configs


def test_standalone_assignment_config_without_root(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path], grading_config: str
) -> None:
    write_tree(tmp_path, "data/a/config.toml", grading_config)
    cfg = load_assignment_file(tmp_path / "data" / "a" / "config.toml")
    # Assignment configs carry no [fetch] anymore; without a course config
    # there is no fetch state at all.
    assert cfg.fetch is None


def test_root_config_plagiarism_defaults_merge(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path], grading_config: str
) -> None:
    write_tree(
        tmp_path,
        "data/config.toml",
        "[plagiarism]\ncopydetect_weight = 0.9\nembedding_weight = 0.1\n",
    )
    write_tree(tmp_path, "data/a/config.toml", grading_config)
    cfg = load_assignment_file(tmp_path / "data" / "a" / "config.toml")
    assert cfg.plagiarism.copydetect_weight == pytest.approx(0.9)
    assert cfg.plagiarism.embedding_weight == pytest.approx(0.1)
    assert cfg.plagiarism.pairwise_alpha == pytest.approx(0.01)  # default


def test_root_config_alone_invalid_for_stages(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path]
) -> None:
    write_tree(tmp_path, "data/config.toml", "[fetch]\ncourse_id = 1\n")
    with pytest.raises(ValueError, match="Missing required config fields"):
        load_assignment_file(tmp_path / "data" / "config.toml")


def test_root_fetch_assignments_list_parses_and_does_not_leak(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path], grading_config: str
) -> None:
    write_tree(
        tmp_path,
        "data/config.toml",
        '[fetch]\ncourse_id = 111111\nmode = "attach"\n'
        "\n[[fetch.assignments]]\nid = 42\n"
        '\n[[fetch.assignments]]\nid = 43\nmode = "text"\n',
    )
    write_tree(tmp_path, "data/a/config.toml", grading_config)
    root = tmp_path / "data" / "config.toml"
    fetch = FetchSection.model_validate(tomllib.loads(root.read_text())["fetch"])
    assert [(e.id, e.mode) for e in fetch.assignments] == [
        (42, None),
        (43, "text"),
    ]

    # The root's assignment list is course-level orchestration; merged
    # assignment configs must not inherit it.
    cfg = load_assignment_file(tmp_path / "data" / "a" / "config.toml")
    assert cfg.fetch is not None
    assert cfg.fetch.assignments == []
