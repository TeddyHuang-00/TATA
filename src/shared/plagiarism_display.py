"""Plagiarism display/text helpers shared by the TUI screens (S4).

Pure (no-widget) functions moved out of ``src.tui.plagiarism``,
``src.tui.scan``, and ``src.tui.score_review`` so the two plagiarism
screens and the scanner can share them without a tui-internal module
cycle: ``plagiarism`` ⇄ ``plagiarism_detail`` previously imported each
other (the detail screen imported ``compare_content`` from the
plagiarism workspace, which lazily imported the detail screens back).
Everything here is text/file logic — rich markup is allowed (rich is an
existing dependency); no Textual widgets are imported.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape

from src.shared.aliases import student_display_name
from src.shared.processing import (
    convert_docx_to_markdown,
    convert_html_to_markdown,
    convert_ipynb_to_markdown,
)

if TYPE_CHECKING:
    from src.tui.scan import AssignmentInfo

PREVIEW_MAX_CHARS = 250_000
SIDE_MAX_LINES = 300


def base_uid(stem: str) -> str:
    """Canvas user id with a fetch suffix (_LATE_N or _N) stripped.

    File stems carry the suffix (canvas_fetch fetches bodies and
    attachments as ``<uid>{_LATE_i|_i}``), but alias.toml keys are the
    unsuffixed uid — so a stem like ``301999_LATE_0`` must resolve to the
    ``301999`` alias.
    """
    return re.sub(r"_(?:LATE_)?\d+$", "", stem)


def pair_pct(pair: dict) -> float:
    """max_similarity_pct as float (0.0 on missing/malformed values).

    Real data writes floats, but dirty JSON (strings, nulls, non-dict
    entries) must not crash the TUI's on_mount scan.
    """
    if not isinstance(pair, dict):
        return 0.0
    try:
        return float(pair.get("max_similarity_pct", 0.0))
    except (TypeError, ValueError):
        return 0.0


def overlap_display(pair: dict) -> str:
    """token_overlap cell text (int count, or line-set length when a list)."""
    overlap = pair.get("token_overlap")
    if isinstance(overlap, list):
        return str(len(overlap))
    if isinstance(overlap, float):
        return str(int(overlap))
    return str(overlap)  # int or missing


def pair_side_name(
    assignments_dir: Path,
    course_dir_name: str,
    assignment_info: AssignmentInfo,
    file_name: str | None,
) -> str:
    """Display name for one pair side: the file stem is the student uid."""
    stem = Path(str(file_name)).stem
    return student_display_name(
        assignments_dir,
        course_dir_name,
        assignment_info.dir_name,
        base_uid(stem),
    )


def find_raw_file(score_dir: Path, student_id: str) -> Path | None:
    """Locate the original submission for a student in a sibling raw/ dir.

    Graded JSON stem and raw file stem match (canvas user id, including
    _LATE_N suffixes); any extension is acceptable. Multi-file students
    (auto-collect all) land in raw/<uid>/ folders — when the flat glob
    finds nothing, fall back to a stem match inside raw/<uid>/ /
    raw/<stem-without-suffix>/ (the folder name is the unsuffixed uid; a
    suffixed stem strips the suffix). Single-file behavior is unchanged.
    """
    for raw_dir in (score_dir.parent / "raw", score_dir / "raw"):
        matches = sorted(raw_dir.glob(f"{student_id}.*"))
        if matches:
            return matches[0]
    # Fallback: multi-file student -> raw/<uid>/<name> (folder per student).
    base = re.sub(r"_(?:LATE_)?\d+$", "", student_id) or student_id
    for raw_dir in (score_dir.parent / "raw", score_dir / "raw"):
        folders = (raw_dir / base, raw_dir / student_id)
        # Exact stem first (body member: <uid>.html / <uid>_LATE_0.html).
        hits = sorted(
            p
            for folder in folders
            for p in folder.rglob("*")
            if p.is_file() and not p.name.startswith(".") and p.stem == student_id
        )
        if hits:
            return hits[0]
        # Then base-uid match (attachment members: <uid>_0.ipynb,
        # <uid>_1.docx, <uid>_LATE_0.html ...). The folder name is the
        # unsuffixed uid; the stem is the uid plus the fetch suffix.
        hits = sorted(
            p
            for folder in folders
            for p in folder.rglob("*")
            if p.is_file()
            and not p.name.startswith(".")
            and re.sub(r"_(?:LATE_)?\d+$", "", p.stem) == base
        )
        if hits:
            return hits[0]
    return None


def _truncate(content: str) -> str:
    # ponytail: hard cap keeps the Markdown widget responsive on huge
    # notebooks; raise PREVIEW_MAX_CHARS if full content is ever needed.
    if len(content) <= PREVIEW_MAX_CHARS:
        return content
    return (
        content[:PREVIEW_MAX_CHARS] + f"\n\n_[truncated, {len(content)} chars total]_"
    )


def convert_preview(raw: Path) -> tuple[str, str]:
    """Convert a raw submission to (kind, content).

    kind is "markdown" (feed the Markdown widget) or "text" (plain Static).
    Uses the same converters as the preprocess stage so the preview matches
    what the grader saw.
    """
    suffix = raw.suffix.lower()
    if suffix in {".md", ".txt", ".text"}:
        return "text", _truncate(raw.read_text(encoding="utf-8", errors="replace"))
    if suffix == ".ipynb":
        kind, converter = "markdown", convert_ipynb_to_markdown
    elif suffix == ".docx":
        kind, converter = "text", convert_docx_to_markdown
    elif suffix == ".html":
        kind, converter = "text", convert_html_to_markdown
    else:
        return "text", f"Unsupported raw file type: {raw.name}"
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / (raw.stem + ".md")
        converter(raw, output)
        content = output.read_text(encoding="utf-8", errors="replace")
    return kind, _truncate(content)


def preview_content(raw: Path | None, processed: Path | None) -> tuple[str, str] | None:
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
        return convert_preview(raw)
    return None


def _resolve_side(
    assignment_dir: Path, file_name: str
) -> tuple[Path | None, Path | None]:
    """(raw, processed) for one compare side; code submissions carry a
    ``<assignment>__<stem>`` prefix handled by stripping segments."""
    stem = Path(file_name).stem
    candidates = [stem, *(part for part in stem.split("__") if part)]
    processed_dir = assignment_dir / "processed"
    for candidate in candidates:
        processed = processed_dir / f"{candidate}.md"
        raw = find_raw_file(processed_dir, candidate)
        if raw is not None or processed.is_file():
            return raw, processed if processed.is_file() else None
    return None, None


def _side_lines(assignment_dir: Path, file_name: str, overlap_lines: set[int]) -> str:
    """Numbered file lines; lines in ``overlap_lines`` rendered red."""
    raw, processed = _resolve_side(assignment_dir, file_name)
    result = preview_content(raw, processed)
    if result is None:
        return f"[dim]{escape(file_name)}: file not found[/dim]"
    lines = result[1].splitlines()[:SIDE_MAX_LINES]
    out: list[str] = []
    for number, line in enumerate(lines, 1):
        escaped = f"{number:>4}  {escape(line)}"
        if number in overlap_lines:
            out.append(f"[red]{escaped}[/red]")
        else:
            out.append(escaped)
    return "\n".join(out)


def compare_content(assignment_dir: Path, pair: dict) -> tuple[str, str]:
    """(left, right) compare text for one pair (shared with detail screens)."""
    overlap = pair.get("token_overlap")
    overlap_lines = (
        {int(line) for line in overlap} if isinstance(overlap, list) else set()
    )
    return (
        _side_lines(assignment_dir, str(pair.get("test_file")), overlap_lines),
        _side_lines(assignment_dir, str(pair.get("reference_file")), overlap_lines),
    )
