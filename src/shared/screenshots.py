"""Screenshot rendering for visual evaluation: docx/pptx/pdf/image via
soffice->pdftoppm and ipynb via image outputs read from the notebook JSON.
"""

from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

from PIL import Image, ImageOps

from .assignment_config import InputFormat

# Rendered-shot classes: _pN pages come from these formats (soffice for
# docx/pptx, pdftoppm/PIL for pdf/image), _iN images from ipynb outputs.
# Shared with the cache-hit freshness check so the two never drift.
_SOFFICE_FORMATS = frozenset({"docx", "pptx"})
_PAGE_FORMATS = _SOFFICE_FORMATS | {"pdf", "image"}


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


def _first_image_payload(output: dict) -> tuple[str, str | list[str]] | None:
    """(mime, base64 payload) of one notebook output's first image, PNG
    preferred over other representations of the same figure."""
    data = output.get("data") or {}
    for mime in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        payload = data.get(mime)
        if payload is not None:
            return mime, payload
    return None


def _write_png_from_payload(target: Path, mime: str, payload: str | list[str]) -> None:
    """Write one base64 notebook image payload to ``target`` as a valid PNG:
    PNG payloads stay byte-identical, other image mimes are re-encoded
    through PIL. The bytes are staged in a sibling ``.<name>.tmp`` (no
    ``_p*``/``_i*`` glob match) and moved in with ``Path.replace`` (atomic
    ``os.replace`` under the hood), so a failed decode/encode/write never
    leaves a partial PNG behind."""
    raw = base64.b64decode(payload if isinstance(payload, str) else "".join(payload))
    if mime == "image/png":
        # Verify without re-encoding: the written bytes must stay identical
        # to the payload (test-pinned).
        with Image.open(io.BytesIO(raw)) as check:
            check.verify()
        data = raw
    else:
        buf = io.BytesIO()
        with Image.open(io.BytesIO(raw)) as src:
            src.save(buf, "PNG")
        data = buf.getvalue()
    tmp = target.with_name(f".{target.name}.tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _iter_notebook_images(
    notebook_path: Path,
) -> Iterator[tuple[str, str | list[str]]]:
    """(mime, base64 payload) of every image output of the notebook, in
    cell/output order — the single JSON traversal the extractor and the
    freshness check share. An unreadable notebook prints and yields
    nothing."""
    try:
        notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[screenshots] failed to read {notebook_path.name}: {exc}")
        return
    for cell in notebook.get("cells", []):
        for output in cell.get("outputs", []):
            image = _first_image_payload(output)
            if image is not None:
                yield image


def _notebook_has_images(notebook_path: Path) -> bool:
    """True when any output carries an image payload. The class predicate
    for the cache-hit freshness check: an image-less notebook produces no
    ``_iN`` shots, so counting it would re-render forever."""
    return next(_iter_notebook_images(notebook_path), None) is not None


def _extract_notebook_images(
    notebook_path: Path, output_stem: str, shots_dir: Path, img_offset: int = 0
) -> int:
    """Save the notebook's embedded image outputs as ``{output_stem}_i{n}.png``
    (n from ``img_offset`` so folder members continue the same stem's
    numbering) in ``shots_dir``. Returns how many images were saved.

    Jupyter keys figure payloads (base64) by mime type under
    ``cell.outputs[*].data``; nbconvert's markdown output only references
    them as external files, so the raw notebook JSON is the source of
    truth. Every written file is a valid PNG, in cell/output order, one
    image per output; unreadable payloads are skipped with a message.
    """
    shots_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for mime, payload in _iter_notebook_images(notebook_path):
        target = shots_dir / f"{output_stem}_i{img_offset + n}.png"
        try:
            _write_png_from_payload(target, mime, payload)
        except Exception as exc:
            print(
                f"[screenshots] skipped unreadable {mime} output "
                f"in {notebook_path.name}: {exc}"
            )
            continue
        n += 1
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


def _render_screenshots(  # ruff: ignore[too-many-return-statements, too-many-arguments, too-many-positional-arguments]
    input_file: Path,
    output_stem: str,
    processed_dir: Path,
    file_format: InputFormat,
    page_offset: int = 0,
    img_offset: int = 0,
) -> tuple[int, int]:
    """Render screenshots for visual evaluation (best-effort, never raises):
    docx/pptx/pdf -> one PNG per page (all pages, no truncation), image ->
    one PNG via PIL, ipynb -> image outputs saved from the notebook JSON.
    ``page_offset``/``img_offset`` shift the numbering so folder members
    continue the same stem (R2: no per-member overwrite). Returns (pages
    rendered, images rendered). Missing tools or render failures print and
    return (0, 0)."""
    shots_dir = processed_dir / "screenshots"
    pdf_dir: Path | None = None
    try:  # ruff: ignore[too-many-statements-in-try-clause]
        if file_format == "ipynb":
            n_images = _extract_notebook_images(
                input_file, output_stem, shots_dir, img_offset=img_offset
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
        if file_format not in _PAGE_FORMATS:  # ipynb/image handled above
            return (0, 0)
        if not shutil.which("pdftoppm") or (
            file_format in _SOFFICE_FORMATS and not shutil.which("soffice")
        ):
            print(
                f"[screenshots] skipped {input_file.name}: soffice/pdftoppm not found"
            )
            return (0, 0)
        shots_dir.mkdir(parents=True, exist_ok=True)
        pdf_dir = shots_dir / "_pdf"
        if file_format in _SOFFICE_FORMATS:
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
            page_offset=page_offset,
            img_offset=img_offset,
        )
        page_offset += n_pages
        img_offset += n_images
