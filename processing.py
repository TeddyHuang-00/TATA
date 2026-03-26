from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

from assignment_config import (
    ensure_assignment_dirs,
    load_assignment_file,
    resolve_assignment_paths,
)


InputFormat = Literal["ipynb", "html", "markdown"]
SUPPORTED_INPUT_FORMATS: tuple[InputFormat, ...] = ("ipynb", "html", "markdown")


def _detect_input_format(file_path: Path) -> InputFormat:
    """Auto-detect input format from file extension."""
    suffix = file_path.suffix.lower()
    if suffix == ".ipynb":
        return "ipynb"
    elif suffix == ".html":
        return "html"
    elif suffix == ".md":
        return "markdown"
    else:
        msg = f"Unsupported file extension: {suffix}. Supported: .ipynb, .html, .md"
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


def _convert_ipynb_to_markdown(
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


def _convert_html_to_markdown(input_path: Path, output_path: Path) -> None:
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


def _postprocess_markdown(
    content: str,
    *,
    source_format: InputFormat,
    remove_base64: bool,
    strip_html_callouts: bool,
    strip_html_div_tags: bool,
    strip_html_escaped_backslashes: bool,
) -> str:
    processed = content

    if remove_base64:
        processed = _remove_base64_images(processed)

    if source_format == "html":
        if strip_html_callouts:
            processed = re.sub(r"(?m)^:::.+$\n?", "", processed)
        if strip_html_div_tags:
            processed = re.sub(r"</?div[^>]*>", "", processed)
        if strip_html_escaped_backslashes:
            processed = processed.replace("\\\\", " ")

    return processed


def _process_single_file(
    input_file: Path,
    output_file: Path,
    input_format: InputFormat,
    remove_base64: bool,
    strip_html_callouts: bool,
    strip_html_div_tags: bool,
    strip_html_escaped_backslashes: bool,
    remove_nbconvert_assets: bool,
    nbconvert_template: str | None,
    nbconvert_template_dir: Path | None,
) -> None:
    """Process a single input file to markdown output."""
    # Convert based on format
    if input_format == "ipynb":
        _convert_ipynb_to_markdown(
            input_file,
            output_file,
            template_name=nbconvert_template,
            template_dir=nbconvert_template_dir,
        )
    elif input_format == "html":
        _convert_html_to_markdown(input_file, output_file)
    elif input_format == "markdown":
        _convert_markdown(input_file, output_file)
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
        )

        output_file.write_text(content, encoding="utf-8")

    if input_format == "ipynb" and remove_nbconvert_assets:
        assets_dir = output_file.parent / f"{output_file.stem}_files"
        if assets_dir.exists() and assets_dir.is_dir():
            shutil.rmtree(assets_dir)


def _glob_for_format(raw_dir: Path, input_format: InputFormat) -> list[Path]:
    pattern_map = {
        "ipynb": "*.ipynb",
        "html": "*.html",
        "markdown": "*.md",
    }
    return sorted(raw_dir.glob(pattern_map[input_format]))


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


def preprocess_assignment(assignment_config_path: Path) -> None:
    """Preprocess all raw files for an assignment into processed markdown."""
    if not assignment_config_path.exists():
        msg = f"Assignment config not found: {assignment_config_path}"
        raise FileNotFoundError(msg)

    cfg = load_assignment_file(assignment_config_path)
    processing = cfg.processing

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
            if p.is_file() and p.suffix.lower() in {".ipynb", ".html", ".md"}
        ]
        if not supported_files:
            print(
                "No supported files found in raw directory: "
                f"{raw_dir}\n"
                "Add student files to raw/ (supported: .ipynb, .html, .md), "
                "then run preprocess again."
            )
            return

        first_file = supported_files[0]
        detected_input_format = _detect_input_format(first_file)
        configured_formats = [detected_input_format]
        print(
            f"Auto-detected input format: {detected_input_format} "
            f"(from {first_file.name})"
        )

    # Processing options
    remove_base64 = processing.remove_base64_images
    clean_filenames = processing.clean_filenames
    strip_canvas_suffix = processing.strip_canvas_suffix
    strip_html_callouts = processing.strip_html_callouts
    strip_html_div_tags = processing.strip_html_div_tags
    strip_html_escaped_backslashes = processing.strip_html_escaped_backslashes
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
        for p in _glob_for_format(raw_dir, fmt):
            raw_file_map[p] = fmt

    raw_files = sorted(raw_file_map.keys())

    if not raw_files:
        print(
            "No files found for configured input format(s) "
            f"{configured_formats} in: {raw_dir}\n"
            "Check [processing.input_format] in config or place matching files in raw/."
        )
        return

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

            try:
                _process_single_file(
                    raw_file,
                    output_file,
                    file_format,
                    remove_base64,
                    strip_html_callouts,
                    strip_html_div_tags,
                    strip_html_escaped_backslashes,
                    remove_nbconvert_assets,
                    nbconvert_template,
                    template_dir_path,
                )
                print(f"[processed] {raw_file.name} -> {output_file.name}")
            except Exception as exc:
                print(f"[error] Failed to process {raw_file.name}: {exc}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run preprocessing pipeline.")
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to assignment config TOML.",
    )
    args = parser.parse_args()

    preprocess_assignment(args.config)


if __name__ == "__main__":
    main()
