from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from copydetect import CopyDetector
from src.shared.caching import cache_file, load_cache_file
from src.shared.plagiarism import (
    PlagiarismConfig,
    _blend_rows,
    _embedding_pairs,
    _pair_key,
    _run_embedding,
    _write_full_pair_data,
    detect_plagiarism,
    embedding_input_hash,
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


# -- T2a/T2b: code detector autoopen fix + quiet aggregate report ------------


def _minimal_notebook() -> str:
    return json.dumps({
        "cells": [
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": ["print(1)"],
            }
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    })


def test_code_detector_disables_autoopen(monkeypatch: pytest.MonkeyPatch) -> None:
    """T2a: the code-path CopyDetector must be constructed with
    autoopen=False/silent=True (a default autoopen=True opens a browser
    window from inside the TUI worker thread)."""
    captured: list[dict] = []

    class FakeDetector:
        def __init__(self, **kwargs: object) -> None:
            captured.append(kwargs)
            self.test_files: list[str] = []
            self.ref_files: list[str] = []
            self.similarity_matrix: list = []
            self.token_overlap_matrix: list = []

        def run(self) -> None:
            pass

        def generate_html_report(self) -> None:
            pass

    monkeypatch.setattr("src.shared.plagiarism.CopyDetector", FakeDetector)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "raw").mkdir()
        (root / "raw" / "100001.ipynb").write_text(
            _minimal_notebook(), encoding="utf-8"
        )
        (root / "config.toml").write_text(
            "[grading]\nrubric = 'rubrics/exam.toml'\n"
            "system_prompt = 'prompt/system.md'\n"
            "provider = 'deepseek'\n"
            "max_parallel_tasks = 4\n"
            "[plagiarism]\ndisplay_threshold = 0.9\n",
            encoding="utf-8",
        )
        detect_plagiarism(root / "config.toml")

    assert captured, "code-path CopyDetector should have been constructed"
    assert captured[-1]["autoopen"] is False, captured[-1]
    assert captured[-1]["silent"] is True, captured[-1]


def test_aggregate_quiet_suppresses_stdout_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T2b: quiet=True skips the text report print (TUI jobs read
    aggregate.json instead); quiet=False keeps it (CLI unchanged)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        a_dir = root / "a1"
        (a_dir / "plagiarism").mkdir(parents=True)
        (a_dir / "config.toml").write_text("", encoding="utf-8")
        pairs = [
            {
                "test_file": "1001.md",
                "reference_file": "1002.md",
                "test_similarity_pct": 80.0,
                "reference_similarity_pct": 85.0,
                "max_similarity_pct": 85.0,
                "token_overlap": 10,
            },
            {
                "test_file": "1001.md",
                "reference_file": "1003.md",
                "test_similarity_pct": 50.0,
                "reference_similarity_pct": 55.0,
                "max_similarity_pct": 55.0,
                "token_overlap": 3,
            },
        ]
        (a_dir / "plagiarism" / "all_pairs.json").write_text(
            json.dumps({"version": 1, "pairs": pairs}), encoding="utf-8"
        )
        (root / "config.toml").write_text("[fetch]\n", encoding="utf-8")
        monkeypatch.setattr(
            "src.shared.plagiarism._run_assignment",
            lambda cfg: {
                "stage": "plagiarism",
                "success": 0,
                "errors": 0,
                "total": 0,
                "success_rate": 0,
            },
        )

        detect_plagiarism(root / "config.toml", aggregate=True, quiet=True)
        out = capsys.readouterr().out
        assert "Cross-Assignment Aggregate" not in out, out

        detect_plagiarism(root / "config.toml", aggregate=True, quiet=False)
        out = capsys.readouterr().out
        assert "Cross-Assignment Aggregate" in out, out


# -- B4: embedding cache at <assignment>/.cache/embedding.json, hash freshness --


def _plagiarism_config(
    root: Path, model: str = "fake-embedding-model"
) -> PlagiarismConfig:
    return PlagiarismConfig(
        assignment_dir=root,
        raw_dir=root / "raw",
        processed_dir=root / "processed",
        output_dir=root / "plagiarism",
        submissions_dir=root / "plagiarism" / "submissions",
        template_dir=root / "plagiarism" / "template",
        report_file=root / "plagiarism" / "report.html",
        full_pairs_file=root / "plagiarism" / "all_pairs.json",
        template_file=root / "template.ipynb",
        extensions=[".py"],
        display_threshold=0.8,
        include_python_files=True,
        copydetect_weight=0.95,
        embedding_weight=0.05,
        embedding_model=model,
    )


@pytest.fixture
def fake_embedder(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Monkeypatched SentenceTransformer (no model download); call sizes recorded.

    ``encode_document`` returns deterministic 1-column embeddings, so file i
    vs file j similarity is ``(i + 1) * (j + 1) / 100`` in percent.
    """
    calls: list[int] = []

    class FakeModel:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def encode_document(
            self,
            texts: list[str],
            batch_size: int = 16,
            show_progress_bar: bool = False,
        ) -> np.ndarray:
            calls.append(len(texts))
            return (np.arange(1, len(texts) + 1) / 10).reshape(-1, 1).astype(np.float32)

    monkeypatch.setattr("src.shared.plagiarism.SentenceTransformer", FakeModel)
    return calls


def test_embedding_reused_when_processed_inputs_unchanged(
    tmp_path: Path,
    write_tree: Callable[[Path, str, str], Path],
    fake_embedder: list[int],
) -> None:
    write_tree(tmp_path, "processed/aaa.md", "first answer essay text " * 4)
    write_tree(tmp_path, "processed/bbb.md", "second answer essay text " * 4)
    cfg = _plagiarism_config(tmp_path)

    assert _run_embedding(cfg) is True
    assert fake_embedder == [2]

    cache_path = cache_file(tmp_path, "embedding")
    data = load_cache_file(cache_path)
    assert data["hash"] == embedding_input_hash(cfg.processed_dir, cfg.embedding_model)
    # Pairs reach the consumer through the same interface as before.
    assert _embedding_pairs(cache_path) == {("aaa.md", "bbb.md"): pytest.approx(2.0)}
    # The old flat file is never written anymore.
    assert not (tmp_path / "plagiarism" / "all_pairs.embedding.json").exists()

    assert _run_embedding(cfg) is True
    assert fake_embedder == [2]  # hash matched: pairs reused, no re-encode


def test_embedding_recomputes_when_processed_md_changes(
    tmp_path: Path,
    write_tree: Callable[[Path, str, str], Path],
    fake_embedder: list[int],
) -> None:
    write_tree(tmp_path, "processed/aaa.md", "first answer essay text " * 4)
    write_tree(tmp_path, "processed/bbb.md", "second answer essay text " * 4)
    cfg = _plagiarism_config(tmp_path)

    assert _run_embedding(cfg) is True
    assert fake_embedder == [2]

    write_tree(tmp_path, "processed/aaa.md", "rewritten answer with more words " * 4)
    assert _run_embedding(cfg) is True
    assert fake_embedder == [2, 2]  # md content change -> recompute
    data = load_cache_file(cache_file(tmp_path, "embedding"))
    assert data["hash"] == embedding_input_hash(cfg.processed_dir, cfg.embedding_model)


def test_embedding_recomputes_when_model_changes(
    tmp_path: Path,
    write_tree: Callable[[Path, str, str], Path],
    fake_embedder: list[int],
) -> None:
    write_tree(tmp_path, "processed/aaa.md", "first answer essay text " * 4)
    write_tree(tmp_path, "processed/bbb.md", "second answer essay text " * 4)

    assert _run_embedding(_plagiarism_config(tmp_path, model="model-a")) is True
    assert fake_embedder == [2]

    assert _run_embedding(_plagiarism_config(tmp_path, model="model-b")) is True
    assert fake_embedder == [2, 2]  # model name is part of the input hash


def test_embedding_recomputes_on_corrupt_cache(
    tmp_path: Path,
    write_tree: Callable[[Path, str, str], Path],
    fake_embedder: list[int],
) -> None:
    write_tree(tmp_path, "processed/aaa.md", "first answer essay text " * 4)
    write_tree(tmp_path, "processed/bbb.md", "second answer essay text " * 4)
    cfg = _plagiarism_config(tmp_path)
    cache_path = cache_file(tmp_path, "embedding")

    assert _run_embedding(cfg) is True
    assert fake_embedder == [2]

    cache_path.write_bytes(b'{"fmt": 1, "data": {')  # truncated JSON
    assert _embedding_pairs(cache_path) == {}  # consumer read stays tolerant
    assert _run_embedding(cfg) is True  # recompute, no crash
    assert fake_embedder == [2, 2]

    cache_path.write_text(
        json.dumps({"fmt": 99, "data": {"hash": "stale", "pairs": []}}),
        encoding="utf-8",
    )
    assert _run_embedding(cfg) is True  # wrong envelope -> treated as empty
    assert fake_embedder == [2, 2, 2]


def test_embedding_input_hash_tracks_md_and_model(
    tmp_path: Path,
    write_tree: Callable[[Path, str, str], Path],
) -> None:
    write_tree(tmp_path, "processed/aaa.md", "first answer essay text " * 4)
    processed = tmp_path / "processed"

    baseline = embedding_input_hash(processed, "model-a")
    assert embedding_input_hash(processed, "model-a") == baseline
    assert embedding_input_hash(processed, "model-b") != baseline

    write_tree(tmp_path, "processed/bbb.md", "second answer essay text " * 4)
    assert embedding_input_hash(processed, "model-a") != baseline
