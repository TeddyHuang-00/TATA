"""Runnable headless check for ScoreReviewScreen reuse (T5).

Follows tests/preview_check.py: App.run_test() + Pilot on temporary fixture
data. Verifies the platform integration contract:
- ScoreReviewScreen can be pushed on an arbitrary App (push_screen)
- the screen renders (students loaded, criteria list populated)
- prev/next bindings still work inside the pushed screen
- escape pops back to the underlying screen
- Viewer is now a thin shell that composes ScoreReviewScreen

Run: uv run tests/review_screen_check.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.cli_options import ScoreReviewCliOptions
from src.score_review import ScoreReviewScreen, Viewer
from textual.app import App, ComposeResult
from textual.widgets import Static


class _Harness(App):
    """Stand-in platform shell: a home screen to push onto."""

    def compose(self) -> ComposeResult:
        yield Static("home", id="home")


def _make_graded(graded: Path) -> None:
    (graded / "100572.json").write_text(
        json.dumps({"task1": {"rating": "correct", "feedback": "good work"}}),
        encoding="utf-8",
    )
    (graded / "201818.json").write_text(
        json.dumps({"task1": {"rating": "partial", "feedback": "meh"}}),
        encoding="utf-8",
    )


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        graded = Path(tmp) / "graded"
        graded.mkdir()
        _make_graded(graded)
        # Assignment-root alias.toml supplies display names (was roster.csv).
        (Path(tmp) / "alias.toml").write_text(
            '[student]\n"100572" = "Aalla, A"\n"201818" = "Zed, Z"\n',
            encoding="utf-8",
        )

        # 1. platform contract: push -> render -> esc pops back
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            home = app.query_one("#home", Static)
            assert home.display
            assert len(app.screen_stack) == 1

            app.push_screen(ScoreReviewScreen(graded, pop_on_escape=True))
            await pilot.pause()
            assert isinstance(app.screen, ScoreReviewScreen), type(app.screen)
            review = app.screen
            assert [s["student"] for s in review.students] == ["100572", "201818"]
            assert [s["sortable_name"] for s in review.students] == [
                "Aalla, A",
                "Zed, Z",
            ]
            listing = review.query_one("#criteria-list", Static)
            assert "good work" in str(listing.content), str(listing.content)

            # bindings still live inside the pushed screen (next student)
            await pilot.press("right")
            assert review.index == 1, review.index

            # esc pops back to the underlying screen
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, ScoreReviewScreen)
            assert len(app.screen_stack) == 1
            assert app.query_one("#home", Static).display

        # 2. CLI shell: Viewer pushes ScoreReviewScreen full-screen on mount
        viewer = Viewer(ScoreReviewCliOptions(score_dir=graded))
        async with viewer.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert isinstance(viewer.screen, ScoreReviewScreen), type(viewer.screen)
            assert len(viewer.screen.students) == 2
            listing = viewer.screen.query_one("#criteria-list", Static)
            assert "good work" in str(listing.content)

            # esc is a no-op in the CLI shell: the stack below is the App's own
            # default Screen, not a platform screen to pop back to.
            stack_before = len(viewer.screen_stack)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(viewer.screen, ScoreReviewScreen), type(viewer.screen)
            assert len(viewer.screen_stack) == stack_before

    print("review screen check OK")


if __name__ == "__main__":
    asyncio.run(main())
