from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import tomllib


InputFormat = Literal["ipynb", "html", "markdown"]


@dataclass
class ProcessingConfig:
    raw_dir: Path
    processed_dir: Path
    input_format: InputFormat
    indent_level: int = 4
    remove_base64_images: bool = True
    clean_filenames: bool = True


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


def _remove_base64_images(content: str) -> str:
    """Remove base64 encoded images from markdown content."""
    # Pattern matches ![alt](data:image/...base64,...)
    pattern = r"!\[.*?\]\(data:image/[^;]+;base64,[^)]+\)"
    return re.sub(pattern, "", content, flags=re.MULTILINE)


def _convert_ipynb_to_markdown(input_path: Path, output_path: Path) -> None:
    """Convert Jupyter notebook to markdown using nbconvert."""
    cmd = [
        "python", "-m", "nbconvert",
        "--to", "markdown",
        "--output", output_path.name,
        "--output-dir", str(output_path.parent),
        str(input_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        # nbconvert outputs to stdout sometimes, but we use --output-dir
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
    cmd = ["pandoc", "-f", "html", "-t", "markdown", str(input_path), "-o", str(output_path)]

    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        msg = f"Failed to convert HTML {input_path}: pandoc not found or conversion failed. {e.stderr}"
        raise RuntimeError(msg) from e


def _convert_markdown(input_path: Path, output_path: Path) -> None:
    """Copy markdown file as-is."""
    shutil.copy2(input_path, output_path)


def _process_single_file(
    input_file: Path,
    output_file: Path,
    input_format: InputFormat,
    remove_base64: bool,
) -> None:
    """Process a single input file to markdown output."""
    # Convert based on format
    if input_format == "ipynb":
        _convert_ipynb_to_markdown(input_file, output_file)
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

        if remove_base64:
            content = _remove_base64_images(content)

        output_file.write_text(content, encoding="utf-8")


def preprocess_assignment(assignment_config_path: Path) -> None:
    """Preprocess all raw files for an assignment into processed markdown."""
    if not assignment_config_path.exists():
        msg = f"Assignment config not found: {assignment_config_path}"
        raise FileNotFoundError(msg)

    # Load assignment config
    cfg = tomllib.loads(assignment_config_path.read_text(encoding="utf-8"))

    assignment = cfg.get("assignment", {})
    processing = cfg.get("processing", {})

    # Determine paths
    raw_dir = (assignment_config_path.parent / assignment.get("raw_dir", "raw")).resolve()
    processed_dir = (assignment_config_path.parent / assignment.get("processed_dir", "processed")).resolve()

    if not raw_dir.exists():
        msg = f"Raw directory not found: {raw_dir}"
        raise FileNotFoundError(msg)

    processed_dir.mkdir(parents=True, exist_ok=True)

    # Get input format (auto-detect from first file or config override)
    input_format = processing.get("input_format")
    if input_format is None:
        # Auto-detect from first file
        raw_files = list(raw_dir.glob("*"))
        if not raw_files:
            print(f"No files found in raw directory: {raw_dir}")
            return

        first_file = raw_files[0]
        input_format = _detect_input_format(first_file)
        print(f"Auto-detected input format: {input_format} (from {first_file.name})")

    # Processing options
    remove_base64 = processing.get("remove_base64_images", True)
    clean_filenames = processing.get("clean_filenames", True)

    # Process each file
    raw_files = sorted(raw_dir.glob("*"))
    for raw_file in raw_files:
        if raw_file.is_file():
            # Determine output filename
            output_name = raw_file.name
            if clean_filenames:
                output_name = _clean_filename(output_name)

            # Ensure .md extension
            output_stem = Path(output_name).stem
            output_file = processed_dir / f"{output_stem}.md"

            # Detect format for this specific file
            file_format = _detect_input_format(raw_file)

            try:
                _process_single_file(raw_file, output_file, file_format, remove_base64)
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