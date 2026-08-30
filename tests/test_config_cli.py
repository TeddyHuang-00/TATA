from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from pydantic_settings import get_subcommand
from src.cli_options import (
    ConfigCliOptions,
    ConfigSetCliOptions,
    TataCli,
    parse_cli_args,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

ASSIGNMENT_CONFIG = (
    "[grading]\n"
    'rubric = "rubrics/a1.toml"\n'
    'system_prompt = "prompt/system.md"\n'
    'provider = "deepseek_chat_tool"\n'
    "max_parallel_tasks = 4\n"
    "\n"
    "[fetch]\n"
    "# keep this comment\n"
    "course_id = 111\n"
    'mode = "auto"\n'
    'custom_field = "user made"\n'
)


def _parse(*args: str) -> ConfigSetCliOptions:
    cmd = parse_cli_args(TataCli, argv=list(args))
    sub = get_subcommand(cmd, is_required=False)
    assert isinstance(sub, ConfigCliOptions)
    assert sub.set is not None
    return sub.set


def _run_main(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "main.py", *args],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        check=False,
    )


def _assignment_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(ASSIGNMENT_CONFIG, encoding="utf-8")
    return cfg


def test_parses_config_set_args() -> None:
    args = _parse("config", "set", "-c", "x.toml", "fetch.course_id", "5")
    assert args.config == Path("x.toml")
    assert args.key == "fetch.course_id"
    assert args.value == "5"


def test_set_preserves_comments_and_unrelated_keys(tmp_path: Path) -> None:
    cfg = _assignment_config(tmp_path)
    proc = _run_main("config", "set", "-c", str(cfg), "fetch.course_id", "2222")
    assert proc.returncode == 0
    text = cfg.read_text(encoding="utf-8")
    assert "course_id = 2222" in text  # the edit was applied
    assert "# keep this comment" in text  # comment survives the round-trip
    assert 'custom_field = "user made"' in text  # unrelated key survives
    assert "course_id = 111" not in text


def test_invalid_value_rejected_and_file_untouched(tmp_path: Path) -> None:
    cfg = _assignment_config(tmp_path)
    before = cfg.read_text(encoding="utf-8")
    proc = _run_main(
        "config", "set", "-c", str(cfg), "grading.max_parallel_tasks", "99"
    )
    assert proc.returncode == 1
    assert "max_parallel_tasks" in proc.stderr
    assert cfg.read_text(encoding="utf-8") == before


def test_unknown_section_rejected_and_file_untouched(tmp_path: Path) -> None:
    cfg = _assignment_config(tmp_path)
    before = cfg.read_text(encoding="utf-8")
    proc = _run_main("config", "set", "-c", str(cfg), "bogus.k", "1")
    assert proc.returncode == 1
    assert "unknown config section 'bogus'" in proc.stderr
    assert cfg.read_text(encoding="utf-8") == before


def test_weight_sum_rule_rejected(tmp_path: Path) -> None:
    cfg = _assignment_config(tmp_path)
    proc = _run_main(
        "config", "set", "-c", str(cfg), "plagiarism.copydetect_weight", "0.7"
    )
    assert proc.returncode == 1
    assert "plagiarism weights" in proc.stderr


def test_invalid_toml_rejected_and_not_clobbered(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("not toml", encoding="utf-8")
    proc = _run_main("config", "set", "-c", str(cfg), "fetch.course_id", "1")
    assert proc.returncode == 1
    assert "invalid TOML" in proc.stderr
    assert cfg.read_text(encoding="utf-8") == "not toml"


def test_key_without_dot_rejected(tmp_path: Path) -> None:
    cfg = _assignment_config(tmp_path)
    proc = _run_main("config", "set", "-c", str(cfg), "justkey", "1")
    assert proc.returncode == 1
    assert "section.key" in proc.stderr


def test_dotted_key_sets_value(tmp_path: Path) -> None:
    cfg = _assignment_config(tmp_path)
    proc = _run_main("config", "set", "-c", str(cfg), "grading.max_parallel_tasks", "4")
    assert proc.returncode == 0
    assert "max_parallel_tasks = 4" in cfg.read_text(encoding="utf-8")


def test_value_coercion(tmp_path: Path) -> None:
    cfg = _assignment_config(tmp_path)
    for key, value in (
        ("grading.max_parallel_tasks", "6"),
        ("processing.remove_base64_images", "true"),
        ("grading.rubric", "hello world"),
    ):
        proc = _run_main("config", "set", "-c", str(cfg), key, value)
        assert proc.returncode == 0, proc.stderr
    text = cfg.read_text(encoding="utf-8")
    assert "max_parallel_tasks = 6" in text  # int, unquoted
    assert "remove_base64_images = true" in text  # bool
    assert 'rubric = "hello world"' in text  # quoted string


def test_set_creates_missing_file(tmp_path: Path) -> None:
    """Regression (F3): `config set -c NEW.toml grading.max_parallel_tasks 4`
    bootstraps a missing file instead of failing on rubric Field required."""
    cfg = tmp_path / "new.toml"
    assert not cfg.exists()
    proc = _run_main("config", "set", "-c", str(cfg), "grading.max_parallel_tasks", "4")
    assert proc.returncode == 0, proc.stderr
    text = cfg.read_text(encoding="utf-8")
    assert "max_parallel_tasks = 4" in text  # int value


def test_set_fetch_on_missing_file(tmp_path: Path) -> None:
    cfg = tmp_path / "new.toml"
    proc = _run_main("config", "set", "-c", str(cfg), "fetch.course_id", "111")
    assert proc.returncode == 0, proc.stderr
    assert "course_id = 111" in cfg.read_text(encoding="utf-8")


def test_set_unknown_section_still_rejected_on_missing_file(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "new.toml"
    proc = _run_main("config", "set", "-c", str(cfg), "bogus.k", "1")
    assert proc.returncode == 1
    assert "unknown config section 'bogus'" in proc.stderr
    assert not cfg.exists()


BROKEN_WEIGHTS_CONFIG = ASSIGNMENT_CONFIG + (
    "\n[plagiarism]\ncopydetect_weight = 0.9\nembedding_weight = 0.05\n"
)


def test_grading_edit_succeeds_on_broken_weights(tmp_path: Path) -> None:
    """Regression (F4): weight-sum rule only guards [plagiarism] edits — an
    unrelated grading edit on a pre-broken config must not be bricked."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(BROKEN_WEIGHTS_CONFIG, encoding="utf-8")
    proc = _run_main("config", "set", "-c", str(cfg), "grading.max_parallel_tasks", "4")
    assert proc.returncode == 0, proc.stderr
    text = cfg.read_text(encoding="utf-8")
    assert "max_parallel_tasks = 4" in text
    assert "copydetect_weight = 0.9" in text  # weights untouched
    assert "embedding_weight = 0.05" in text


def test_weight_sum_still_applies_to_plagiarism_edits(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(BROKEN_WEIGHTS_CONFIG, encoding="utf-8")
    proc = _run_main(
        "config", "set", "-c", str(cfg), "plagiarism.copydetect_weight", "0.6"
    )
    assert proc.returncode == 1
    assert "plagiarism weights" in proc.stderr
