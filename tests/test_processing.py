from __future__ import annotations

import json
import shutil
from pathlib import Path

import anydoc
import nbformat
import pytest
from src.shared.grading import _read_reference_text
from src.shared.processing import (
    SUPPORTED_INPUT_FORMATS,
    _format_for_suffix,
    convert_ipynb_to_markdown,
    convert_pdf_to_markdown,
    preprocess_assignment,
)


def test_txt_text_submission_converts_as_html(tmp_path: Path) -> None:
    """Canvas text entries are saved as .txt but are HTML: preprocess must
    route them through the HTML -> markdown conversion (regression)."""
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "390489_LATE_0.txt").write_text("<p>my <b>answer</b></p>", encoding="utf-8")
    (tmp_path / "config.toml").write_text(
        '[grading]\nrubric = "r.toml"\nsystem_prompt = ["p.md"]\nprovider = "deepseek"\n',
        encoding="utf-8",
    )

    result = preprocess_assignment(tmp_path / "config.toml")

    md = tmp_path / "processed" / "390489_LATE_0.md"
    assert md.exists()
    content = md.read_text(encoding="utf-8")
    assert "answer" in content
    assert "<p>" not in content
    assert "<b>" not in content
    assert result is not None
    assert result["success"] == 1


def test_ipynb_converts_to_markdown(tmp_path: Path) -> None:
    nb_path = tmp_path / "nb.ipynb"
    out_path = tmp_path / "nb.md"
    nb = nbformat.v4.new_notebook(
        cells=[
            nbformat.v4.new_markdown_cell("# Hello"),
            nbformat.v4.new_code_cell("print(42)"),
        ]
    )
    nbformat.write(nb, nb_path)

    convert_ipynb_to_markdown(nb_path, out_path)

    assert out_path.exists()
    content = out_path.read_text(encoding="utf-8")
    assert "# Hello" in content
    assert "print(42)" in content


def test_reference_text_converts_ipynb_and_html(tmp_path: Path) -> None:
    nb = nbformat.v4.new_notebook(
        cells=[
            nbformat.v4.new_markdown_cell("# Ref"),
            nbformat.v4.new_code_cell("x = 1"),
        ]
    )
    nb_path = tmp_path / "ref.ipynb"
    nbformat.write(nb, nb_path)
    html_path = tmp_path / "ref.html"
    html_path.write_text("<h1>Title</h1><p>my <b>answer</b></p>", encoding="utf-8")

    nb_text = _read_reference_text(nb_path)
    assert "# Ref" in nb_text
    assert "x = 1" in nb_text

    html_text = _read_reference_text(html_path)
    assert "answer" in html_text
    assert "<p>" not in html_text


def _write_grading_config(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        '[grading]\nrubric = "r.toml"\nsystem_prompt = ["p.md"]\nprovider = "deepseek"\n',
        encoding="utf-8",
    )


def test_preprocess_cache_skips_unchanged_second_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 2: unchanged inputs -> the second run converts nothing."""
    from src.tui.scan import count_files

    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "100.md").write_text("# hello\n", encoding="utf-8")
    _write_grading_config(tmp_path)
    calls: list[Path] = []

    def spy(src: Path, dst: Path) -> None:
        calls.append(src)
        shutil.copy2(src, dst)

    monkeypatch.setattr("src.shared.processing._convert_markdown", spy)

    preprocess_assignment(tmp_path / "config.toml")
    preprocess_assignment(tmp_path / "config.toml")

    assert len(calls) == 1
    cache_file = tmp_path / "processed" / ".preprocess.cache.json"
    assert cache_file.is_file()
    # dot-named cache file is not a processed student
    assert count_files(tmp_path / "processed", ".md") == 1


def test_preprocess_cache_reconverts_on_raw_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 2: changed raw content -> hash mismatch -> reconvert."""
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "100.md").write_text("v1\n", encoding="utf-8")
    _write_grading_config(tmp_path)
    calls: list[Path] = []

    def spy(src: Path, dst: Path) -> None:
        calls.append(src)
        shutil.copy2(src, dst)

    monkeypatch.setattr("src.shared.processing._convert_markdown", spy)

    preprocess_assignment(tmp_path / "config.toml")
    (raw / "100.md").write_text("v2\n", encoding="utf-8")
    preprocess_assignment(tmp_path / "config.toml")

    assert len(calls) == 2
    md = tmp_path / "processed" / "100.md"
    assert md.read_text(encoding="utf-8") == "v2\n"


def test_preprocess_broken_cache_treated_as_empty(tmp_path: Path) -> None:
    """Item 2: an unparsable cache file must not crash or skip conversion."""
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "100.md").write_text("# x\n", encoding="utf-8")
    _write_grading_config(tmp_path)
    (tmp_path / "processed").mkdir()
    (tmp_path / "processed" / ".preprocess.cache.json").write_text(
        "{not json", encoding="utf-8"
    )

    result = preprocess_assignment(tmp_path / "config.toml")

    assert result is not None
    assert result["success"] == 1
    assert (tmp_path / "processed" / "100.md").is_file()
    cache = json.loads(
        (tmp_path / "processed" / ".preprocess.cache.json").read_text("utf-8")
    )
    assert "100" in cache


def test_folder_concat_html_and_ipynb(tmp_path: Path) -> None:
    """A multi-file student folder becomes ONE md: each file converted and
    concatenated with a per-file header (file:/submitted:), html section
    before ipynb section (sorted by name)."""
    raw = tmp_path / "raw"
    (raw / "100").mkdir(parents=True)
    (raw / "100" / "100.html").write_text(
        "<h1>Answer</h1><p>my <b>body</b></p>", encoding="utf-8"
    )
    nb = nbformat.v4.new_notebook(
        cells=[
            nbformat.v4.new_markdown_cell("# nb part"),
            nbformat.v4.new_code_cell("x = 1"),
        ]
    )
    nbformat.write(nb, raw / "100" / "100_0.ipynb")
    (raw / ".fetch-cache.json").write_text(
        json.dumps({
            "100.html": "2026-01-01T00:00:00Z",
            "100_0.ipynb": "2026-01-02T00:00:00Z",
        }),
        encoding="utf-8",
    )
    _write_grading_config(tmp_path)

    result = preprocess_assignment(tmp_path / "config.toml")

    md = tmp_path / "processed" / "100.md"
    assert md.exists()
    assert not (tmp_path / "processed" / "100.html.md").exists()
    assert not (tmp_path / "processed" / "100_0.ipynb.md").exists()
    content = md.read_text(encoding="utf-8")
    html_idx = content.index("file: 100.html")
    ipynb_idx = content.index("file: 100_0.ipynb")
    assert html_idx < ipynb_idx  # sorted by name -> html section first
    assert "submitted: 2026-01-01T00:00:00Z" in content
    assert "submitted: 2026-01-02T00:00:00Z" in content
    assert "# Answer" in content
    assert "# nb part" in content
    assert "x = 1" in content
    assert result is not None
    assert result["success"] == 1  # one student -> one md
    assert len(list((tmp_path / "processed").glob("*.md"))) == 1


def test_folder_concat_body_member_ordered_first(tmp_path: Path) -> None:
    """The unsuffixed member (the written body) is concatenated before the
    suffixed _N members regardless of file-name order: [20_0.ipynb, 20.html]
    must yield the html section first, then the ipynb."""
    raw = tmp_path / "raw"
    (raw / "20").mkdir(parents=True)
    nb = nbformat.v4.new_notebook(
        cells=[
            nbformat.v4.new_markdown_cell("# nb part"),
            nbformat.v4.new_code_cell("x = 1"),
        ]
    )
    nbformat.write(nb, raw / "20" / "20_0.ipynb")
    (raw / "20" / "20.html").write_text(
        "<h1>Answer</h1><p>my <b>body</b></p>", encoding="utf-8"
    )
    _write_grading_config(tmp_path)

    result = preprocess_assignment(tmp_path / "config.toml")

    md = tmp_path / "processed" / "20.md"
    assert md.exists()
    content = md.read_text(encoding="utf-8")
    html_idx = content.index("file: 20.html")
    ipynb_idx = content.index("file: 20_0.ipynb")
    assert html_idx < ipynb_idx
    assert result is not None
    assert result["success"] == 1


def test_explicit_input_format_filters_top_level_files(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "100.html").write_text("<h1>Answer</h1><p>my body</p>", encoding="utf-8")
    nb = nbformat.v4.new_notebook(cells=[nbformat.v4.new_markdown_cell("# nb")])
    nbformat.write(nb, raw / "100_0.ipynb")
    _write_grading_config(tmp_path)
    (tmp_path / "config.toml").write_text(
        '[grading]\nrubric = "r.toml"\nsystem_prompt = ["p.md"]\nprovider = "deepseek"\n'
        '[processing]\ninput_format = "html"\n',
        encoding="utf-8",
    )

    result = preprocess_assignment(tmp_path / "config.toml")

    md = tmp_path / "processed" / "100.md"
    assert md.exists()  # html file processed...
    assert not (tmp_path / "processed" / "100_0.md").exists()  # ipynb filtered out
    assert result is not None
    assert result["success"] == 1


def test_explicit_input_format_filters_folder_files(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    (raw / "100").mkdir(parents=True)
    (raw / "100" / "100.html").write_text("<h1>Answer</h1>", encoding="utf-8")
    nb = nbformat.v4.new_notebook(cells=[nbformat.v4.new_markdown_cell("# nb")])
    nbformat.write(nb, raw / "100" / "100_0.ipynb")
    (tmp_path / "config.toml").write_text(
        '[grading]\nrubric = "r.toml"\nsystem_prompt = ["p.md"]\nprovider = "deepseek"\n'
        '[processing]\ninput_format = ["ipynb"]\n',
        encoding="utf-8",
    )

    result = preprocess_assignment(tmp_path / "config.toml")

    md = tmp_path / "processed" / "100.md"
    assert md.exists()
    content = md.read_text(encoding="utf-8")
    assert "file: 100_0.ipynb" in content  # ipynb section included
    assert "file: 100.html" not in content  # html filtered out
    assert result is not None
    assert result["success"] == 1


def _write_docx(path: Path, text: str) -> None:
    """Minimal docx (zip with one paragraph); anydoc parses it in-process."""
    import zipfile

    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
            'content-types">'
            '<Default Extension="rels" ContentType="application/vnd.'
            'openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/'
            "vnd.openxmlformats-officedocument.wordprocessingml.document.main."
            '+xml"/></Types>',
        )
        z.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
            '2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/officeDocument" Target="word/'
            'document.xml"/></Relationships>',
        )
        z.writestr("word/document.xml", doc)


def _write_pdf(path: Path, text: str) -> None:
    """Minimal text PDF (one Helvetica line, computed xref); anydoc parses it in-process."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    count = len(objects) + 1
    out += f"xref\n0 {count}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))


def _write_raster_pdf(path: Path, text: str) -> None:
    """Image-only PDF (one white page with drawn text): anydoc's local parser
    finds no text layer and raises NeedsOcrError, exactly like a scan."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (400, 100), "white")
    ImageDraw.Draw(img).text((10, 40), text, fill="black")
    img.save(path, "PDF", resolution=72)


def test_pdf_converts_to_markdown(tmp_path: Path) -> None:
    pdf_path = tmp_path / "s.pdf"
    out_path = tmp_path / "s.md"
    _write_pdf(pdf_path, "tata pdf test 12345")

    convert_pdf_to_markdown(pdf_path, out_path)

    assert out_path.exists()
    content = out_path.read_text(encoding="utf-8")
    assert "tata pdf test 12345" in content


def test_single_pdf_preprocess_raw_to_md(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    _write_pdf(raw / "100.pdf", "tata pdf test 12345")
    _write_grading_config(tmp_path)

    result = preprocess_assignment(tmp_path / "config.toml")

    md = tmp_path / "processed" / "100.md"
    assert md.exists()
    content = md.read_text(encoding="utf-8")
    assert "tata pdf test 12345" in content
    assert result is not None
    assert result["success"] == 1


def test_format_for_suffix_infers_pdf() -> None:
    assert _format_for_suffix(".pdf") == "pdf"
    assert _format_for_suffix(".PDF") == "pdf"
    assert "pdf" in SUPPORTED_INPUT_FORMATS


def test_folder_skip_messages_for_unsupported_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unsupported files inside a folder print a [skip] line (not silent);
    a folder with no supported files prints one '[skip] folder' line."""
    raw = tmp_path / "raw"
    (raw / "100").mkdir(parents=True)
    (raw / "100" / "100.html").write_text("<h1>ok</h1>", encoding="utf-8")
    (raw / "100" / "100.py").write_text("print(1)", encoding="utf-8")
    (raw / "200").mkdir(parents=True)
    (raw / "200" / "200.py").write_text("print(2)", encoding="utf-8")
    _write_grading_config(tmp_path)

    result = preprocess_assignment(tmp_path / "config.toml")

    out = capsys.readouterr().out
    assert "[skip] 100.py (unsupported format)" in out
    assert "[skip] folder 200 (no supported files)" in out
    assert (tmp_path / "processed" / "100.md").exists()
    assert result is not None
    assert result["success"] == 1


def test_mixed_layout_skips_stale_flat_duplicates(tmp_path: Path) -> None:
    """Regression: raw/ with BOTH a folderized student (415019/) and stale
    flat leftovers of the previous flat fetch (415019.docx, 415019_1.docx)
    must produce ONE 415019.md (folder concat, both headers) and no stray
    415019_1.md."""
    raw = tmp_path / "raw"
    (raw / "415019").mkdir(parents=True)
    _write_docx(raw / "415019" / "415019.docx", "part one")
    _write_docx(raw / "415019" / "415019_1.docx", "part two")
    (raw / "415019.docx").write_bytes(b"stale flat")
    (raw / "415019_1.docx").write_bytes(b"stale flat")
    _write_grading_config(tmp_path)

    result = preprocess_assignment(tmp_path / "config.toml")

    md = tmp_path / "processed" / "415019.md"
    assert md.exists()
    content = md.read_text(encoding="utf-8")
    assert "file: 415019.docx" in content
    assert "file: 415019_1.docx" in content
    assert "part one" in content
    assert "part two" in content
    assert not (tmp_path / "processed" / "415019_1.md").exists()
    assert len(list((tmp_path / "processed").glob("*.md"))) == 1
    assert result is not None
    assert result["success"] == 1


def test_format_for_suffix_infers_image() -> None:
    assert _format_for_suffix(".jpg") == "image"
    assert _format_for_suffix(".JPG") == "image"
    assert _format_for_suffix(".jpeg") == "image"
    assert _format_for_suffix(".png") == "image"
    assert "image" in SUPPORTED_INPUT_FORMATS


def test_image_jpeg_preprocess_uses_hosted_ocr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .jpg raw file is rastered to a temp PDF and fed to anydoc with
    ocr=\"hosted\"; the temp PDF exists when OCR runs (and is removed after)."""
    from PIL import Image

    raw = tmp_path / "raw"
    raw.mkdir()
    img = Image.new("RGB", (200, 80), "white")
    img.save(raw / "100.jpg", "JPEG")
    _write_grading_config(tmp_path)

    calls: list[tuple[Path, bool, dict]] = []

    def fake_to_markdown(path: str, **kwargs: object) -> str:
        pdf_path = Path(path)
        calls.append((pdf_path, pdf_path.exists(), kwargs))  # exists at OCR time
        return "hello image ocr\n"

    monkeypatch.setattr(anydoc, "to_markdown", fake_to_markdown)

    result = preprocess_assignment(tmp_path / "config.toml")

    md = tmp_path / "processed" / "100.md"
    assert md.exists()
    content = md.read_text(encoding="utf-8")
    assert "hello image ocr" in content
    assert len(calls) == 1
    pdf_path, existed, kwargs = calls[0]
    assert pdf_path.suffix == ".pdf"
    assert existed  # temp PDF existed when OCR ran
    assert kwargs.get("ocr") == "hosted"
    assert not pdf_path.exists()  # temp PDF removed after processing
    assert result is not None
    assert result["success"] == 1


def test_scanned_pdf_uses_hosted_ocr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scan (no text layer) raises NeedsOcrError locally; anydoc routes it
    to hosted OCR (mocked) instead of failing or returning empty text."""
    pdf_path = tmp_path / "s.pdf"
    out_path = tmp_path / "s.md"
    _write_raster_pdf(pdf_path, "SCANNED TEXT")

    monkeypatch.setattr(anydoc, "_parse_hosted", lambda *a, **k: "scanned text\n")

    convert_pdf_to_markdown(pdf_path, out_path)

    content = out_path.read_text(encoding="utf-8")
    assert "scanned text" in content


def test_normal_pdf_parses_without_hosted_ocr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A text PDF parses locally: hosted OCR must never run (zero API cost)."""
    pdf_path = tmp_path / "s.pdf"
    out_path = tmp_path / "s.md"
    _write_pdf(pdf_path, "tata pdf test 12345")

    forbidden_msg = "hosted OCR must not run for a text PDF"

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError(forbidden_msg)

    monkeypatch.setattr(anydoc, "_parse_hosted", forbidden)

    convert_pdf_to_markdown(pdf_path, out_path)

    content = out_path.read_text(encoding="utf-8")
    assert "tata pdf test 12345" in content


def test_hosted_ocr_failure_raises_with_key_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hosted OCR failure becomes a RuntimeError naming FIRECRAWL_API_KEY
    (no silent empty-output fallback for scans)."""
    pdf_path = tmp_path / "s.pdf"
    out_path = tmp_path / "s.md"
    _write_raster_pdf(pdf_path, "SCANNED TEXT")

    failure_msg = "out of credits"

    def failed(*args: object, **kwargs: object) -> None:
        raise anydoc.HostedError(failure_msg)

    monkeypatch.setattr(anydoc, "_parse_hosted", failed)

    with pytest.raises(RuntimeError, match="FIRECRAWL_API_KEY") as exc_info:
        convert_pdf_to_markdown(pdf_path, out_path)
    assert "out of credits" in str(exc_info.value)
