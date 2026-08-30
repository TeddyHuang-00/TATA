from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from src.assignment_config import (
    FetchSection,
    find_root_config,
    is_root_config,
    load_assignment_file,
)

GRADING = '[grading]\nrubric = "rubrics/x.toml"\nsystem_prompt = "prompt/system.md"\nprovider = "ollama"\n'


def _write(tree: Path, rel: str, text: str) -> Path:
    p = tree / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_root_config_detection(tmp_path: Path) -> None:
    _write(tmp_path, "data/config.toml", "[fetch]\ncourse_id = 1\n")
    _write(tmp_path, "data/a/config.toml", GRADING)
    root = tmp_path / "data" / "config.toml"
    assignment = tmp_path / "data" / "a" / "config.toml"
    assert is_root_config(root)
    assert not is_root_config(assignment)
    assert find_root_config(assignment) == root
    assert find_root_config(root) is None


def test_layered_merge_root_defaults_assignment_overrides(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "data/config.toml",
        '[fetch]\ncourse_id = 271218\nmode = "attach"\n',
    )
    _write(
        tmp_path,
        "data/a/config.toml",
        GRADING + "[fetch]\nassignment_id = 42\n",
    )
    cfg = load_assignment_file(tmp_path / "data" / "a" / "config.toml")
    assert cfg.fetch is not None
    assert cfg.fetch.course_id == 271218
    assert cfg.fetch.assignment_id == 42
    assert cfg.fetch.mode == "attach"

    # Assignment value wins per key.
    _write(
        tmp_path,
        "data/b/config.toml",
        GRADING + '[fetch]\nassignment_id = 43\nmode = "text"\n',
    )
    cfg = load_assignment_file(tmp_path / "data" / "b" / "config.toml")
    assert cfg.fetch is not None
    assert cfg.fetch.mode == "text"
    assert cfg.fetch.course_id == 271218


def test_standalone_assignment_config_without_root(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "data/a/config.toml",
        GRADING + "[fetch]\ncourse_id = 7\nassignment_id = 9\n",
    )
    cfg = load_assignment_file(tmp_path / "data" / "a" / "config.toml")
    assert cfg.fetch is not None
    assert cfg.fetch.course_id == 7
    assert cfg.fetch.assignment_id == 9


def test_root_config_plagiarism_defaults_merge(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "data/config.toml",
        "[plagiarism]\ncopydetect_weight = 0.9\nembedding_weight = 0.1\n",
    )
    _write(tmp_path, "data/a/config.toml", GRADING)
    cfg = load_assignment_file(tmp_path / "data" / "a" / "config.toml")
    assert cfg.plagiarism.copydetect_weight == pytest.approx(0.9)
    assert cfg.plagiarism.embedding_weight == pytest.approx(0.1)
    assert cfg.plagiarism.pairwise_alpha == pytest.approx(0.01)  # default


def test_root_config_alone_invalid_for_stages(tmp_path: Path) -> None:
    _write(tmp_path, "data/config.toml", "[fetch]\ncourse_id = 1\n")
    with pytest.raises(ValueError, match="Missing required config fields"):
        load_assignment_file(tmp_path / "data" / "config.toml")


def test_root_fetch_assignments_list_parses_and_does_not_leak(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "data/config.toml",
        '[fetch]\ncourse_id = 271218\nmode = "attach"\n'
        '\n[[fetch.assignments]]\nassignment_id = 42\nout = "a/raw"\n'
        '\n[[fetch.assignments]]\nassignment_id = 43\nmode = "text"\nout = "b/raw"\n',
    )
    _write(
        tmp_path,
        "data/a/config.toml",
        GRADING + "[fetch]\nassignment_id = 42\n",
    )
    root = tmp_path / "data" / "config.toml"
    fetch = FetchSection.model_validate(tomllib.loads(root.read_text())["fetch"])
    assert [(e.assignment_id, e.mode, e.out) for e in fetch.assignments] == [
        (42, None, "a/raw"),
        (43, "text", "b/raw"),
    ]

    # The root's assignment list is course-level orchestration; merged
    # assignment configs must not inherit it.
    cfg = load_assignment_file(tmp_path / "data" / "a" / "config.toml")
    assert cfg.fetch is not None
    assert cfg.fetch.assignments == []
