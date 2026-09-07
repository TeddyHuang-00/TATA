from __future__ import annotations

from src.shared.assignment_config import ProcessingSection


def test_default_visual_evaluation_false_and_screenshot_pages_removed() -> None:
    section = ProcessingSection()
    assert section.visual_evaluation is False
    assert not hasattr(section, "screenshot_pages")


def test_legacy_render_screenshots_key_still_accepted() -> None:
    section = ProcessingSection.model_validate({"render_screenshots": True})
    assert section.visual_evaluation is True


def test_new_key_wins_when_both_present() -> None:
    section = ProcessingSection.model_validate({
        "visual_evaluation": True,
        "render_screenshots": False,
    })
    assert section.visual_evaluation is True


def test_model_dump_outputs_new_key_only() -> None:
    dumped = ProcessingSection().model_dump()
    assert "visual_evaluation" in dumped
    assert "render_screenshots" not in dumped
    assert "screenshot_pages" not in dumped
