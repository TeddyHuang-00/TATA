from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic_settings import get_subcommand
from src.cli_options import (
    FetchCliOptions,
    GradeCliOptions,
    TataCli,
    parse_cli_args,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _parse(*args: str) -> FetchCliOptions:
    cmd = parse_cli_args(TataCli, argv=list(args))
    sub = get_subcommand(cmd, is_required=False)
    assert isinstance(sub, FetchCliOptions)
    return sub


def _run_main(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "main.py", *args],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        check=False,
    )


def test_parses_full_options() -> None:
    args = _parse("fetch", "271218", "2979509", "--mode", "text")
    assert args.course == 271218
    assert args.assignment == 2979509
    assert args.mode == "text"
    assert args.retry is False
    assert args.out is None
    assert args.config is None


def test_defaults() -> None:
    args = _parse("fetch")
    assert args.course is None
    assert args.assignment is None
    assert args.mode == "auto"
    assert args.retry is False
    assert args.out is None
    assert args.config is None


def test_out_and_config_strings() -> None:
    args = _parse("fetch", "--out", "out", "--config", "pyproject.toml")
    assert args.out == "out"
    assert args.config == Path("pyproject.toml")


def test_retry_with_out_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _parse("fetch", "--retry", "--out", "x")
    assert excinfo.value.code == 2
    assert "ignored with --retry" in capsys.readouterr().err


def test_course_without_assignment_rejected(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _parse("fetch", "271218")
    assert excinfo.value.code == 2
    assert "must be given together" in capsys.readouterr().err


def test_invalid_mode_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _parse("fetch", "1", "2", "--mode", "bogus")
    assert excinfo.value.code == 2
    assert "Input should be 'attach', 'text' or 'auto'" in capsys.readouterr().err


def test_retry_allows_single_course_filter() -> None:
    args = _parse("fetch", "--retry", "271218")
    assert args.retry is True
    assert args.course == 271218
    assert args.assignment is None


def test_main_fetch_course_without_assignment_exits_2() -> None:
    proc = _run_main("fetch", "271218")
    assert proc.returncode == 2
    assert "must be given together" in proc.stderr


def test_main_fetch_retry_with_out_exits_2() -> None:
    proc = _run_main("fetch", "--retry", "--out", "x")
    assert proc.returncode == 2
    assert "ignored with --retry" in proc.stderr


def test_bare_env_vars_do_not_inject(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETRY", "true")
    monkeypatch.setenv("COURSE", "999")
    args = _parse("fetch", "1", "2")
    assert args.retry is False
    assert args.course == 1


def test_tata_prefixed_env_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TATA_RETRY", "true")
    args = _parse("fetch")
    assert args.retry is False


def test_config_nonexistent_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _parse("fetch", "--config", "/nonexistent.toml")
    assert excinfo.value.code == 2
    assert "not found" in capsys.readouterr().err


def test_no_subcommand_exits_2() -> None:
    proc = _run_main()
    assert proc.returncode == 2
    assert "subcommands" in proc.stderr


def test_unknown_subcommand_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        parse_cli_args(TataCli, argv=["doit"])
    assert excinfo.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_grade_config_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        parse_cli_args(TataCli, argv=["grade"])
    assert excinfo.value.code == 2
    assert "--config" in capsys.readouterr().err


def test_grade_force_and_config() -> None:
    cmd = parse_cli_args(TataCli, argv=["grade", "-c", "pyproject.toml", "--force"])
    sub = get_subcommand(cmd, is_required=False)
    assert isinstance(sub, GradeCliOptions)
    assert sub.force is True


def test_fetch_entries_uses_list_out_and_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.cli as main_mod
    from src.assignment_config import FetchSection

    cfg = FetchSection.model_validate({
        "course_id": 271218,
        "mode": "attach",
        "assignments": [
            {"assignment_id": 11, "out": "a/raw"},
            {"assignment_id": 12, "mode": "text", "out": "b/raw"},
        ],
    })
    calls: list[tuple[int, int, str, str]] = []
    monkeypatch.setattr(
        main_mod,
        "fetch_assignment",
        lambda canvas, cid, aid, out, mode: calls.append((cid, aid, str(out), mode)),
    )
    cfg_path = tmp_path / "data" / "config.toml"
    main_mod._fetch_entries(object(), 271218, cfg_path, cfg)
    assert calls == [
        (271218, 11, str((tmp_path / "data/a/raw").resolve()), "attach"),
        (271218, 12, str((tmp_path / "data/b/raw").resolve()), "text"),
    ]
