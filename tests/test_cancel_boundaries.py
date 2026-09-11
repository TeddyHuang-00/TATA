"""Cancel-boundary guards for the v10 batch-2 cooperative cancel contract.

Every stage callable accepts ``cancel_event=`` and checks it at item
boundaries; ``tests/test_processing.py`` covers preprocess, ``tests/
test_grading.py`` covers grade and ``tests/test_fetch_cli.py`` covers fetch.
This file hosts the two stages with no other test host:
``score_assignment`` (``src/shared/scoring.py``) and ``analyze_assignment``
(``src/shared/analysis.py``) — a pre-set event must short-circuit with the
zero summary and leave no artifacts behind.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from src.shared import scoring as scoring_mod
from src.shared.analysis import analyze_assignment
from src.shared.scoring import score_assignment

ZERO = {
    "success": 0,
    "errors": 0,
    "total": 0,
    "success_rate": 0,
}


def _setup_env(tmp_path: Path) -> Path:
    """Course/assignment layout so config paths resolve like real data
    (same shape as ``test_grading._setup_grade_env``): one graded file
    matching the rubric's single ``C1`` criterion, plus a logs/ dir."""
    a_dir = tmp_path / "data" / "c1" / "a1"
    (a_dir / "graded").mkdir(parents=True)
    (a_dir / "graded" / "100001.json").write_text(
        json.dumps({"c1": {"rating": "correct", "feedback": "ok"}}),
        encoding="utf-8",
    )
    (a_dir / "logs").mkdir()
    (tmp_path / "data" / "rubrics").mkdir(parents=True)
    (tmp_path / "data" / "rubrics" / "r.toml").write_text(
        '[[criterion]]\nname = "C1"\ndesc = "d"\npts = 10\nrating = "binary"\n'
        'grading = "standard"\n',
        encoding="utf-8",
    )
    (tmp_path / "data" / "prompt").mkdir(parents=True)
    (tmp_path / "data" / "prompt" / "system.md").write_text(
        "You are a TA.\n", encoding="utf-8"
    )
    (a_dir / "config.toml").write_text(
        '[grading]\nrubric = "rubrics/r.toml"\n'
        'system_prompt = ["prompt/system.md"]\nprovider = "test"\n',
        encoding="utf-8",
    )
    return a_dir / "config.toml"


def test_score_cancel_before_first_item_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-set cancel_event stops score before the first graded file:
    zero summary, ``score_submission`` never called, no scored file."""
    config_path = _setup_env(tmp_path)
    calls: list[object] = []

    def spy(*args: object, **kwargs: object) -> tuple[float, str]:
        calls.append(args)
        return 0.0, "summary"

    monkeypatch.setattr(scoring_mod, "score_submission", spy)
    cancel_event = threading.Event()
    cancel_event.set()

    result = score_assignment(config_path, cancel_event=cancel_event)

    assert result == {"stage": "score", **ZERO}
    assert calls == [], "score_submission must not run on a pre-set cancel event"
    scored = config_path.parent / "scored"
    assert [str(p) for p in scored.rglob("*") if p.is_file()] == []


def test_analyze_cancel_before_pass_returns_empty(tmp_path: Path) -> None:
    """A pre-set cancel_event short-circuits analyze before the pass: zero
    summary and no ``meta_analysis.{json,md}`` written."""
    config_path = _setup_env(tmp_path)
    cancel_event = threading.Event()
    cancel_event.set()

    result = analyze_assignment(config_path, cancel_event=cancel_event)

    assert result == {"stage": "analyze", **ZERO}
    logs = config_path.parent / "logs"
    assert not (logs / "meta_analysis.json").exists()
    assert not (logs / "meta_analysis.md").exists()
