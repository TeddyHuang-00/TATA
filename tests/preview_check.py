"""Runnable check for the raw-file preview in score_review_tui (TUI).

Uses App.run_test() + Pilot on temporary fixture data:
- processed/<stem>.md is preferred (already-converted; instant)
- ipynb preview is rendered (Markdown widget)
- document preview shows extracted text (Static)
- raw-file conversion as fallback when processed/ is missing
- wide/narrow layout toggle (grid 2x1 vs 1x2, matching discussion viewer)

Run: uv run tests/preview_check.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.cli_options import ScoreReviewCliOptions
from src.score_review import (
    Viewer,
    _convert_preview,
    _preview_content,
)
from textual import events
from textual.geometry import Size
from textual.widgets import Markdown, Static


def _write_notebook(path: Path) -> None:
    nb = {
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": ["# Preview Check", "hello from markdown cell"],
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": ["print(42)"],
            },
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    path.write_text(json.dumps(nb), encoding="utf-8")


async def _wait_for(pilot, predicate, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause()
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timeout waiting for predicate")


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        graded = root / "graded"
        raw = root / "raw"
        processed = root / "processed"
        graded.mkdir()
        raw.mkdir()
        processed.mkdir()
        (graded / "100572.json").write_text(
            json.dumps({"task1": {"rating": "correct", "feedback": "good work"}}),
            encoding="utf-8",
        )
        (graded / "201818.json").write_text(
            json.dumps({"task1": {"rating": "partial", "feedback": "meh"}}),
            encoding="utf-8",
        )
        _write_notebook(raw / "100572.ipynb")
        (raw / "201818.md").write_text("# doc text\nplain paragraph", encoding="utf-8")
        # 100572's preprocess output exists -> must be preferred over raw
        (processed / "100572.md").write_text(
            "# Processed Content\nstudent 100572", encoding="utf-8"
        )

        # preview content: processed first, raw conversion as fallback
        result = _preview_content(raw / "100572.ipynb", processed / "100572.md")
        assert result is not None
        kind, content = result
        assert kind == "markdown" and "Processed Content" in content, (
            kind,
            content[:80],
        )
        result = _preview_content(raw / "201818.md", None)
        assert result is not None
        kind, content = result
        assert kind == "text" and content.startswith("# doc text"), kind
        result = _preview_content(None, processed / "100572.md")
        assert result is not None
        kind, _ = result
        assert kind == "text"
        assert _preview_content(raw / "100572.ipynb", None) is not None
        assert _preview_content(None, None) is None

        # conversion dispatch (raw fallback): ipynb -> markdown; md -> text
        kind, content = _convert_preview(raw / "100572.ipynb")
        assert kind == "markdown" and "Preview Check" in content, (kind, content[:80])
        kind, content = _convert_preview(raw / "201818.md")
        assert kind == "text" and content.startswith("# doc text"), kind
        kind, content = _convert_preview(raw / "unsupported.xyz")
        assert kind == "text" and "Unsupported" in content, kind

        args = ScoreReviewCliOptions(score_dir=graded)
        app = Viewer(args)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()  # ScoreReviewScreen pushed in on_mount (async)
            md_view = app.screen.query_one("#preview-markdown", Markdown)
            text_view = app.screen.query_one("#preview-text", Static)
            panel = app.screen.query_one("#preview-panel")

            # student 1: ipynb + processed -> rendered Markdown from processed
            await _wait_for(
                pilot,
                lambda: (
                    "Processed Content" in (getattr(md_view, "_markdown", "") or "")
                ),
            )
            assert md_view.display and not text_view.display, "ipynb markdown not shown"
            assert "100572.ipynb" in panel.border_title

            # student 2: document (.md, no processed) -> Static text
            await pilot.click("#next-btn")
            await _wait_for(pilot, lambda: "# doc text" in text_view.content)
            assert text_view.display and not md_view.display, "doc text not shown"

            # layout: wide = split, narrow = stacked (resize lives on the screen)
            content_h = app.screen.query_one("#content-horizontal")
            assert not content_h.has_class("narrow"), "wide should not be narrow"
            review_screen = app.screen
            review_screen.on_resize(
                events.Resize(size=Size(80, 40), virtual_size=Size(80, 40))
            )
            assert content_h.has_class("narrow"), "narrow class not applied"
            review_screen.on_resize(
                events.Resize(size=Size(120, 40), virtual_size=Size(120, 40))
            )
            assert not content_h.has_class("narrow"), "wide class not restored"

    print("preview check OK")


if __name__ == "__main__":
    asyncio.run(main())
