from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest
from src.shared.assignment_config import (
    FetchSection,
    ProcessingSection,
    find_root_config,
    is_root_config,
    load_assignment_file,
    resolve_assignment_paths,
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
        "[fetch]\ncourse_id = 111111\n",
    )
    write_tree(tmp_path, "data/a/config.toml", grading_config)
    cfg = load_assignment_file(tmp_path / "data" / "a" / "config.toml")
    assert cfg.fetch is not None
    assert cfg.fetch.course_id == 111111
    # Assignment identity is the dir name, not a [fetch] key; the course
    # assignment list stays out of merged assignment configs.
    assert not cfg.fetch.assignments

    # The assignment list lives on [[fetch.assignments]] entries of the
    # course config (id-only entries).
    write_tree(
        tmp_path,
        "data/config.toml",
        "[fetch]\ncourse_id = 111111\n\n[[fetch.assignments]]\nid = 43\n",
    )
    cfg = load_assignment_file(tmp_path / "data" / "a" / "config.toml")
    assert cfg.fetch is not None
    assert cfg.fetch.course_id == 111111
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
        "[fetch]\ncourse_id = 111111\n"
        "\n[[fetch.assignments]]\nid = 42\n"
        "\n[[fetch.assignments]]\nid = 43\n",
    )
    write_tree(tmp_path, "data/a/config.toml", grading_config)
    root = tmp_path / "data" / "config.toml"
    fetch = FetchSection.model_validate(tomllib.loads(root.read_text())["fetch"])
    assert [e.id for e in fetch.assignments] == [42, 43]

    # The root's assignment list is course-level orchestration; merged
    # assignment configs must not inherit it.
    cfg = load_assignment_file(tmp_path / "data" / "a" / "config.toml")
    assert cfg.fetch is not None
    assert cfg.fetch.assignments == []


def test_processing_input_format_accepts_image_and_pdf() -> None:
    """Config-level input_format mirrors the pipeline's format set (drift
    regression: assignment_config used to lag processing by image/pdf)."""
    for val in ("image", "pdf", ["ipynb", "image"]):
        model = ProcessingSection.model_validate({"input_format": val})
        assert model.input_format == val


def test_empty_reference_file_resolves_to_none(
    tmp_path: Path, write_tree: Callable[[Path, str, str], Path], grading_config: str
) -> None:
    """assignment.reference_file = "" counts as unset: joined to the base dir
    it would resolve to the assignment dir itself (a bogus reference), while
    every caller branches on None for "no reference" mode."""
    write_tree(
        tmp_path,
        "data/a/config.toml",
        grading_config + '[assignment]\nreference_file = ""\n',
    )
    cfg = load_assignment_file(tmp_path / "data" / "a" / "config.toml")
    paths = resolve_assignment_paths(cfg, tmp_path / "data" / "a")
    assert paths.reference_file is None
