from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from copydetect import CopyDetector
from src.plagiarism import (
    _blend_rows,
    _pair_key,
    _write_full_pair_data,
)


def test_copydetect_text_ranks_verbatim_above_distinct() -> None:
    """Ported from misc/plagiarism_text.py --selftest; guards the
    disable_filtering pitfall (filter_code drops token.Text, prose
    fingerprints become empty and every pair scores 0.0%)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        text = (
            "Artificial Intelligence is when a computer or system can use information "
            "to make decisions, recognize patterns, learn, or complete tasks that "
            "normally require some level of human intelligence."
        )
        other = "GDP growth accelerated to 4.1 percent in the third quarter as consumer spending rose."
        (root / "aaa.md").write_text(text, encoding="utf-8")
        (root / "bbb.md").write_text(text, encoding="utf-8")
        (root / "ccc.md").write_text(other, encoding="utf-8")

        detector = CopyDetector(
            test_dirs=[str(root)],
            extensions=[".md"],
            autoopen=False,
            silent=True,
            disable_filtering=True,
        )
        detector.run()
        pair_path = root / "pairs.json"
        _write_full_pair_data(detector, pair_path)
        rows = json.loads(pair_path.read_text(encoding="utf-8"))["pairs"]
        by_key = {_pair_key(r["test_file"], r["reference_file"]): r for r in rows}
        s_dup = by_key["aaa.md", "bbb.md"]["max_similarity_pct"]
        s_diff = max(
            by_key["aaa.md", "ccc.md"]["max_similarity_pct"],
            by_key["bbb.md", "ccc.md"]["max_similarity_pct"],
        )
        assert s_dup > s_diff, f"verbatim {s_dup:.1f}% not above distinct {s_diff:.1f}%"


def test_blend_rows_weights() -> None:
    rows = [
        {
            "test_file": "a.md",
            "reference_file": "b.md",
            "max_similarity_pct": 80.0,
            "token_overlap": 10,
        },
        {
            "test_file": "a.md",
            "reference_file": "c.md",
            "max_similarity_pct": 60.0,
            "token_overlap": 5,
        },
    ]
    emb = {("a.md", "b.md"): 100.0}
    blended = _blend_rows(rows, emb, copydetect_weight=0.95, embedding_weight=0.05)
    by_key = {_pair_key(r["test_file"], r["reference_file"]): r for r in blended}
    # 0.95 * 80 + 0.05 * 100 = 81; pair without embedding stays at copydetect.
    assert by_key["a.md", "b.md"]["max_similarity_pct"] == pytest.approx(81.0)
    assert by_key["a.md", "b.md"]["embedding_similarity_pct"] == pytest.approx(100.0)
    assert by_key["a.md", "c.md"]["max_similarity_pct"] == pytest.approx(60.0)
    assert by_key["a.md", "c.md"]["embedding_similarity_pct"] is None
    # Sorted by blended score descending.
    assert blended[0]["max_similarity_pct"] == pytest.approx(81.0)
