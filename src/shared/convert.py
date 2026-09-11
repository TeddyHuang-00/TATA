"""Format conversion helpers for the preprocess stage: ipynb/html/md/docx/pptx/pdf
-> markdown (in-process, nbconvert/markitdown/anydoc).
"""

from __future__ import annotations

import re
import shutil
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

import anydoc
from markitdown import MarkItDown, StreamInfo
from nbconvert import MarkdownExporter

from .assignment_config import InputFormat

SUPPORTED_INPUT_FORMATS: tuple[InputFormat, ...] = (
    "ipynb",
    "html",
    "markdown",
    "docx",
    "pptx",
    "pdf",
    "image",
)

_SUFFIX_FORMATS: dict[str, InputFormat] = {
    ".ipynb": "ipynb",
    ".html": "html",
    ".txt": "html",  # Canvas text-entry bodies arrive as .txt but contain HTML
    ".md": "markdown",
    ".docx": "docx",
    ".pptx": "pptx",
    ".pdf": "pdf",
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
}


def _format_for_suffix(suffix: str) -> InputFormat | None:
    """Map a file suffix to a supported input format (None when unsupported)."""
    return _SUFFIX_FORMATS.get(suffix.lower())


def _clean_filename(filename: str) -> str:
    """Clean filename by replacing spaces and special chars with underscores."""
    # Keep extension, clean stem
    stem = Path(filename).stem
    suffix = Path(filename).suffix

    # Replace non-alphanumeric with underscore, collapse multiple underscores
    clean_stem = re.sub(r"[^a-zA-Z0-9]", "_", stem)
    clean_stem = re.sub(r"_+", "_", clean_stem).strip("_")

    if not clean_stem:
        clean_stem = "file"

    return f"{clean_stem}{suffix}"


def _strip_canvas_suffix(filename: str) -> str:
    """Strip known Canvas-export suffixes from the stem while keeping extension."""
    stem = Path(filename).stem
    suffix = Path(filename).suffix

    cleaned = re.sub(r"_[0-9]+_text$", "", stem)
    cleaned = re.sub(r"_[0-9]+_[0-9]+_.*$", "", cleaned)
    if not cleaned:
        cleaned = "file"
    return f"{cleaned}{suffix}"


def _remove_base64_images(content: str) -> str:
    """Remove base64 encoded images from markdown content."""
    # Pattern matches ![alt](data:image/...base64,...)
    pattern = r"!\[.*?\]\(data:image/[^;]+;base64,[^)]+\)"
    return re.sub(pattern, "", content, flags=re.MULTILINE)


class _TableHTMLParser(HTMLParser):
    """Lightweight HTML table parser for converting DataFrame HTML to markdown tables."""

    def __init__(self) -> None:
        super().__init__()
        self.in_table = False
        self.in_row = False
        self.in_cell = False
        self.current_cell_parts: list[str] = []
        self.current_row: list[str] = []
        self.current_row_is_header = False
        self.rows: list[list[str]] = []
        self.row_is_header: list[bool] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:  # ruff: ignore[unused-method-argument]
        lower = tag.lower()
        if lower == "table":
            self.in_table = True
        elif self.in_table and lower == "tr":
            self.in_row = True
            self.current_row = []
            self.current_row_is_header = False
        elif self.in_row and lower in {"th", "td"}:
            self.in_cell = True
            self.current_cell_parts = []
            if lower == "th":
                self.current_row_is_header = True
        elif self.in_cell and lower == "br":
            self.current_cell_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        if lower == "table":
            self.in_table = False
        elif self.in_row and lower == "tr":
            if self.current_row:
                self.rows.append(self.current_row)
                self.row_is_header.append(self.current_row_is_header)
            self.in_row = False
            self.current_row = []
        elif self.in_cell and lower in {"th", "td"}:
            value = unescape("".join(self.current_cell_parts))
            value = re.sub(r"\s+", " ", value).strip()
            self.current_row.append(value)
            self.in_cell = False
            self.current_cell_parts = []

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.current_cell_parts.append(data)


def _markdown_escape_cell(value: str) -> str:
    escaped = value.replace("|", "\\|")
    return escaped or " "


def _table_html_to_markdown(table_html: str) -> str:
    parser = _TableHTMLParser()
    parser.feed(table_html)

    rows = parser.rows
    if not rows:
        return ""

    max_cols = max(len(r) for r in rows)
    normalized = [r + [""] * (max_cols - len(r)) for r in rows]

    if parser.row_is_header and parser.row_is_header[0]:
        header = normalized[0]
        data_rows = normalized[1:]
    else:
        header = normalized[0]
        data_rows = normalized[1:]

    md_lines = [
        "| " + " | ".join(_markdown_escape_cell(v) for v in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]

    md_lines.extend(
        "| " + " | ".join(_markdown_escape_cell(v) for v in row) + " |"
        for row in data_rows
    )

    return "\n".join(md_lines)


def _convert_html_tables_to_markdown(content: str) -> str:
    table_pattern = re.compile(
        r"<table\b[^>]*>.*?</table>", flags=re.IGNORECASE | re.DOTALL
    )

    def replace_table(match: re.Match[str]) -> str:
        table_html = match.group(0)
        md_table = _table_html_to_markdown(table_html)
        if not md_table:
            return ""
        return f"\n\n{md_table}\n\n"

    return table_pattern.sub(replace_table, content)


def _strip_colab_dataframe_widgets(content: str) -> str:
    processed = content

    # Remove Colab dataframe widget buttons and script payloads.
    processed = re.sub(
        r"<button\b[^>]*class=\"colab-df-[^\"]*\"[^>]*>.*?</button>",
        "",
        processed,
        flags=re.IGNORECASE | re.DOTALL,
    )
    processed = re.sub(
        r"<script\b[^>]*>.*?(google\.colab|convertToInteractive|generateWithVariable).*?</script>",
        "",
        processed,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # Remove occasional leftover SVG fragments from dataframe widgets.
    return re.sub(
        r"<svg\b[^>]*>.*?</svg>",
        "",
        processed,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _normalize_dtype_label_html(content: str) -> str:
    return re.sub(
        r"<br\s*/?>\s*<label>\s*<b>\s*dtype:\s*</b>\s*([^<]+)\s*</label>",
        r"\ndtype: \1",
        content,
        flags=re.IGNORECASE,
    )


def convert_ipynb_to_markdown(
    input_path: Path,
    output_path: Path,
    *,
    template_name: str | None = None,
    template_dir: Path | None = None,
) -> None:
    """Convert Jupyter notebook to markdown using nbconvert MarkdownExporter (in-process)."""
    kwargs: dict = {}
    if template_name:
        kwargs["template_name"] = template_name
    if template_dir:
        kwargs["extra_template_basedirs"] = [str(template_dir)]
    exporter = MarkdownExporter(**kwargs)
    try:
        content, _resources = exporter.from_filename(str(input_path))
    except Exception as exc:
        msg = f"Failed to convert notebook {input_path}: {exc}"
        raise RuntimeError(msg) from exc
    # Clean base64 images and write the converted markdown directly
    content = _remove_base64_images(content)
    output_path.write_text(content, encoding="utf-8")


def convert_html_to_markdown(input_path: Path, output_path: Path) -> None:
    """Convert HTML to markdown using markitdown (in-process)."""
    # Canvas text entries may arrive as .txt while containing HTML; tell
    # markitdown the real extension so it picks its HTML converter.
    stream_info = StreamInfo(extension=".html")
    try:
        content = (
            MarkItDown().convert(str(input_path), stream_info=stream_info).text_content
        )
    except Exception as exc:
        msg = f"Failed to convert HTML {input_path} with markitdown: {exc}"
        raise RuntimeError(msg) from exc
    output_path.write_text(content, encoding="utf-8")


def _convert_markdown(input_path: Path, output_path: Path) -> None:
    """Copy markdown file as-is."""
    shutil.copy2(input_path, output_path)


def convert_docx_to_markdown(input_path: Path, output_path: Path) -> None:
    """Convert docx to markdown with firecrawl-anydoc, falling back to markitdown (both in-process)."""
    try:
        content = anydoc.to_markdown(input_path)
    except Exception as anydoc_exc:
        try:
            content = MarkItDown().convert(str(input_path)).text_content
        except Exception as exc:
            msg = (
                f"Failed to convert docx {input_path}: anydoc failed ({anydoc_exc}); "
                f"markitdown failed ({exc})"
            )
            raise RuntimeError(msg) from exc
    output_path.write_text(content, encoding="utf-8")


def convert_pptx_to_markdown(input_path: Path, output_path: Path) -> None:
    """Convert pptx to markdown with firecrawl-anydoc, falling back to markitdown (both in-process)."""
    try:
        content = anydoc.to_markdown(input_path)
    except Exception as anydoc_exc:
        try:
            content = MarkItDown().convert(str(input_path)).text_content
        except Exception as exc:
            msg = (
                f"Failed to convert pptx {input_path}: anydoc failed ({anydoc_exc}); "
                f"markitdown failed ({exc})"
            )
            raise RuntimeError(msg) from exc
    output_path.write_text(content, encoding="utf-8")


def convert_pdf_to_markdown(input_path: Path, output_path: Path) -> None:
    """Convert PDF to markdown with firecrawl-anydoc (in-process); scanned
    pages trigger automatic hosted OCR (Firecrawl Parse)."""
    try:
        # Local parse first; scanned pages raise NeedsOcrError, which anydoc
        # handles internally by re-sending the document to hosted OCR.
        content = anydoc.to_markdown(input_path, ocr="hosted")
    except anydoc.HostedError as exc:
        # A scanned PDF has no text layer, so the markitdown fallback would
        # yield empty/garbage output: surface the OCR failure instead.
        msg = (
            f"Hosted OCR failed for {input_path}: {exc}. "
            "Check FIRECRAWL_API_KEY in .env (or a local FIRECRAWL_API_URL proxy) if it should use Firecrawl OCR."
        )
        raise RuntimeError(msg) from exc
    except Exception as anydoc_exc:
        try:
            content = MarkItDown().convert(str(input_path)).text_content
        except Exception as exc:
            msg = (
                f"Failed to convert pdf {input_path}: anydoc failed ({anydoc_exc}); "
                f"markitdown failed ({exc})"
            )
            raise RuntimeError(msg) from exc
    output_path.write_text(content, encoding="utf-8")
