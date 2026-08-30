# Score Review viewer — Textual TUI, web via textual-serve (--web).
# Usage: uv run score-view <score_dir>  |  uv run main.py view <score_dir> [--web]
from __future__ import annotations

import json
import shlex
import sys
import tempfile
from pathlib import Path
from typing import ClassVar

from pydantic import ValidationError
from pydantic_settings import CliApp
from rich.markup import escape
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Markdown,
    ProgressBar,
    Select,
    Static,
)
from textual_serve.server import Server

from src.cli_options import ScoreReviewCliOptions
from src.processing import (
    _convert_docx_to_markdown,
    _convert_html_to_markdown,
    _convert_ipynb_to_markdown,
)
from src.tata_alias import student_display_name

# Layout threshold: stack the panels below this width (Textual has no media
# queries; same threshold as the discussion TUI viewer).
NARROW_WIDTH = 100


def _extract_criterion_feedback(payload: object, prefix: str = "") -> list[dict]:
    rows: list[dict] = []
    if isinstance(payload, dict):
        feedback = payload.get("feedback")
        if isinstance(feedback, str) and feedback.strip():
            rows.append({
                "criterion": prefix or "criterion",
                "rating": str(payload.get("rating") or ""),
                "comment": feedback.strip(),
            })
        for key, value in payload.items():
            if key == "feedback":
                continue
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            rows.extend(_extract_criterion_feedback(value, next_prefix))
    elif isinstance(payload, list):
        for i, value in enumerate(payload):
            next_prefix = f"{prefix}[{i}]" if prefix else f"[{i}]"
            rows.extend(_extract_criterion_feedback(value, next_prefix))
    return rows


def _load_students(score_dir: Path) -> list[dict]:
    students = []
    for file in sorted(score_dir.glob("*.json"), key=lambda f: f.name.lower()):
        try:
            payload = json.loads(file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            students.append({
                "student": file.stem,
                "criteria": _extract_criterion_feedback(payload),
                "json": payload,
                "raw_file": _find_raw_file(score_dir, file.stem),
                "processed_file": _find_processed_file(score_dir, file.stem),
            })
    return students


def _find_raw_file(score_dir: Path, student_id: str) -> Path | None:
    """Locate the original submission for a student in a sibling raw/ dir.

    Graded JSON stem and raw file stem match (canvas user id, including
    _LATE_N suffixes); any extension is acceptable.
    """
    for raw_dir in (score_dir.parent / "raw", score_dir / "raw"):
        matches = sorted(raw_dir.glob(f"{student_id}.*"))
        if matches:
            return matches[0]
    return None


def _find_processed_file(score_dir: Path, student_id: str) -> Path | None:
    """Locate the preprocess output for a student in a sibling processed/ dir."""
    for md_dir in (score_dir.parent / "processed", score_dir / "processed"):
        candidate = md_dir / f"{student_id}.md"
        if candidate.exists():
            return candidate
    return None


# Raw-file preview: prefer processed/<stem>.md (exactly what the grader saw);
# fall back to converting the raw file (ipynb -> Markdown widget, documents
# .docx/.html/.md/.txt -> extracted plain text).
PREVIEW_MAX_CHARS = 250_000


def _truncate(content: str) -> str:
    # ponytail: hard cap keeps the Markdown widget responsive on huge
    # notebooks; raise PREVIEW_MAX_CHARS if full content is ever needed.
    if len(content) <= PREVIEW_MAX_CHARS:
        return content
    return (
        content[:PREVIEW_MAX_CHARS] + f"\n\n_[truncated, {len(content)} chars total]_"
    )


def _convert_preview(raw: Path) -> tuple[str, str]:
    """Convert a raw submission to (kind, content).

    kind is "markdown" (feed the Markdown widget) or "text" (plain Static).
    Uses the same converters as the preprocess stage so the preview matches
    what the grader saw.
    """
    suffix = raw.suffix.lower()
    if suffix in {".md", ".txt", ".text"}:
        return "text", _truncate(raw.read_text(encoding="utf-8", errors="replace"))
    if suffix == ".ipynb":
        kind, converter = "markdown", _convert_ipynb_to_markdown
    elif suffix == ".docx":
        kind, converter = "text", _convert_docx_to_markdown
    elif suffix == ".html":
        kind, converter = "text", _convert_html_to_markdown
    else:
        return "text", f"Unsupported raw file type: {raw.name}"
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / (raw.stem + ".md")
        converter(raw, output)
        content = output.read_text(encoding="utf-8", errors="replace")
    return kind, _truncate(content)


def _preview_content(
    raw: Path | None, processed: Path | None
) -> tuple[str, str] | None:
    """(kind, content) for a student's preview, or None if no file is known.

    Prefers the preprocess markdown (already-converted, what the grader saw)
    over a fresh raw conversion.
    """
    if processed is not None:
        kind = (
            "markdown" if raw is not None and raw.suffix.lower() == ".ipynb" else "text"
        )
        return kind, _truncate(processed.read_text(encoding="utf-8", errors="replace"))
    if raw is not None:
        return _convert_preview(raw)
    return None


# Display order and color classes for rating filters.
RATING_ORDER = ["correct", "partial", "incorrect"]
RATING_CLASS = {
    "incorrect": "rating-incorrect",
    "completely incorrect": "rating-incorrect",
    "partial": "rating-partial",
    "somewhat incorrect": "rating-partial",
    "correct": "rating-correct",
    "somewhat correct": "rating-correct",
    "completely correct": "rating-correct",
}


def _rating_sort_key(rating: str) -> tuple[int, str]:
    lowered = rating.lower()
    rank = next(
        (i for i, base in enumerate(RATING_ORDER) if base in lowered.split()),
        len(RATING_ORDER),
    )
    return (rank, lowered)


class ScoreReviewScreen(Screen):
    """Reusable score-review screen (shared by CLI Viewer and the platform).

    Carries the full viewer UI/state — students, preview cache, bindings —
    so the platform can ``push_screen(ScoreReviewScreen(score_dir))`` while
    the CLI entry point keeps the original full-screen ``Viewer(App)`` shell.
    """

    CSS_PATH = "score_review.tcss"
    BINDINGS: ClassVar = [
        ("left", "prev", "Prev"),
        ("right", "next", "Next"),
        ("up", "prev", "Prev"),
        ("down", "next", "Next"),
        ("j", "toggle_json", "Toggle raw JSON"),
        ("1", "copy_criterion('1')", "Copy comment 1"),
        ("2", "copy_criterion('2')", "Copy comment 2"),
        ("3", "copy_criterion('3')", "Copy comment 3"),
        ("4", "copy_criterion('4')", "Copy comment 4"),
        ("5", "copy_criterion('5')", "Copy comment 5"),
        ("6", "copy_criterion('6')", "Copy comment 6"),
        ("7", "copy_criterion('7')", "Copy comment 7"),
        ("8", "copy_criterion('8')", "Copy comment 8"),
        ("9", "copy_criterion('9')", "Copy comment 9"),
        ("escape", "close", "Back"),
    ]

    def __init__(self, score_dir: Path, pop_on_escape: bool = False) -> None:
        super().__init__()
        # pop_on_escape: True when pushed on a platform screen (esc pops back);
        # False in the CLI Viewer, where the screen below is the App's own empty
        # default Screen — esc is a no-op there (crossing that boundary removed
        # the review screen with no way back, see design/03 §3.3).
        self.pop_on_escape = pop_on_escape
        self.students = _load_students(score_dir)
        # Display names come from the alias.toml chain; the assignment root is
        # score_dir.parent, so the chain candidates are assignment root /
        # parent / parent.parent alias.toml files (see src.tata_alias).
        assignment_root = score_dir.parent
        assignments_dir = assignment_root.parent.parent
        course_dir_name = assignment_root.parent.name
        assignment_dir_name = assignment_root.name
        for s in self.students:
            s["sortable_name"] = student_display_name(
                assignments_dir,
                course_dir_name,
                assignment_dir_name,
                s["student"],
            )
        # deterministic order: sortable name, then user id
        self.students.sort(key=lambda s: (s["sortable_name"].lower(), s["student"]))
        self.index = 0
        self.show_json = False
        # student id -> (kind, content); rebuilt lazily per student.
        self.preview_cache: dict[str, tuple[str, str]] = {}
        self.preview_pending: set[str] = set()
        # rating -> bool; default all on (web version pre-selects everything)
        ratings: set[str] = {
            c["rating"] or "(empty)" for s in self.students for c in s["criteria"]
        }
        self.rating_on = dict.fromkeys(sorted(ratings, key=_rating_sort_key), True)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="toolbar"):
            yield Button("◀ Prev", id="prev-btn", flat=True)
            yield Select(
                [
                    (f"{s['sortable_name']} ({s['student']})", i)
                    for i, s in enumerate(self.students)
                ],
                id="student-select",
                prompt="Jump to student",
                allow_blank=True,
            )
            yield Button("Next ▶", id="next-btn", flat=True)
        yield ProgressBar(id="progress", show_percentage=False, show_eta=False)
        with Horizontal(id="filters"):
            for rating in self.rating_on:
                cls = RATING_CLASS.get(rating.lower(), "rating-other")
                yield Button(
                    rating,
                    id=f"filter-{rating.replace(' ', '_')}",
                    flat=True,
                    classes=f"filter {cls}",
                )
        with Horizontal(id="content-horizontal"):
            with VerticalScroll(id="criteria-scroll"):
                yield Static(id="criteria-list")
                yield Markdown("", id="json-view")
            with (
                Container(id="preview-panel") as panel,
                VerticalScroll(id="preview-scroll"),
            ):
                # ponytail: raw/submission text may contain [brackets] (citations,
                # markdown links) — never parse it as rich markup.
                yield Static("", id="preview-text", markup=False)
                yield Markdown("", id="preview-markdown")
        self.preview_panel = panel
        panel.border_title = "Raw File"
        yield Footer()

    def on_mount(self) -> None:
        progress = self.query_one("#progress", ProgressBar)
        progress.total = len(self.students)
        self._sync_filters()
        self._render_review()

    def on_resize(self, event: events.Resize) -> None:
        content = self.query_one("#content-horizontal")
        content.set_class(event.size.width < NARROW_WIDTH, "narrow")

    # -- helpers ---------------------------------------------------------

    @property
    def current(self) -> dict | None:
        return self.students[self.index] if self.students else None

    def visible_criteria(self, student: dict) -> list[dict]:
        return [
            c
            for c in student["criteria"]
            if self.rating_on.get(c["rating"] or "(empty)", True)
        ]

    def _sync_filters(self) -> None:
        for rating, on in self.rating_on.items():
            btn = self.query_one(f"#filter-{rating.replace(' ', '_')}", Button)
            btn.set_class(not on, "off")

    def _render_review(self) -> None:
        s = self.current
        self._refresh_preview()
        listing = self.query_one("#criteria-list", Static)
        json_view = self.query_one("#json-view", Markdown)
        prev = self.query_one("#prev-btn", Button)
        nxt = self.query_one("#next-btn", Button)
        select = self.query_one("#student-select", Select)
        progress = self.query_one("#progress", ProgressBar)

        if not s:
            listing.update("No student data in this folder.")
            prev.disabled = nxt.disabled = True
            return

        prev.disabled = self.index == 0
        nxt.disabled = self.index == len(self.students) - 1
        progress.update(progress=self.index + 1)
        if select.value != self.index:
            select.value = self.index

        lines = []
        for pos, item in enumerate(self.visible_criteria(s), 1):
            rating = item["rating"] or "(empty)"
            cls = RATING_CLASS.get(rating.lower(), "rating-other")
            lines.append(
                f"[b][reverse]{escape(item['criterion'])}[/][/b]  "
                f"[{cls}]rating: {escape(rating)}[/]"
                f"  [dim](press {pos} to copy)[/]\n"
                f"{escape(item['comment'])}\n"
            )
        listing.update(
            "\n".join(lines)
            if lines
            else "No comments match the selected rating filter."
        )
        json_view.update(
            f"```json\n{json.dumps(s['json'], indent=2)}\n```" if self.show_json else ""
        )

    # -- raw file preview -------------------------------------------------

    def _refresh_preview(self) -> None:
        """Show cached preview or kick off a build worker for the current
        student (content is cached by student id)."""
        s = self.current
        if not s:
            self._show_preview(None, "text", "No student data in this folder.")
            return
        cached = self.preview_cache.get(s["student"])
        if cached is not None:
            self._show_preview(s, *cached)
            return
        raw = s.get("raw_file")
        processed = s.get("processed_file")
        if raw is None and processed is None:
            self._show_preview(s, "text", "No submission file found for this student.")
            return
        if s["student"] in self.preview_pending:
            return
        self.preview_pending.add(s["student"])
        self._show_preview(
            s, "text", f"Converting {raw.name}…" if raw is not None else "Loading…"
        )
        self.run_worker(
            lambda: self._build_preview_sync(raw, processed, s["student"]),
            thread=True,
            group="preview",
            exclusive=True,
        )

    def _show_preview(self, s: dict | None, kind: str, content: str) -> None:
        text_view = self.query_one("#preview-text", Static)
        md_view = self.query_one("#preview-markdown", Markdown)
        submission = s.get("raw_file") or s.get("processed_file") if s else None
        self.preview_panel.border_title = (
            f"Preview: {submission.name}" if submission else "Preview"
        )
        is_markdown = kind == "markdown"
        text_view.display = not is_markdown
        md_view.display = is_markdown
        (md_view if is_markdown else text_view).update(content)

    def _build_preview_sync(
        self, raw: Path | None, processed: Path | None, student: str
    ) -> None:
        try:
            result: tuple[str, str] | None = _preview_content(raw, processed)
        except Exception as error:
            result = ("text", f"Preview failed:\n{error}")
        self.app.call_from_thread(self._on_preview_ready, student, result)

    def _on_preview_ready(self, student: str, result: tuple[str, str] | None) -> None:
        self.preview_pending.discard(student)
        if result is not None:
            self.preview_cache[student] = result
        s = self.current
        if s is not None and s["student"] == student:
            self._show_preview(s, *(result or ("text", "No preview available.")))

    # -- actions / handlers ----------------------------------------------

    def action_prev(self) -> None:
        if self.index > 0:
            self.index -= 1
            self._render_review()

    def action_next(self) -> None:
        if self.index < len(self.students) - 1:
            self.index += 1
            self._render_review()

    def action_toggle_json(self) -> None:
        self.show_json = not self.show_json
        self._render_review()

    def action_close(self) -> None:
        """Pop back to the previous screen (platform push); CLI: no-op."""
        if self.pop_on_escape and len(self.app.screen_stack) > 1:
            self.app.pop_screen()

    def action_copy_criterion(self, pos: str) -> None:
        s = self.current
        items = self.visible_criteria(s) if s else []
        idx = int(pos) - 1
        if 0 <= idx < len(items):
            item = items[idx]
            self.app.copy_to_clipboard(item["comment"])
            self.notify(f"Copied comment for {item['criterion']}")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "prev-btn":
            self.action_prev()
        elif bid == "next-btn":
            self.action_next()
        elif bid.startswith("filter-"):
            rating = bid[len("filter-") :].replace("_", " ")
            self.rating_on[rating] = not self.rating_on[rating]
            self._sync_filters()
            self._render_review()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "student-select" and event.value is not None:
            self.index = event.value
            self._render_review()


class Viewer(App):
    """CLI view: full-screen App hosting ScoreReviewScreen (thin shell).

    All behavior lives on ScoreReviewScreen; ``run()`` starts this App
    exactly as before, so ``main.py view`` / ``uv run score-view`` are
    unchanged.
    """

    TITLE = "Score Review"

    def __init__(self, args: ScoreReviewCliOptions) -> None:
        super().__init__()
        self.score_dir = args.score_dir

    def on_mount(self) -> None:
        # ponytail: a Screen composed inline gets zero size in Textual 8.2,
        # so the CLI shell pushes it — the same contract the platform uses.
        self.push_screen(ScoreReviewScreen(self.score_dir))


def _serve_web(score_dir: Path) -> None:
    """Run the viewer under textual-serve (http://localhost:8000)."""
    command = f"uv run score-view {shlex.quote(str(score_dir))}"
    Server(command).serve()


def run(args: ScoreReviewCliOptions) -> None:
    """Entry shared by `main.py view` and the score-view script."""
    if args.web:
        _serve_web(args.score_dir)
    else:
        Viewer(args).run()


def main() -> None:
    try:
        args = CliApp.run(ScoreReviewCliOptions)
    except ValidationError as exc:
        msg = exc.errors()[0]["msg"]
        if msg.startswith("Value error, "):
            msg = msg.removeprefix("Value error, ")
        print(f"error: {msg}", file=sys.stderr)
        raise SystemExit(2) from exc
    run(args)


if __name__ == "__main__":
    main()
