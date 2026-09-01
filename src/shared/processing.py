from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal

import anydoc
from markitdown import MarkItDown, StreamInfo
from nbconvert import MarkdownExporter

from src import REPO_ROOT

from .assignment_config import (
    ensure_assignment_dirs,
    load_assignment_file,
    resolve_assignment_paths,
)
from .cli_options import ConfigFileCliOptions, parse_cli_args
from .hooks_runtime import HookRuntime

InputFormat = Literal["ipynb", "html", "markdown", "docx", "pdf"]
SUPPORTED_INPUT_FORMATS: tuple[InputFormat, ...] = (
    "ipynb",
    "html",
    "markdown",
    "docx",
    "pdf",
)

_SUFFIX_FORMATS: dict[str, InputFormat] = {
    ".ipynb": "ipynb",
    ".html": "html",
    ".txt": "html",  # Canvas text-entry bodies arrive as .txt but contain HTML
    ".md": "markdown",
    ".docx": "docx",
    ".pdf": "pdf",
}


class ProcessingCliOptions(ConfigFileCliOptions):
    pass


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


def convert_pdf_to_markdown(input_path: Path, output_path: Path) -> None:
    """Convert pdf to markdown with firecrawl-anydoc, falling back to markitdown (both in-process)."""
    try:
        content = anydoc.to_markdown(input_path)
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


def _render_docx_screenshots(
    input_file: Path,
    output_stem: str,
    processed_dir: Path,
    pages: int,
) -> None:
    """Render docx to page PNGs (via soffice+pdftoppm) for multimodal grading."""
    if not shutil.which("soffice") or not shutil.which("pdftoppm"):
        print(f"[screenshots] skipped {input_file.name}: soffice/pdftoppm not found")
        return
    shots_dir = processed_dir / "screenshots"
    pdf_dir = shots_dir / "_pdf"
    shots_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir.mkdir(exist_ok=True)
    try:
        subprocess.run(
            [
                "soffice",
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(pdf_dir),
                str(input_file),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"[screenshots] soffice failed for {input_file.name}: {e.stderr}")
        return
    pdf_path = pdf_dir / f"{input_file.stem}.pdf"
    if not pdf_path.exists():
        print(f"[screenshots] no PDF produced for {input_file.name}")
        return
    subprocess.run(
        [
            "pdftoppm",
            "-png",
            "-r",
            "100",
            "-f",
            "1",
            "-l",
            str(pages),
            str(pdf_path),
            str(shots_dir / output_stem),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    for f in sorted(shots_dir.glob(f"{output_stem}-*.png")):
        page = f.name.rsplit("-", 1)[-1].split(".")[0]
        f.rename(shots_dir / f"{output_stem}_p{page}.png")
    shutil.rmtree(pdf_dir, ignore_errors=True)
    print(
        f"[screenshots] rendered {len(list(shots_dir.glob(f'{output_stem}_p*.png')))} page(s) for {output_stem}"
    )


def _postprocess_markdown(  # ruff: ignore[too-many-arguments]
    content: str,
    *,
    source_format: InputFormat,
    remove_base64: bool,
    strip_html_callouts: bool,
    strip_html_div_tags: bool,
    strip_html_escaped_backslashes: bool,
    strip_html_style_blocks: bool,
    convert_html_tables_to_markdown: bool,
    strip_colab_dataframe_widgets: bool,
    strip_html_script_tags: bool,
    strip_html_button_tags: bool,
    strip_html_svg_tags: bool,
    normalize_dtype_label_html: bool,
) -> str:
    processed = content

    if remove_base64:
        processed = _remove_base64_images(processed)

    if source_format in {"html", "ipynb"}:
        if strip_html_style_blocks:
            processed = re.sub(
                r"<style\b[^>]*>.*?</style>",
                "",
                processed,
                flags=re.IGNORECASE | re.DOTALL,
            )
        if convert_html_tables_to_markdown:
            processed = _convert_html_tables_to_markdown(processed)
        if strip_colab_dataframe_widgets:
            processed = _strip_colab_dataframe_widgets(processed)
        if strip_html_script_tags:
            processed = re.sub(
                r"<script\b[^>]*>.*?</script>",
                "",
                processed,
                flags=re.IGNORECASE | re.DOTALL,
            )
        if strip_html_button_tags:
            processed = re.sub(
                r"<button\b[^>]*>.*?</button>",
                "",
                processed,
                flags=re.IGNORECASE | re.DOTALL,
            )
        if strip_html_svg_tags:
            processed = re.sub(
                r"<svg\b[^>]*>.*?</svg>",
                "",
                processed,
                flags=re.IGNORECASE | re.DOTALL,
            )
        if strip_html_callouts:
            processed = re.sub(r"(?m)^:::.+$\n?", "", processed)
        if strip_html_div_tags:
            processed = re.sub(r"</?div[^>]*>", "", processed)
        if strip_html_escaped_backslashes:
            processed = processed.replace("\\\\", " ")
        if normalize_dtype_label_html:
            processed = _normalize_dtype_label_html(processed)

        # Compact excessive empty lines after stripping bulky HTML blocks.
        processed = re.sub(r"\n{3,}", "\n\n", processed)

    return processed


def _process_single_file(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
    input_file: Path,
    output_file: Path,
    input_format: InputFormat,
    remove_base64: bool,
    strip_html_callouts: bool,
    strip_html_div_tags: bool,
    strip_html_escaped_backslashes: bool,
    strip_html_style_blocks: bool,
    convert_html_tables_to_markdown: bool,
    strip_colab_dataframe_widgets: bool,
    strip_html_script_tags: bool,
    strip_html_button_tags: bool,
    strip_html_svg_tags: bool,
    normalize_dtype_label_html: bool,
    remove_nbconvert_assets: bool,
    nbconvert_template: str | None,
    nbconvert_template_dir: Path | None,
) -> None:
    """Process a single input file to markdown output."""
    # Convert based on format
    if input_format == "ipynb":
        convert_ipynb_to_markdown(
            input_file,
            output_file,
            template_name=nbconvert_template,
            template_dir=nbconvert_template_dir,
        )
    elif input_format == "html":
        convert_html_to_markdown(input_file, output_file)
    elif input_format == "markdown":
        _convert_markdown(input_file, output_file)
    elif input_format == "docx":
        convert_docx_to_markdown(input_file, output_file)
    elif input_format == "pdf":
        convert_pdf_to_markdown(input_file, output_file)
    else:
        msg = f"Unsupported input format: {input_format}"
        raise ValueError(msg)

    # Post-processing
    if output_file.exists():
        content = output_file.read_text(encoding="utf-8")
        content = _postprocess_markdown(
            content,
            source_format=input_format,
            remove_base64=remove_base64,
            strip_html_callouts=strip_html_callouts,
            strip_html_div_tags=strip_html_div_tags,
            strip_html_escaped_backslashes=strip_html_escaped_backslashes,
            strip_html_style_blocks=strip_html_style_blocks,
            convert_html_tables_to_markdown=convert_html_tables_to_markdown,
            strip_colab_dataframe_widgets=strip_colab_dataframe_widgets,
            strip_html_script_tags=strip_html_script_tags,
            strip_html_button_tags=strip_html_button_tags,
            strip_html_svg_tags=strip_html_svg_tags,
            normalize_dtype_label_html=normalize_dtype_label_html,
        )

        output_file.write_text(content, encoding="utf-8")

    if input_format == "ipynb" and remove_nbconvert_assets:
        assets_dir = output_file.parent / f"{output_file.stem}_files"
        if assets_dir.exists() and assets_dir.is_dir():
            shutil.rmtree(assets_dir)


def _iter_raw_items(raw_dir: Path) -> list[Path]:
    """Top-level raw entries: files and dirs, dot-entries (e.g.
    .fetch-cache.json) skipped. A top-level file whose base uid (stem with
    any _N/_LATE_N suffix stripped) names a top-level dir is a stale flat
    leftover of a folderized student (mixed legacy layout): skipped so the
    student isn't double-processed."""
    entries = sorted(
        p
        for p in raw_dir.iterdir()
        if not p.name.startswith(".") and (p.is_file() or p.is_dir())
    )
    dirs = {p.name for p in entries if p.is_dir()}
    return [
        p
        for p in entries
        if not (p.is_file() and re.sub(r"_(?:LATE_)?\d+$", "", p.stem) in dirs)
    ]


def _submission_stamp(cache: dict[str, str], raw_file: Path) -> str:
    """Stamp for a raw file: the .fetch-cache.json entry when known, else
    the file's mtime, else '' — the submitted header part is omitted."""
    stamp = cache.get(raw_file.name)
    if stamp is not None:
        return stamp
    try:
        modified = raw_file.stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(modified, tz=UTC).isoformat(timespec="seconds")


def _normalize_input_formats(
    input_format_config: InputFormat | list[InputFormat] | None,
) -> list[InputFormat] | None:
    if input_format_config is None:
        return None

    if isinstance(input_format_config, list):
        deduped: list[InputFormat] = []
        seen: set[str] = set()
        for fmt in input_format_config:
            if fmt not in seen:
                deduped.append(fmt)
                seen.add(fmt)
        return deduped

    return [input_format_config]


def preprocess_assignment(assignment_config_path: Path) -> dict | None:  # ruff: ignore[too-many-branches, too-many-statements, too-many-locals]
    """Preprocess all raw files for an assignment into processed markdown.

    Top-level raw entries are per-student: a file (single submission) or a
    folder (multi-file student). Folders are concatenated into one
    ``<folder>.md`` with a per-file header (``file:``, ``submitted:`` when the
    stamp is known), and before/after_preprocess_file hooks fire per input
    file with output_file set to the final concatenated file.
    """
    cfg = load_assignment_file(assignment_config_path)
    processing = cfg.processing
    hook_runtime = HookRuntime.from_config(
        cfg,
        assignment_config_path=assignment_config_path,
    )

    paths = resolve_assignment_paths(cfg, assignment_config_path.parent)
    ensure_assignment_dirs(paths)

    raw_dir = paths.raw_dir
    processed_dir = paths.processed_dir

    processed_dir.mkdir(parents=True, exist_ok=True)

    # Determine input format(s): auto (per-file by suffix, no config) or an
    # explicit [processing.input_format] list.
    configured_formats = _normalize_input_formats(processing.input_format)

    items = _iter_raw_items(raw_dir)

    def item_files(item: Path) -> list[tuple[Path, InputFormat]]:
        """Supported files of one raw item: a top-level file itself, or the
        files inside a folder (sorted by name), filtered by the configured
        formats when set. Unsupported files inside a folder are logged as
        skips instead of being dropped silently."""
        files = (
            [item]
            if item.is_file()
            else sorted(
                (p for p in item.iterdir() if p.is_file()),
                key=lambda p: (
                    # unsuffixed-uid member (the body: <uid>.html) first,
                    # then _N/_LATE_N members; stable by name within a group.
                    0 if re.sub(r"_(?:LATE_)?\d+$", "", p.stem) == p.stem else 1,
                    p.name,
                ),
            )
        )
        found: list[tuple[Path, InputFormat]] = []
        for f in files:
            fmt = _format_for_suffix(f.suffix)
            if fmt is None:
                if item.is_dir():
                    print(f"[skip] {f.name} (unsupported format)")
                continue
            if configured_formats is not None and fmt not in configured_formats:
                continue
            found.append((f, fmt))
        return found

    item_files_by = {item: item_files(item) for item in items}
    if not any(item_files_by.values()):
        if configured_formats is None:
            print(
                "No supported files found in raw directory: "
                f"{raw_dir}\n"
                "Add student files to raw/ (supported: .ipynb, .html, .txt, .md, .docx, .pdf), "
                "then run preprocess again."
            )
        else:
            print(
                "No files found for configured input format(s) "
                f"{configured_formats} in: {raw_dir}\n"
                "Check [processing.input_format] in config or place matching files in raw/."
            )
        return {
            "stage": "preprocess",
            "success": 0,
            "errors": 0,
            "total": 0,
            "success_rate": 0,
        }

    if hook_runtime is not None:
        hook_runtime.run(
            "before_preprocess",
            {
                "assignment_config": str(assignment_config_path),
                "raw_dir": str(raw_dir),
                "processed_dir": str(processed_dir),
                "configured_formats": configured_formats,
            },
        )

    # Processing options
    remove_base64 = processing.remove_base64_images
    clean_filenames = processing.clean_filenames
    strip_canvas_suffix = processing.strip_canvas_suffix
    strip_html_callouts = processing.strip_html_callouts
    strip_html_div_tags = processing.strip_html_div_tags
    strip_html_escaped_backslashes = processing.strip_html_escaped_backslashes
    strip_html_style_blocks = processing.strip_html_style_blocks
    convert_html_tables_to_markdown = processing.convert_html_tables_to_markdown
    strip_colab_dataframe_widgets = processing.strip_colab_dataframe_widgets
    strip_html_script_tags = processing.strip_html_script_tags
    strip_html_button_tags = processing.strip_html_button_tags
    strip_html_svg_tags = processing.strip_html_svg_tags
    normalize_dtype_label_html = processing.normalize_dtype_label_html
    remove_nbconvert_assets = processing.remove_nbconvert_assets
    nbconvert_template = processing.nbconvert_template

    default_template_dir = REPO_ROOT / "templates"
    nbconvert_template_dir = processing.nbconvert_template_dir
    if nbconvert_template_dir is not None:
        template_dir_path = (
            assignment_config_path.parent / nbconvert_template_dir
        ).resolve()
    elif default_template_dir.exists() and (default_template_dir / "mdoutput").exists():
        template_dir_path = default_template_dir.resolve()
        if nbconvert_template is None:
            nbconvert_template = "mdoutput"
    else:
        template_dir_path = None

    # Process each raw item (per-student): a file (single submission) or a
    # folder (multi-file student, concatenated into one per-student md).
    cache: dict[str, str] = {}
    cache_path = raw_dir / ".fetch-cache.json"
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}

    processed_count = 0
    failed_count = 0

    for item in items:
        files = item_files_by[item]
        if not files:
            if item.is_dir():
                print(f"[skip] folder {item.name} (no supported files)")
            continue
        if item.is_file():
            raw_file, file_format = files[0]
            # Determine output filename
            output_name = raw_file.name
            if strip_canvas_suffix:
                output_name = _strip_canvas_suffix(output_name)
            if clean_filenames:
                output_name = _clean_filename(output_name)

            # Ensure .md extension
            output_stem = Path(output_name).stem
            output_file = processed_dir / f"{output_stem}.md"

            if hook_runtime is not None:
                before_payload = hook_runtime.run(
                    "before_preprocess_file",
                    {
                        "assignment_config": str(assignment_config_path),
                        "input_file": str(raw_file),
                        "output_file": str(output_file),
                        "input_format": file_format,
                    },
                )
                input_file = Path(before_payload.get("input_file", str(raw_file)))
                output_file = Path(before_payload.get("output_file", str(output_file)))
                file_format = str(before_payload.get("input_format", file_format))
            else:
                input_file = raw_file

            try:  # ruff: ignore[too-many-statements-in-try-clause]
                assert file_format in SUPPORTED_INPUT_FORMATS, (
                    f"Unsupported input format: {file_format}. "
                    f"Must be one of: {SUPPORTED_INPUT_FORMATS}"
                )
                _process_single_file(
                    input_file,
                    output_file,
                    file_format,  # ty:ignore[invalid-argument-type]
                    remove_base64,
                    strip_html_callouts,
                    strip_html_div_tags,
                    strip_html_escaped_backslashes,
                    strip_html_style_blocks,
                    convert_html_tables_to_markdown,
                    strip_colab_dataframe_widgets,
                    strip_html_script_tags,
                    strip_html_button_tags,
                    strip_html_svg_tags,
                    normalize_dtype_label_html,
                    remove_nbconvert_assets,
                    nbconvert_template,
                    template_dir_path,
                )
                print(f"[processed] {raw_file.name} -> {output_file.name}")
                processed_count += 1
                if processing.render_screenshots and file_format == "docx":
                    _render_docx_screenshots(
                        input_file,
                        output_stem,
                        processed_dir,
                        processing.screenshot_pages,
                    )
                if hook_runtime is not None:
                    hook_runtime.run(
                        "after_preprocess_file",
                        {
                            "assignment_config": str(assignment_config_path),
                            "input_file": str(raw_file),
                            "output_file": str(output_file),
                            "input_format": file_format,
                            "success": True,
                        },
                    )
            except Exception as exc:
                print(f"[error] Failed to process {raw_file.name}: {exc}")
                failed_count += 1
                if hook_runtime is not None:
                    hook_runtime.run(
                        "after_preprocess_file",
                        {
                            "assignment_config": str(assignment_config_path),
                            "input_file": str(raw_file),
                            "output_file": str(output_file),
                            "input_format": file_format,
                            "success": False,
                            "error": str(exc),
                        },
                    )
        else:
            # Multi-file student folder: convert each supported file to a
            # temp md, then concatenate into one <folder>.md with per-file
            # headers (file:, submitted: when the stamp is known). Hooks fire
            # per input file but always report the final concatenated file as
            # output_file.
            output_file = processed_dir / f"{item.name}.md"
            parts: list[str] = []
            converted = 0
            for raw_file, fmt in files:
                tmp_file: Path | None = None
                input_file = raw_file
                file_format = fmt
                try:  # ruff: ignore[too-many-statements-in-try-clause]
                    if hook_runtime is not None:
                        before_payload = hook_runtime.run(
                            "before_preprocess_file",
                            {
                                "assignment_config": str(assignment_config_path),
                                "input_file": str(raw_file),
                                "output_file": str(output_file),
                                "input_format": file_format,
                            },
                        )
                        input_file = Path(
                            before_payload.get("input_file", str(raw_file))
                        )
                        output_file = Path(
                            before_payload.get("output_file", str(output_file))
                        )
                        file_format = str(
                            before_payload.get("input_format", file_format)
                        )
                    assert file_format in SUPPORTED_INPUT_FORMATS, (
                        f"Unsupported input format: {file_format}. "
                        f"Must be one of: {SUPPORTED_INPUT_FORMATS}"
                    )
                    fd, tmp_name = tempfile.mkstemp(suffix=".md", dir=processed_dir)
                    os.close(fd)
                    tmp_file = Path(tmp_name)
                    _process_single_file(
                        input_file,
                        tmp_file,
                        file_format,  # ty:ignore[invalid-argument-type]
                        remove_base64,
                        strip_html_callouts,
                        strip_html_div_tags,
                        strip_html_escaped_backslashes,
                        strip_html_style_blocks,
                        convert_html_tables_to_markdown,
                        strip_colab_dataframe_widgets,
                        strip_html_script_tags,
                        strip_html_button_tags,
                        strip_html_svg_tags,
                        normalize_dtype_label_html,
                        remove_nbconvert_assets,
                        nbconvert_template,
                        template_dir_path,
                    )
                    text = tmp_file.read_text(encoding="utf-8")
                    stamp = _submission_stamp(cache, raw_file)
                    submitted = f", submitted: {stamp}" if stamp else ""
                    parts.append(
                        f"---\n<!--- file: {raw_file.name}{submitted} -->\n\n{text}"
                    )
                    output_file.write_text("\n".join(parts), encoding="utf-8")
                    converted += 1
                    print(f"[processed] {raw_file.name} -> {output_file.name}")
                    if hook_runtime is not None:
                        hook_runtime.run(
                            "after_preprocess_file",
                            {
                                "assignment_config": str(assignment_config_path),
                                "input_file": str(raw_file),
                                "output_file": str(output_file),
                                "input_format": file_format,
                                "success": True,
                            },
                        )
                except Exception as exc:
                    print(f"[error] Failed to process {raw_file.name}: {exc}")
                    failed_count += 1
                    if hook_runtime is not None:
                        hook_runtime.run(
                            "after_preprocess_file",
                            {
                                "assignment_config": str(assignment_config_path),
                                "input_file": str(raw_file),
                                "output_file": str(output_file),
                                "input_format": file_format,
                                "success": False,
                                "error": str(exc),
                            },
                        )
                finally:
                    if tmp_file is not None:
                        tmp_file.unlink(missing_ok=True)
            if converted:
                processed_count += 1

    if hook_runtime is not None:
        hook_runtime.run(
            "after_preprocess",
            {
                "assignment_config": str(assignment_config_path),
                "raw_dir": str(raw_dir),
                "processed_dir": str(processed_dir),
                "processed_count": processed_count,
                "failed_count": failed_count,
            },
        )

    total = processed_count + failed_count
    success_rate = (processed_count / total * 100) if total > 0 else 0
    return {
        "stage": "preprocess",
        "success": processed_count,
        "errors": failed_count,
        "total": total,
        "success_rate": success_rate,
    }


def main() -> None:
    args = parse_cli_args(ProcessingCliOptions)

    preprocess_assignment(args.config)


if __name__ == "__main__":
    main()
