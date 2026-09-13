"""Runnable headless check for ScoreReviewScreen reuse (T5).

Follows tests/preview_check.py: App.run_test() + Pilot on temporary fixture
data. Verifies the platform integration contract:
- ScoreReviewScreen can be pushed on an arbitrary App (push_screen)
- the screen renders (students loaded, criteria list populated)
- prev/next bindings still work inside the pushed screen
- escape pops back to the underlying screen
- Viewer is now a thin shell that composes ScoreReviewScreen
- criterion rows: number = copy key (both read the visible/filtered list),
  name background = rating color, no rating text

Run: uv run tests/review_screen_check.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from e2e_common import write_aliases, write_graded  # isort: skip - seeds repo-root sys.path before src imports
from rich.style import Style
from src.shared.cli_options import ScoreReviewCliOptions
from src.tui.score_review import ScoreReviewScreen, Viewer, _filter_label, find_raw_file
from textual import events
from textual.app import App, ComposeResult
from textual.color import Color
from textual.geometry import Size
from textual.pilot import Pilot
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Button, Select, Static


class _Harness(App):
    """Stand-in platform shell: a home screen to push onto."""

    def compose(self) -> ComposeResult:
        yield Static("home", id="home")


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        graded = _write_fixture(Path(tmp))

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

            # D9 framing: both columns are single round $primary panels in the
            # wide and in the stacked (narrow) layout (shared score_review.tcss)
            await _check_panel_frames(app, review, pilot)

            # numbered criteria: number = copy key, name bg = rating color
            await _check_criteria_rendering(app, review, pilot)

            # filter buttons work for every rating (empty / illegal-id chars)
            await _check_filter_buttons(review, pilot)

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
            # the CLI viewer path gets the same framed criteria panel
            _assert_round_primary_frame(
                viewer.screen.query_one("#criteria-scroll"),
                Color.parse(viewer.get_css_variables()["primary"]),
                "cli-criteria",
            )

            # esc is a no-op in the CLI shell: the stack below is the App's own
            # default Screen, not a platform screen to pop back to.
            stack_before = len(viewer.screen_stack)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(viewer.screen, ScoreReviewScreen), type(viewer.screen)
            assert len(viewer.screen_stack) == stack_before

        # 3. find_raw_file: folder members resolve by exact stem (body) or
        # by base-uid match (attachment members <uid>_N / <uid>_LATE_N).
        _check_find_raw_file(Path(tmp))

    print("review screen check OK")


def _assert_round_primary_frame(panel: Widget, primary: Color, name: str) -> None:
    """All four panel edges are a single ``round $primary`` border (D9)."""
    edges = (
        panel.styles.border_top,
        panel.styles.border_right,
        panel.styles.border_bottom,
        panel.styles.border_left,
    )
    for edge in edges:
        assert edge[0] == "round", (name, edges)
        assert edge[1] == primary, (name, edges)


async def _check_panel_frames(app: App, review: Screen, pilot: Pilot) -> None:
    """Both columns are identical round ``$primary`` frames, in the wide and
    in the stacked (narrow) layout — no edge may be dropped."""
    primary = Color.parse(app.get_css_variables()["primary"])
    criteria = review.query_one("#criteria-scroll")
    _assert_round_primary_frame(criteria, primary, "criteria")
    _assert_round_primary_frame(review.query_one("#preview-panel"), primary, "preview")
    # narrow drops nothing: the old border-right:none override is gone
    review.on_resize(events.Resize(size=Size(80, 40), virtual_size=Size(80, 40)))
    await pilot.pause()
    content_h = review.query_one("#content-horizontal")
    assert content_h.has_class("narrow"), "narrow class not applied"
    assert criteria.styles.border_right[0] == "round", criteria.styles.border_right
    review.on_resize(events.Resize(size=Size(120, 40), virtual_size=Size(120, 40)))
    await pilot.pause()
    assert not content_h.has_class("narrow"), "wide class not restored"


def _write_fixture(tmp: Path) -> Path:
    """graded/ fixture: three students, the first with five criteria (see
    ``_add_multi_criteria``); alias.toml at the assignment root supplies names."""
    graded = tmp / "graded"
    graded.mkdir()
    write_graded(graded, "100001", "correct", "good work")
    _add_multi_criteria(graded / "100001.json")
    write_graded(graded, "100002", "partial", "meh")
    # late submission: fetch suffixes the stem (_LATE_0); the alias key is the
    # base uid ("333333").
    write_graded(graded, "333333_LATE_0", "partial", "late")
    write_aliases(
        tmp / "alias.toml",
        students={"100001": "Doe, A", "100002": "Zed, Z", "333333": "Doe, Jane"},
    )
    return graded


def _add_multi_criteria(path: Path) -> None:
    """Add partial/incorrect/empty/bracket-rating/markup-rating criteria to a
    graded record: row numbers and their background colors must follow the
    visible (filtered) list and the rating -> color map. The empty rating and
    ``correct]`` both crashed ``compose()`` when the filter button id was built
    from the rating text (illegal Textual id characters); ``[red]boom[/red]``
    renders as a styled chip when the label is not escaped — regression
    coverage for all three."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["task2"] = {"rating": "partial", "feedback": "half right"}
    payload["task3"] = {"rating": "incorrect", "feedback": "wrong"}
    # empty rating -> the "(empty)" filter bucket
    payload["task4"] = {"rating": "", "feedback": "no rating"}
    # "correct]" is not a Textual id character: id=f"filter-correct]" crashed
    payload["task5"] = {"rating": "correct]", "feedback": "weird rating"}
    # markup-shaped rating: the chip label must show it literally, unstyled
    payload["task6"] = {"rating": "[red]boom[/red]", "feedback": "markup rating"}
    path.write_text(json.dumps(payload), encoding="utf-8")


def _rendered_rows(listing: Static) -> dict[int, str]:
    """Content-row index -> rendered text (``render_line`` is content-relative)."""
    return {y: listing.render_line(y).text.rstrip() for y in range(listing.size.height)}


def _rgb(color: object) -> str:
    """Opaque RGB hex string (no '#', lowercase). ``get_style_at`` hands back
    *rich* Colors (never equal to a textual ``Color``), and textual's ``.hex``
    appends alpha — so compare the RGB triple only."""
    triplet = getattr(color, "triplet", None)
    if triplet is not None:
        return str(triplet.hex).lstrip("#").lower()
    red, green, blue = color[:3]  # type: ignore[index]
    return f"{red:02x}{green:02x}{blue:02x}"


def _expected_bg(css: dict[str, str], css_var: str) -> str:
    """Rendered background for a CSS color variable. Translucent tokens (e.g.
    ``$foreground-muted``, 60% alpha) are composited over the screen
    background, so compare against that blend rather than the raw token."""
    token = Color.parse(css[css_var])
    # an opaque token has blend factor 0, which returns the token itself
    return _rgb(token.blend(Color.parse(css["background"]), 1.0 - token.a))


def _name_styles(listing: Static, y: int, width: int) -> list[Style]:
    """Styles across a name span: ``get_style_at`` is region-relative, so
    content rows are offset by the widget's padding."""
    padding = listing.styles.padding
    pad_left = int(getattr(padding.left, "value", padding.left))
    pad_top = int(getattr(padding.top, "value", padding.top))
    return [
        listing.get_style_at(x, y + pad_top) for x in range(pad_left, pad_left + width)
    ]


async def _check_criteria_rendering(app: App, review: Screen, pilot: Pilot) -> None:
    """Feedback #4: criterion rows are numbered ``1..n`` in the *visible* list
    order (the same list the copy keys read), the name's background is the
    rating color, and no rating text is rendered."""
    listing = review.query_one("#criteria-list", Static)
    css = app.get_css_variables()

    expected = [
        ("1. task1", "good work", "success"),
        ("2. task2", "half right", "warning"),
        ("3. task3", "wrong", "error"),
        # empty / unmapped ratings: neutral fallback background, never a crash
        ("4. task4", "no rating", "foreground-muted"),
        ("5. task5", "weird rating", "foreground-muted"),
        ("6. task6", "markup rating", "foreground-muted"),
    ]
    rows = _rendered_rows(listing)
    for prefix, comment, css_var in expected:
        y = next((y for y, text in rows.items() if text.startswith(prefix)), None)
        assert y is not None, (prefix, rows)
        assert comment in rows[y], (prefix, rows[y])
        styles = _name_styles(listing, y, len(prefix))
        assert {_rgb(style.bgcolor) for style in styles} == {
            _expected_bg(css, css_var)
        }, (
            prefix,
            {_rgb(style.bgcolor) for style in styles},
            css_var,
        )
        # no [reverse]: it flips the name back to a foreground color, i.e. the
        # rating color would no longer be the *background*
        assert not any(style.reverse for style in styles), (prefix, styles)
        # the inline "auto" foreground resolves to pure black/white; without it
        # the default foreground on $success/$warning is ~1.5:1 contrast
        assert {_rgb(style.color) for style in styles} <= {"000000", "ffffff"}, prefix
    assert "rating:" not in str(listing.content), str(listing.content)

    # number <-> copy key read the same list: pressing "1" copies the comment
    # of the row labelled "1." (before and after filtering)
    await pilot.press("1")
    assert app.clipboard == "good work", app.clipboard
    await pilot.press("4")
    assert app.clipboard == "no rating", app.clipboard
    await pilot.press("5")
    assert app.clipboard == "weird rating", app.clipboard
    await pilot.press("6")
    assert app.clipboard == "markup rating", app.clipboard
    # a number past the visible rows is a no-op, never an IndexError
    await pilot.press("7")
    assert app.clipboard == "markup rating", app.clipboard

    review.rating_on["correct"] = False
    review._sync_filters()
    review._render_review()
    await pilot.pause()
    assert {text for text in _rendered_rows(listing).values() if text} == {
        "1. task2  half right",
        "2. task3  wrong",
        "3. task4  no rating",
        "4. task5  weird rating",
        "5. task6  markup rating",
    }, _rendered_rows(listing)
    await pilot.press("1")
    assert app.clipboard == "half right", app.clipboard
    await pilot.press("3")
    assert app.clipboard == "no rating", app.clipboard

    review.rating_on["correct"] = True
    review._sync_filters()
    review._render_review()
    await pilot.pause()


def _filter_button(review: Screen, label: str) -> Button:
    """The filter button showing ``label`` (the rating text is the label)."""
    return next(
        button
        for button in review.query("#filters Button")
        if str(button.label) == label
    )


async def _click_filter(pilot: Pilot, button: Button) -> None:
    """Click a filter button, waiting out the press-animation debounce: a
    second click while the 0.2 s ``-active`` animation runs is ignored."""
    await pilot.click(button)
    for _ in range(20):
        await pilot.pause()
        if not button.has_class("-active"):
            return
        await asyncio.sleep(0.05)


async def _check_filter_buttons(review: Screen, pilot: Pilot) -> None:
    """Regression: an empty rating, a bracket rating (``correct]``) and a
    markup rating (``[red]boom[/red]``) all hit the two rating-shaped chips.
    ``filter-(empty)`` / ``filter-correct]`` are invalid Textual ids (ids are
    now positional and map back to the rating), and an unescaped markup rating
    renders as a style instead of literal text. Every chip must show readable
    literal text, and toggling it must update the visible list."""
    buttons = list(review.query("#filters Button"))
    labels = {str(button.label) for button in buttons}
    assert {"(empty)", "correct]", "[red]boom[/red]"} <= labels, labels
    # no chip may be blank: a whitespace-only rating falls back to "(empty)"
    assert all(str(button.label).strip() for button in buttons), labels
    assert _filter_label("   ") == "(empty)"
    listing = review.query_one("#criteria-list", Static)

    def rows() -> set[str]:
        return {text for text in _rendered_rows(listing).values() if text}

    # "(empty)" toggles the empty-rating row out and back in
    empty_btn = _filter_button(review, "(empty)")
    await _click_filter(pilot, empty_btn)
    assert review.rating_on["(empty)"] is False
    assert empty_btn.has_class("off"), empty_btn.classes
    assert not any("no rating" in text for text in rows()), rows()
    await _click_filter(pilot, empty_btn)
    assert review.rating_on["(empty)"] is True
    assert not empty_btn.has_class("off"), empty_btn.classes
    assert any("no rating" in text for text in rows()), rows()

    # "correct]": the id -> rating reverse mapping survives the toggle
    bracket_btn = _filter_button(review, "correct]")
    await _click_filter(pilot, bracket_btn)
    assert review.rating_on["correct]"] is False
    assert not any("weird rating" in text for text in rows()), rows()
    await _click_filter(pilot, bracket_btn)
    assert review.rating_on["correct]"] is True
    assert any("weird rating" in text for text in rows()), rows()

    # "[red]boom[/red]": markup-shaped rating stays literal text on the chip
    markup_btn = _filter_button(review, "[red]boom[/red]")
    await _click_filter(pilot, markup_btn)
    assert review.rating_on["[red]boom[/red]"] is False
    assert not any("markup rating" in text for text in rows()), rows()
    await _click_filter(pilot, markup_btn)
    assert review.rating_on["[red]boom[/red]"] is True
    assert any("markup rating" in text for text in rows()), rows()


def _check_find_raw_file(tmp: Path) -> None:
    """Folder members resolve by exact stem (body) or base-uid match."""
    raw = tmp / "raw"
    (raw / "20").mkdir(parents=True)
    (raw / "20" / "20_0.ipynb").write_bytes(b"nb")
    (raw / "20" / "20_1.docx").write_bytes(b"doc")
    (raw / "30").mkdir(parents=True)
    (raw / "30" / "30_LATE_0.html").write_bytes(b"<p>late</p>")
    (raw / "30" / "30_0.ipynb").write_bytes(b"nb")
    hit = find_raw_file(tmp, "20")
    assert hit is not None
    assert hit.name == "20_0.ipynb"
    hit = find_raw_file(tmp, "30")
    assert hit is not None
    assert hit.name == "30_0.ipynb"
    # a suffixed student id resolves to the late body by exact stem
    hit = find_raw_file(tmp, "30_LATE_0")
    assert hit is not None
    assert hit.name == "30_LATE_0.html"


if __name__ == "__main__":
    asyncio.run(main())
