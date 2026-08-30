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
import tempfile
from pathlib import Path

from e2e_common import write_aliases, write_graded  # isort: skip - seeds repo-root sys.path before src imports
from src.cli_options import ScoreReviewCliOptions
from src.score_review import ScoreReviewScreen, Viewer
from textual.app import App, ComposeResult
from textual.widgets import Select, Static


class _Harness(App):
    """Stand-in platform shell: a home screen to push onto."""

    def compose(self) -> ComposeResult:
        yield Static("home", id="home")


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        graded = Path(tmp) / "graded"
        graded.mkdir()
        write_graded(graded, "100001", "correct", "good work")
        write_graded(graded, "100002", "partial", "meh")
        # late submission: fetch suffixes the stem (_LATE_0); the alias key is
        # the base uid ("333333").
        write_graded(graded, "333333_LATE_0", "partial", "late")
        # Assignment-root alias.toml supplies display names (was roster.csv).
        write_aliases(
            Path(tmp) / "alias.toml",
            students={"100001": "Doe, A", "100002": "Zed, Z", "333333": "Doe, Jane"},
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
            assert [s["student"] for s in review.students] == [
                "100001",
                "333333_LATE_0",
                "100002",
            ]
            assert [s["sortable_name"] for s in review.students] == [
                "Doe, A",
                "Doe, Jane",
                "Zed, Z",
            ]
            # _LATE_N stem resolves to the base-uid alias, shown raw in the id
            late = next(s for s in review.students if s["student"] == "333333_LATE_0")
            assert late["sortable_name"] == "Doe, Jane", late
            select = review.query_one("#student-select", Select)
            prompts = [str(prompt) for prompt, _ in select._options]  # type: ignore[attr-defined]
            assert "Doe, Jane (333333_LATE_0)" in prompts, """
                alias must render with the RAW stem in the id part
            """
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
            assert len(viewer.screen.students) == 3
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
