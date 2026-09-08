"""config_root must resolve the data/ root for global/course/assignment layouts."""

from __future__ import annotations

from pathlib import Path

import pytest
from src.shared.assignment_config import config_root


@pytest.mark.parametrize(
    ("rel", "expect"),
    [
        # (a) assignment level: data/<course>/<assignment>/config.toml
        ("data/course/assignment/config.toml", "data"),
        # (b) course level: data/<course>/config.toml
        ("data/course/config.toml", "data"),
        # (c) global level: data/config.toml
        ("data/config.toml", "data"),
        # (d) no data/ dir anywhere -> fall back to the config's parent
        ("cfg/config.toml", "cfg"),
    ],
)
def test_config_root(tmp_path: Path, rel: str, expect: str) -> None:
    cfg = tmp_path / rel
    cfg.parents[0].mkdir(parents=True, exist_ok=True)
    cfg.write_text("", encoding="utf-8")
    assert config_root(cfg) == tmp_path / expect
