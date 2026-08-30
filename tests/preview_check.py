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
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.cli_options import ScoreReviewCliOptions
from src.score_review import (
    Viewer,
    convert_preview,
    preview_content,
)
from textual import events
from textual.geometry import Size
from textual.pilot import Pilot
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


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], timeout: float = 60.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause()
        if predicate():
            return
        await asyncio.sleep(0.05)
    msg = "timeout waiting for predicate"
    raise AssertionError(msg)


async def main() -> None:  # ruff: ignore[too-many-statements]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        graded = root / "graded"
        raw = root / "raw"
        processed = root / "processed"
        graded.mkdir()
        raw.mkdir()
        processed.mkdir()
        (graded / "100001.json").write_text(
            json.dumps({"task1": {"rating": "correct", "feedback": "good work"}}),
            encoding="utf-8",
        )
        (graded / "100002.json").write_text(
            json.dumps({"task1": {"rating": "partial", "feedback": "meh"}}),
            encoding="utf-8",
        )
        _write_notebook(raw / "100001.ipynb")
        (raw / "100002.md").write_text("# doc text\nplain paragraph", encoding="utf-8")
        # 100001's preprocess output exists -> must be preferred over raw
        (processed / "100001.md").write_text(
            "# Processed Content\nstudent 100001", encoding="utf-8"
        )

        # preview content: processed first, raw conversion as fallback
        result = preview_content(raw / "100001.ipynb", processed / "100001.md")
        assert result is not None
        kind, content = result
        assert kind == "markdown", (kind, content[:80])
        assert "Processed Content" in content, (kind, content[:80])
        result = preview_content(raw / "100002.md", None)
        assert result is not None
        kind, content = result
        assert kind == "text", kind
        assert content.startswith("# doc text"), kind
        result = preview_content(None, processed / "100001.md")
        assert result is not None
        kind, _ = result
        assert kind == "text"
        assert preview_content(raw / "100001.ipynb", None) is not None
        assert preview_content(None, None) is None

        # conversion dispatch (raw fallback): ipynb -> markdown; md -> text
        kind, content = convert_preview(raw / "100001.ipynb")
        assert kind == "markdown", (kind, content[:80])
        assert "Preview Check" in content, (kind, content[:80])
        kind, content = convert_preview(raw / "100002.md")
        assert kind == "text", kind
        assert content.startswith("# doc text"), kind
        kind, content = convert_preview(raw / "unsupported.xyz")
        assert kind == "text", kind
        assert "Unsupported" in content, kind

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
            assert md_view.display, "ipynb markdown not shown"
            assert not text_view.display, "ipynb markdown not shown"
            assert "100001.ipynb" in panel.border_title

            # student 2: document (.md, no processed) -> Static text
            await pilot.click("#next-btn")
            await _wait_for(pilot, lambda: "# doc text" in text_view.content)
            assert text_view.display, "doc text not shown"
            assert not md_view.display, "doc text not shown"

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
