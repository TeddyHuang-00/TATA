"""Regression: settings save must preserve user comments and unknown keys.

The old hand-rolled ``_dump_toml`` rebuilt the whole config from the parsed
dict (re-serializing every line), so any comment a user had added inside a
section was destroyed on save. ``_dump_edits`` applies the edits in place on
the parsed tomlkit document instead.
"""

from __future__ import annotations

from pathlib import Path

from src.config_edit import edit_config

CONFIG = (
    "[fetch]\n"
    "# keep this comment\n"
    "course_id = 111111\n"
    'mode = "attach"\n'
    'custom_field = "user made"\n'
)


def test_save_preserves_comments_and_unknown_keys(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(CONFIG, encoding="utf-8")
    edit_config(cfg, {"fetch": {"course_id": 999}})
    text = cfg.read_text(encoding="utf-8")
    assert "# keep this comment" in text  # comment survives the round-trip
    assert 'custom_field = "user made"' in text  # unknown key survives
    assert "course_id = 999" in text  # the edit itself was applied
