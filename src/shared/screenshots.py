"""Screenshot rendering for visual evaluation: docx/pdf/image via
soffice->pdftoppm and ipynb via embedded-image extraction.
"""

from __future__ import annotations

import base64
import re
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageOps

from .assignment_config import InputFormat


def _image_to_pdf(input_path: Path, out_pdf: Path) -> None:
    """Raster the image into a single-page PDF; Firecrawl Parse only accepts PDFs."""
    with Image.open(input_path) as src:
        img = ImageOps.exif_transpose(src)
        if img.mode in {"RGBA", "LA"} or (
            img.mode == "P" and "transparency" in img.info
        ):
            # Composite transparent pixels on white before flattening: PIL defaults to black.
            img = Image.alpha_composite(
                Image.new("RGBA", img.size, "white"), img.convert("RGBA")
            )
        img.convert("RGB").save(out_pdf, "PDF", resolution=150)


def _extract_embedded_images(
    md_text: str, output_stem: str, shots_dir: Path, img_offset: int = 0
) -> int:
    """Save inline base64 images from markdown as ``{output_stem}_i{n}.png``
    (n from ``img_offset`` so folder members continue the same stem's
    numbering) in ``shots_dir``, stripping the image links from the returned
    copy. Returns how many images were saved.

    Matches ``![alt](data:image/<png|jpeg|gif>;base64,<b64>)`` with any alt
    text; the payload runs to the closing ``)`` (no nested parens). Invalid
    payloads are left in place — the regular base64 cleanup drops them from
    the written md anyway.
    """
    shots_dir.mkdir(parents=True, exist_ok=True)
    n = 0

    def save_image(match: re.Match[str]) -> str:
        nonlocal n
        try:
            data = base64.b64decode(match.group(1))
        except Exception:
            return match.group(0)
        (shots_dir / f"{output_stem}_i{img_offset + n}.png").write_bytes(data)
        n += 1
        return ""

    re.sub(
        r"!\[.*?\]\(data:image/(?:png|jpeg|gif);base64,([^)]+)\)",
        save_image,
        md_text,
    )
    return n


def _shift_page(page: str, offset: int) -> str:
    """Page name with the folder-member ``offset`` applied. Keeps
    pdftoppm's zero-padding width (offset 0 -> byte-identical to the old
    naming) so string-sorted ``_pN`` stays in page order.
    ponytail: width = this member's own render width; cross-member width
    mismatch (3-page member next to 12-page member) stays a known edge —
    strictly better than the pre-fix name collision."""
    if offset == 0:
        return page
    return f"{offset + int(page):0{len(page)}d}"


def _pdftoppm_pages(
    pdf_path: Path, output_stem: str, shots_dir: Path, page_offset: int = 0
) -> int:
    """Raster every page of ``pdf_path`` to ``{output_stem}_pN.png`` (no page
    limit) and print a summary. Returns the number of pages rendered."""
    try:
        subprocess.run(
            [
                "pdftoppm",
                "-png",
                "-r",
                "100",
                str(pdf_path),
                str(shots_dir / output_stem),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"[screenshots] pdftoppm failed for {pdf_path.name}: {e.stderr}")
        return 0
    rendered = sorted(shots_dir.glob(f"{output_stem}-*.png"))
    for f in rendered:
        page = f.name.rsplit("-", 1)[-1].split(".")[0]
        f.rename(shots_dir / f"{output_stem}_p{_shift_page(page, page_offset)}.png")
    print(f"[screenshots] rendered {len(rendered)} page(s) for {output_stem}")
    return len(rendered)


def _render_screenshots(  # ruff: ignore[too-many-return-statements, too-many-arguments, too-many-positional-arguments, too-many-branches]
    input_file: Path,
    output_stem: str,
    processed_dir: Path,
    file_format: InputFormat,
    template_name: str | None = None,
    template_dir: Path | None = None,
    page_offset: int = 0,
    img_offset: int = 0,
) -> tuple[int, int]:
    """Render screenshots for visual evaluation (best-effort, never raises):
    docx/pdf -> one PNG per page (all pages, no truncation), image -> one
    PNG via PIL, ipynb -> embedded base64 images saved from the converted
    markdown. ``page_offset``/``img_offset`` shift the numbering so folder
    members continue the same stem (R2: no per-member overwrite). Returns
    (pages rendered, images rendered). Missing tools or render failures
    print and return (0, 0)."""
    shots_dir = processed_dir / "screenshots"
    pdf_dir: Path | None = None
    try:  # ruff: ignore[too-many-statements-in-try-clause]
        if file_format == "ipynb":
            shots_dir.mkdir(parents=True, exist_ok=True)
            kwargs: dict = {}
            if template_name:
                kwargs["template_name"] = template_name
            if template_dir:
                kwargs["extra_template_basedirs"] = [str(template_dir)]
            from nbconvert import MarkdownExporter  # ruff: ignore[import-outside-top-level]

            text, _ = MarkdownExporter(**kwargs).from_filename(str(input_file))
            n_images = _extract_embedded_images(
                text, output_stem, shots_dir, img_offset=img_offset
            )
            return (0, n_images)
        if file_format == "image":
            shots_dir.mkdir(parents=True, exist_ok=True)
            with Image.open(input_file) as src:
                src.convert("RGB").save(
                    shots_dir / f"{output_stem}_p{page_offset + 1}.png"
                )
            print(
                f"[screenshots] rendered {output_stem}_p{page_offset + 1}.png "
                f"for {output_stem}"
            )
            return (1, 0)
        if file_format not in {"docx", "pdf"}:
            return (0, 0)
        if not shutil.which("pdftoppm") or (
            file_format == "docx" and not shutil.which("soffice")
        ):
            print(
                f"[screenshots] skipped {input_file.name}: soffice/pdftoppm not found"
            )
            return (0, 0)
        shots_dir.mkdir(parents=True, exist_ok=True)
        pdf_dir = shots_dir / "_pdf"
        if file_format == "docx":
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
                return (0, 0)
            pdf_path = pdf_dir / f"{input_file.stem}.pdf"
            if not pdf_path.exists():
                print(f"[screenshots] no PDF produced for {input_file.name}")
                return (0, 0)
        else:
            pdf_path = input_file
        n_pages = _pdftoppm_pages(pdf_path, output_stem, shots_dir, page_offset)
        return (n_pages, 0)
    except Exception as exc:
        print(f"[screenshots] failed for {input_file.name}: {exc}")
        return (0, 0)
    finally:
        if pdf_dir is not None:
            shutil.rmtree(pdf_dir, ignore_errors=True)


def _cleanup_stem_shots(shots_dir: Path, output_stem: str) -> None:
    """Delete every old screenshot of ``output_stem`` (``{stem}_p*.png`` /
    ``{stem}_i*.png``) before re-rendering, so a changed submission never
    leaves stale shots that grading would collect (R2). Only this stem's
    prefix is touched — other students' files are untouched."""
    if not shots_dir.exists():
        return
    for pattern in (f"{output_stem}_p*.png", f"{output_stem}_i*.png"):
        for old in shots_dir.glob(pattern):
            old.unlink(missing_ok=True)


def _render_stem_screenshots(
    processed_dir: Path,
    output_stem: str,
    files: list[tuple[Path, InputFormat]],
    template_name: str | None,
    template_dir: Path | None,
) -> None:
    """Clean old shots for ``output_stem``, then render each member file in
    order with globally continuous page/image numbers (R2). Best-effort:
    every render failure is swallowed by ``_render_screenshots``."""
    _cleanup_stem_shots(processed_dir / "screenshots", output_stem)
    page_offset = 0
    img_offset = 0
    for input_file, file_format in files:
        n_pages, n_images = _render_screenshots(
            input_file,
            output_stem,
            processed_dir,
            file_format,
            template_name,
            template_dir,
            page_offset=page_offset,
            img_offset=img_offset,
        )
        page_offset += n_pages
        img_offset += n_images
