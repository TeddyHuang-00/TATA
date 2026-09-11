from __future__ import annotations

import json
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from copydetect import CopyDetector
from src.shared import plagiarism as plagiarism_mod
from src.shared.assignment_config import PlagiarismSection
from src.shared.caching import cache_file, load_cache_file
from src.shared.plagiarism import (
    PlagiarismConfig,
    _blend_rows,
    _embedding_pairs,
    _load_plagiarism_config,
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
                "id": "a1b2c3d4",
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
            lambda cfg, **_kwargs: {
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


def test_detect_plagiarism_stops_before_first_assignment_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cooperative cancel (v10 batch 2): a pre-set cancel_event stops the
    per-assignment loop before any assignment runs (zero summary)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name in ("a1", "a2"):
            (root / name).mkdir()
            (root / name / "config.toml").write_text("", encoding="utf-8")
        (root / "config.toml").write_text("[fetch]\n", encoding="utf-8")
        calls: list[Path] = []

        def fake_run(cfg: Path, **_kwargs: object) -> dict:
            calls.append(cfg)
            return {
                "stage": "plagiarism",
                "success": 0,
                "errors": 0,
                "total": 0,
                "success_rate": 0,
            }

        monkeypatch.setattr("src.shared.plagiarism._run_assignment", fake_run)
        cancel_event = threading.Event()
        cancel_event.set()

        summary = detect_plagiarism(root / "config.toml", cancel_event=cancel_event)

        assert calls == [], "no assignment may run after a pre-set cancel event"
        assert summary == {
            "stage": "plagiarism",
            "success": 0,
            "errors": 0,
            "total": 0,
            "success_rate": 0,
        }


# -- v10 round 2 (M1): a cancelled run never writes a truncated report --------


def test_cancelled_run_stops_before_the_pair_pass(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """M1 (v10 round 2): a cancel that breaks the extraction loop must stop
    before the detector pass — the previous all_pairs.json/report.html stay
    byte-identical (the truncated pass used to overwrite them) and no new
    report file appears."""
    constructed: list[dict] = []

    class FakeDetector:
        """Stand-in that would rewrite both report files if ever reached."""

        def __init__(self, **kwargs: object) -> None:
            constructed.append(kwargs)
            self.out_file = kwargs["out_file"]
            self.test_files: list[str] = []
            self.ref_files: list[str] = []
            self.similarity_matrix: list = []
            self.token_overlap_matrix: list = []

        def run(self) -> None:
            pass

        def generate_html_report(self) -> None:
            Path(str(self.out_file)).write_text("REWRITTEN", encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "raw").mkdir()
        for uid in ("100001", "100002", "100003"):
            (root / "raw" / f"{uid}.ipynb").write_text(
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
        plag_dir = root / "plagiarism"
        plag_dir.mkdir()
        pairs_file = plag_dir / "all_pairs.json"
        report_file = plag_dir / "report.html"
        pairs_file.write_text(
            json.dumps({
                "version": 1,
                "pair_count": 1,
                "pairs": [{"max_similarity_pct": 95.0}],
            }),
            encoding="utf-8",
        )
        report_file.write_text("complete report from the last run", encoding="utf-8")
        before = {p.name: p.read_bytes() for p in plag_dir.iterdir() if p.is_file()}

        cancel_event = threading.Event()
        real_write = plagiarism_mod._write_extracted_code
        extracted: list[str] = []

        def stop_after_first(input_path: Path, output_path: Path) -> None:
            real_write(input_path, output_path)
            extracted.append(input_path.name)
            cancel_event.set()

        monkeypatch.setattr(plagiarism_mod, "_write_extracted_code", stop_after_first)
        monkeypatch.setattr(plagiarism_mod, "CopyDetector", FakeDetector)

        summary = detect_plagiarism(root / "config.toml", cancel_event=cancel_event)

        assert extracted == ["100001.ipynb"], extracted  # cancelled mid-extraction
        assert constructed == [], "the pair pass must not start after a cancel"
        assert summary == {
            "stage": "plagiarism",
            "success": 0,
            "errors": 0,
            "total": 0,
            "success_rate": 0,
        }
        after = {p.name: p.read_bytes() for p in plag_dir.iterdir() if p.is_file()}
        assert after == before, "existing reports must stay byte-identical"
        assert "stopped before the pair pass" in capsys.readouterr().out


def test_cancelled_course_run_skips_the_aggregate_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M1 (v10 round 2): a cancel observed mid-course stops the aggregate —
    neither the core `_aggregate_report` nor the TUI `run_aggregate_job`
    writes anything after the cancel."""
    from src.tui.plagiarism import run_aggregate_job

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name in ("a1", "a2"):
            (root / name).mkdir()
            (root / name / "config.toml").write_text("", encoding="utf-8")
        (root / "config.toml").write_text("[fetch]\n", encoding="utf-8")
        cancel_event = threading.Event()
        ran: list[str] = []

        def fake_run(cfg: Path, *, cancel_event: threading.Event | None = None) -> dict:
            ran.append(cfg.parent.name)
            if cancel_event is not None:
                cancel_event.set()  # user cancels while the first assignment runs
            return {
                "stage": "plagiarism",
                "success": 0,
                "errors": 0,
                "total": 0,
                "success_rate": 0,
            }

        aggregate_calls: list[tuple] = []
        json_writes: list[Path] = []

        def spy_aggregate(*args: object, **kwargs: object) -> None:
            aggregate_calls.append(args)

        monkeypatch.setattr(plagiarism_mod, "_run_assignment", fake_run)
        monkeypatch.setattr(plagiarism_mod, "_aggregate_report", spy_aggregate)
        monkeypatch.setattr(
            "src.tui.plagiarism._write_aggregate_json", json_writes.append
        )

        summary = run_aggregate_job(root / "config.toml", cancel_event=cancel_event)

        assert ran == ["a1"], ran  # the loop stopped at the next boundary
        assert aggregate_calls == [], "truncated aggregate must not run"
        assert json_writes == [], "cancelled aggregate must not rewrite the JSON"
        assert summary == {
            "stage": "plagiarism",
            "success": 0,
            "errors": 0,
            "total": 0,
            "success_rate": 0,
        }


# -- template_file "" / non-file: treated as unset, never crashes extraction ---


def _empty_template_config(root: Path) -> None:
    """Two notebook submissions + an assignment config whose [plagiarism]
    template_file is the empty string (the reported bug shape)."""
    (root / "raw").mkdir()
    for uid in ("100001", "100002"):
        (root / "raw" / f"{uid}.ipynb").write_text(
            _minimal_notebook(), encoding="utf-8"
        )
    (root / "config.toml").write_text(
        "[grading]\n"
        "rubric = 'rubrics/exam.toml'\n"
        "system_prompt = 'prompt/system.md'\n"
        "provider = 'deepseek'\n"
        "[plagiarism]\n"
        "template_file = ''\n",
        encoding="utf-8",
    )


def test_empty_template_file_uses_the_default_template(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """template_file = "" counts as unset (the default template.ipynb beside
    the config). Before the fix "" resolved to the config's own directory —
    which exists() — and the stage died with "Unsupported input type for
    extraction: <assignment dir>"."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _empty_template_config(root)
        (root / "template.ipynb").write_text(_minimal_notebook(), encoding="utf-8")

        summary = detect_plagiarism(root / "config.toml")

        assert summary is not None, "empty template_file must not abort the run"
        assert summary["success"] == 2, summary
        extracted = root / "plagiarism" / "template" / "template.py"
        assert "print(1)" in extracted.read_text(encoding="utf-8")
        assert "template not found" not in capsys.readouterr().out


def test_empty_template_file_without_template_runs_without_removal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty template_file with no template.ipynb beside the config
    degrades to the existing "running without boilerplate removal" path
    instead of raising."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _empty_template_config(root)

        summary = detect_plagiarism(root / "config.toml")

        assert summary is not None, "empty template_file must not abort the run"
        assert summary["success"] == 2, summary
        out = capsys.readouterr().out
        assert "running without boilerplate removal" in out, out


def test_directory_template_path_runs_without_removal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non-file path (directory) at the template location degrades the same
    way — the is_file() guard replaces exists(), which is true for a dir."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _empty_template_config(root)
        (root / "config.toml").write_text(
            (root / "config.toml")
            .read_text(encoding="utf-8")
            .replace("template_file = ''", 'template_file = "raw"'),
            encoding="utf-8",
        )

        summary = detect_plagiarism(root / "config.toml")

        assert summary is not None
        assert summary["success"] == 2, summary
        assert "running without boilerplate removal" in capsys.readouterr().out


def test_existing_unsupported_template_type_runs_without_removal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A template at an existing path with an unsupported suffix (notes.txt)
    is no more extractable than a missing one: the stage degrades to the
    no-template branch (naming the path) instead of dying in extraction with
    the "Unsupported input type for extraction" error the empty string used
    to trigger."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _empty_template_config(root)
        (root / "config.toml").write_text(
            (root / "config.toml")
            .read_text(encoding="utf-8")
            .replace("template_file = ''", 'template_file = "notes.txt"'),
            encoding="utf-8",
        )
        (root / "notes.txt").write_text("plain notes, not a notebook", encoding="utf-8")

        summary = detect_plagiarism(root / "config.toml")

        assert summary is not None, "unsupported template type must not abort the run"
        assert summary["success"] == 2, summary
        out = capsys.readouterr().out
        assert "running without boilerplate removal" in out, out
        assert "notes.txt" in out, out


def test_empty_full_pairs_file_uses_the_default() -> None:
    """Same-class: every path-valued [plagiarism] key normalizes through one
    helper. full_pairs_file = "" would otherwise resolve to the plagiarism
    output dir itself and the pair-data write would fail."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _empty_template_config(root)
        (root / "config.toml").write_text(
            (root / "config.toml")
            .read_text(encoding="utf-8")
            .replace("template_file = ''", 'full_pairs_file = ""'),
            encoding="utf-8",
        )

        summary = detect_plagiarism(root / "config.toml")

        assert summary is not None
        assert summary["success"] == 2, summary
        pairs = json.loads(
            (root / "plagiarism" / "all_pairs.json").read_text(encoding="utf-8")
        )
        assert pairs["pair_count"] == 1, pairs


def test_all_six_path_keys_treat_empty_and_whitespace_as_unset() -> None:
    """Batch A's read-side normalization covers exactly these six path-valued
    keys: "" and pure whitespace both resolve to the field default under
    their own base dir — never the assignment directory itself (the reported
    crash shape)."""
    defaults = PlagiarismSection()
    with tempfile.TemporaryDirectory() as tmp:
        assignment_dir = Path(tmp) / "course" / "100001"
        assignment_dir.mkdir(parents=True)
        config_path = assignment_dir / "config.toml"
        out_dir = assignment_dir / defaults.output_dir  # output_dir stays unset
        expected = {
            "output_dir": out_dir,
            "template_file": assignment_dir / defaults.template_file,
            "submissions_subdir": out_dir / defaults.submissions_subdir,
            "template_subdir": out_dir / defaults.template_subdir,
            "report_file": out_dir / defaults.report_file,
            "full_pairs_file": out_dir / defaults.full_pairs_file,
        }

        def resolved_paths(cfg: PlagiarismConfig) -> dict[str, Path]:
            return {
                "output_dir": cfg.output_dir,
                "template_file": cfg.template_file,
                "submissions_subdir": cfg.submissions_dir,
                "template_subdir": cfg.template_dir,
                "report_file": cfg.report_file,
                "full_pairs_file": cfg.full_pairs_file,
            }

        for field, want in expected.items():
            for value in ("", "   "):
                config_path.write_text(
                    "[grading]\n"
                    "rubric = 'rubrics/exam.toml'\n"
                    "system_prompt = 'prompt/system.md'\n"
                    "provider = 'deepseek'\n"
                    "[plagiarism]\n"
                    f'{field} = "{value}"\n',
                    encoding="utf-8",
                )
                cfg = _load_plagiarism_config(config_path)
                actual = resolved_paths(cfg)[field]
                assert actual == want.resolve(), f"{field}={value!r} -> {actual}"
                assert actual != assignment_dir.resolve(), (
                    f"{field}={value!r} collapsed to the assignment directory"
                )


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
