"""Shared pytest fixtures for the TATA test suite."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

# [grading] block duplicated in test_layered_config.py + test_multicourse.py.
GRADING = '[grading]\nrubric = "rubrics/x.toml"\nsystem_prompt = "prompt/system.md"\nprovider = "ollama"\n'


@pytest.fixture
def grading_config() -> str:
    return GRADING


@pytest.fixture
def write_tree() -> Callable[[Path, str, str], Path]:
    def _write(tree: Path, rel: str, text: str) -> Path:
        p = tree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    return _write
