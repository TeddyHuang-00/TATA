from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from src.processing import preprocess_assignment


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc not installed")
def test_txt_text_submission_converts_as_html(tmp_path: Path) -> None:
    """Canvas text entries are saved as .txt but are HTML: preprocess must
    route them through the HTML -> markdown conversion (regression)."""
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "390489_LATE_0.txt").write_text("<p>my <b>answer</b></p>", encoding="utf-8")
    (tmp_path / "config.toml").write_text(
        '[grading]\nrubric = "r.toml"\nsystem_prompt = ["p.md"]\nprovider = "deepseek"\n',
        encoding="utf-8",
    )

    result = preprocess_assignment(tmp_path / "config.toml")

    md = tmp_path / "processed" / "390489_LATE_0.md"
    assert md.exists()
    content = md.read_text(encoding="utf-8")
    assert "answer" in content
    assert "<p>" not in content
    assert "<b>" not in content
    assert result is not None
    assert result["success"] == 1
