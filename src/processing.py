from __future__ import annotations

import re
import shutil
import subprocess
import sys
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal

from .assignment_config import (
    ensure_assignment_dirs,
    load_assignment_file,
    resolve_assignment_paths,
)
from .cli_options import ConfigFileCliOptions, parse_cli_args
from .hooks_runtime import HookRuntime

InputFormat = Literal["ipynb", "html", "markdown", "docx"]
SUPPORTED_INPUT_FORMATS: tuple[InputFormat, ...] = ("ipynb", "html", "markdown", "docx")


class ProcessingCliOptions(ConfigFileCliOptions):
    pass


def _detect_input_format(file_path: Path) -> InputFormat:
    """Auto-detect input format from file extension."""
    suffix = file_path.suffix.lower()
    if suffix == ".ipynb":
        return "ipynb"
    if suffix in {
        ".html",
        ".txt",
    }:  # Canvas text-entry bodies are saved as .txt but contain HTML
        return "html"
    if suffix == ".md":
        return "markdown"
    if suffix == ".docx":
        return "docx"
    msg = f"Unsupported file extension: {suffix}. Supported: .ipynb, .html, .txt, .md, .docx"
    raise ValueError(msg)


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
    """Convert Jupyter notebook to markdown using nbconvert."""
    cmd = [
        sys.executable,
        "-m",
        "jupyter",
        "nbconvert",
        "--to",
        "markdown",
        "--output",
        output_path.name,
        "--output-dir",
        str(output_path.parent),
    ]

    if template_name:
        cmd.extend(["--template", template_name])

    if template_dir:
        cmd.append(f"--TemplateExporter.extra_template_basedirs={template_dir}")

    cmd.append(
        str(input_path),
    )

    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        msg = f"Failed to convert notebook {input_path}: {e.stderr}"
        raise RuntimeError(msg) from e

    # Clean up the output file
    if output_path.exists():
        content = output_path.read_text(encoding="utf-8")
        # Remove base64 images if present
        content = _remove_base64_images(content)
        output_path.write_text(content, encoding="utf-8")


def convert_html_to_markdown(input_path: Path, output_path: Path) -> None:
    """Convert HTML to markdown using pandoc."""
    cmd = [
        "pandoc",
        "-f",
        "html",
        "-t",
        "markdown",
        str(input_path),
        "-o",
        str(output_path),
    ]

    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        msg = f"Failed to convert HTML {input_path}: pandoc not found or conversion failed. {e.stderr}"
        raise RuntimeError(msg) from e


def _convert_markdown(input_path: Path, output_path: Path) -> None:
    """Copy markdown file as-is."""
    shutil.copy2(input_path, output_path)


def convert_docx_to_markdown(input_path: Path, output_path: Path) -> None:
    """Convert docx to markdown with system anydoc, falling back to markitdown."""
    if shutil.which("anydoc"):
        cmd = ["anydoc", str(input_path), "-o", str(output_path)]
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            return
        except subprocess.CalledProcessError as e:
            msg = f"anydoc failed on {input_path}: {e.stderr}"
            raise RuntimeError(msg) from e
    if (
        shutil.which("markitdown")
        or (Path(sys.executable).parent / "markitdown").exists()
    ):
        cmd = [
            sys.executable,
            "-m",
            "markitdown",
            str(input_path),
            "-o",
            str(output_path),
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            return
        except subprocess.CalledProcessError as e:
            msg = f"markitdown failed on {input_path}: {e.stderr}"
            raise RuntimeError(msg) from e
    msg = "Neither anydoc nor markitdown found. Install anydoc (system) to convert .docx files."
    raise RuntimeError(msg)


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


def _glob_for_format(raw_dir: Path, input_format: InputFormat) -> list[Path]:
    pattern_map = {
        "ipynb": ("*.ipynb",),
        "html": ("*.html", "*.txt"),
        "markdown": ("*.md",),
        "docx": ("*.docx",),
    }
    files = [p for pat in pattern_map[input_format] for p in raw_dir.glob(pat)]
    return sorted(files)


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
    """Preprocess all raw files for an assignment into processed markdown."""
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

    # Get input format(s) (auto-detect from first supported file or config override)
    configured_formats = _normalize_input_formats(processing.input_format)

    if configured_formats is None:
        supported_files = [
            p
            for p in sorted(raw_dir.glob("*"))
            if p.is_file()
            and p.suffix.lower() in {".ipynb", ".html", ".txt", ".md", ".docx"}
        ]
        if not supported_files:
            print(
                "No supported files found in raw directory: "
                f"{raw_dir}\n"
                "Add student files to raw/ (supported: .ipynb, .html, .txt, .md, .docx), "
                "then run preprocess again."
            )
            return {
                "stage": "preprocess",
                "success": 0,
                "errors": 0,
                "total": 0,
                "success_rate": 0,
            }

        first_file = supported_files[0]
        detected_input_format = _detect_input_format(first_file)
        configured_formats = [detected_input_format]
        print(
            f"Auto-detected input format: {detected_input_format} "
            f"(from {first_file.name})"
        )

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

    default_template_dir = assignment_config_path.parents[2] / "templates"
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

    # Process each file
    raw_file_map: dict[Path, InputFormat] = {}
    for fmt in configured_formats:
        assert fmt in SUPPORTED_INPUT_FORMATS
        for p in _glob_for_format(raw_dir, fmt):
            raw_file_map[p] = fmt

    raw_files = sorted(raw_file_map.keys())

    if not raw_files:
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

    processed_count = 0
    failed_count = 0

    for raw_file in raw_files:
        if raw_file.is_file():
            # Determine output filename
            output_name = raw_file.name
            if strip_canvas_suffix:
                output_name = _strip_canvas_suffix(output_name)
            if clean_filenames:
                output_name = _clean_filename(output_name)

            # Ensure .md extension
            output_stem = Path(output_name).stem
            output_file = processed_dir / f"{output_stem}.md"

            # Detect format for this specific file
            file_format = raw_file_map.get(raw_file) or _detect_input_format(raw_file)

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
