"""Regression: settings save must preserve user comments and unknown keys.

``edit_config`` (the shared TUI/CLI writer, :mod:`src.config_edit`) applies
edits in place on the parsed tomlkit document, so comments and unknown keys
survive a save — a whole-file rebuild would destroy them.
"""

from __future__ import annotations

from pathlib import Path

from src.shared.config_edit import edit_config

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


def test_checkbox_bool_writes_bare_true(tmp_path: Path) -> None:
    """Regression (F2): a checkbox save writes a real bool, not ``"true"``.

    The checkbox path is ``SettingsScreen._parse("bool", ...)`` ->
    ``edit_config``; a quoted string would break the processing section
    model (bool field rejects str).
    """
    from src.tui.settings import SettingsScreen

    cfg = tmp_path / "config.toml"
    cfg.write_text("[processing]\n", encoding="utf-8")
    edit_config(
        cfg,
        {"processing": {"remove_base64_images": SettingsScreen._parse("bool", "true")}},
    )
    text = cfg.read_text(encoding="utf-8")
    assert "remove_base64_images = true" in text
    assert 'remove_base64_images = "true"' not in text


def test_save_empty_string_unsets_the_key(tmp_path: Path) -> None:
    """Clearing a text field saves as unset: the key is removed (same as the
    per-field reset), not written as ``= ""``. A ``template_file = ""`` would
    resolve to the config's own directory and the plagiarism extraction dies
    with "Unsupported input type for extraction"."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "# keep this comment\n"
        "[plagiarism]\n"
        'template_file = "template.ipynb"\n'
        "display_threshold = 0.75\n",
        encoding="utf-8",
    )
    edit_config(cfg, {"plagiarism": {"template_file": ""}})
    text = cfg.read_text(encoding="utf-8")
    assert "template_file" not in text
    assert "# keep this comment" in text  # comment survives the round-trip
    assert "display_threshold = 0.75" in text  # sibling key survives
