from __future__ import annotations

from pathlib import Path

import nbformat
from src.grading import _read_reference_text
from src.processing import convert_ipynb_to_markdown, preprocess_assignment


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
