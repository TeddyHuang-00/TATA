from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from pydantic_settings import get_subcommand
from src.shared.cli_options import (
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
    args = _parse("fetch", "111111", "222225")
    assert args.course == 111111
    assert args.assignment == 222225
    assert args.retry is False
    assert args.config is None


def test_defaults() -> None:
    args = _parse("fetch")
    assert args.course is None
    assert args.assignment is None
    assert args.retry is False
    assert args.config is None


def test_config_string() -> None:
    args = _parse("fetch", "--config", "pyproject.toml")
    assert args.config == Path("pyproject.toml")


def test_out_no_longer_accepted(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _parse("fetch", "--out", "x")
    assert excinfo.value.code == 2


def test_retry_mode_ignored_with_out(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _parse("fetch", "--retry", "--out", "x")
    assert excinfo.value.code == 2


def test_course_without_assignment_rejected(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _parse("fetch", "111111")
    assert excinfo.value.code == 2
    assert "must be given together" in capsys.readouterr().err


def test_mode_no_longer_accepted(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _parse("fetch", "1", "2", "--mode", "text")
    assert excinfo.value.code == 2


def test_retry_allows_single_course_filter() -> None:
    args = _parse("fetch", "--retry", "111111")
    assert args.retry is True
    assert args.course == 111111
    assert args.assignment is None


def test_main_fetch_course_without_assignment_exits_2() -> None:
    proc = _run_main("fetch", "111111")
    assert proc.returncode == 2
    assert "must be given together" in proc.stderr


def test_main_fetch_out_exits_2() -> None:
    proc = _run_main("fetch", "--out", "x")
    assert proc.returncode == 2


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


def test_fetch_entries_uses_list_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main_mod = __import__("src.cli.main", fromlist=["_"])
    from src.shared.assignment_config import FetchSection

    cfg = FetchSection.model_validate({
        "course_id": 111111,
        "assignments": [
            {"id": 11},
            {"id": 12},
        ],
    })
    calls: list[tuple[int, int, str]] = []
    monkeypatch.setattr(
        main_mod,
        "fetch_assignment",
        lambda canvas, cid, aid, out: calls.append((cid, aid, str(out))),
    )
    cfg_path = tmp_path / "data" / "config.toml"
    main_mod._fetch_entries(object(), 111111, cfg_path, cfg)
    assert calls == [
        (111111, 11, str((tmp_path / "data/11/raw").resolve())),
        (111111, 12, str((tmp_path / "data/12/raw").resolve())),
    ]


def test_retry_finds_course_config_list(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression (F1): --retry must scan the repo root, not src/.

    The retry scan is repo-root-relative (data/<course>/config.toml); with
    cli.py in src/ the old ``Path(__file__).parent`` resolved to src/ and the
    scan always reported 'no assignment configs...'."""
    main_mod = __import__("src.cli.main", fromlist=["_"])

    course = tmp_path / "data" / "111111"
    (course / "222222").mkdir(parents=True)
    (course / "config.toml").write_text(
        "[fetch]\ncourse_id = 111111\n\n[[fetch.assignments]]\nid = 222222\n",
        encoding="utf-8",
    )
    (course / "222222" / "config.toml").write_text(
        "[grading]\n"
        'rubric = "rubrics/a.toml"\n'
        'system_prompt = "prompt/system.md"\n'
        'provider = "deepseek_chat_tool"\n',
        encoding="utf-8",
    )

    calls: list[tuple[int, int, str]] = []
    monkeypatch.setattr(main_mod, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(main_mod, "load_env", lambda: ("https://x", "t"))
    monkeypatch.setattr(main_mod, "Canvas", lambda *a, **k: object())
    monkeypatch.setattr(
        main_mod,
        "fetch_assignment",
        lambda canvas, cid, aid, out: calls.append((cid, aid, str(out))),
    )
    main_mod._retry_fetch(None, None)  # no SystemExit: configs were found
    assert calls == [(111111, 222222, str((course / "222222" / "raw").resolve()))]


def test_run_fetch_course_config_positional_derives_aid_raw_out(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A course config + positional course/assignment derive the fetch out
    dir as <course dir>/<aid>/raw (no stored out dir anymore)."""
    main_mod = __import__("src.cli.main", fromlist=["_"])

    course = tmp_path / "data" / "111111"
    course.mkdir(parents=True)
    cfg_path = course / "config.toml"
    cfg_path.write_text("[fetch]\ncourse_id = 111111\n", encoding="utf-8")

    calls: list[tuple[int, int, str]] = []
    monkeypatch.setattr(main_mod, "load_env", lambda: ("https://x", "t"))
    monkeypatch.setattr(main_mod, "Canvas", lambda *a, **k: object())
    monkeypatch.setattr(
        main_mod,
        "fetch_assignment",
        lambda canvas, cid, aid, out: calls.append((cid, aid, str(out))),
    )
    main_mod._run_fetch(
        FetchCliOptions(course=111111, assignment=222333, config=cfg_path)
    )
    assert calls == [(111111, 222333, str((course / "222333" / "raw").resolve()))]
    # The fetch was remembered as a [[fetch.assignments]] entry (no [fetch].mode).
    fetch = tomllib.loads(cfg_path.read_text())["fetch"]
    assert fetch["course_id"] == 111111
    assert "mode" not in fetch
    assert [e["id"] for e in fetch["assignments"]] == [222333]


def test_run_fetch_assignment_config_uses_course_fetch_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A standalone fetch -c <assignment config> uses the course config
    above it for course_id; the entry is remembered there (id only)."""
    main_mod = __import__("src.cli.main", fromlist=["_"])

    course = tmp_path / "data" / "111111"
    (course / "222333").mkdir(parents=True)
    (course / "config.toml").write_text(
        "[fetch]\ncourse_id = 111111\n", encoding="utf-8"
    )
    (course / "222333" / "config.toml").write_text(
        "[grading]\n"
        'rubric = "rubrics/a.toml"\n'
        'system_prompt = "prompt/system.md"\n'
        'provider = "deepseek_chat_tool"\n',
        encoding="utf-8",
    )

    calls: list[tuple[int, int, str]] = []
    monkeypatch.setattr(main_mod, "load_env", lambda: ("https://x", "t"))
    monkeypatch.setattr(main_mod, "Canvas", lambda *a, **k: object())
    monkeypatch.setattr(
        main_mod,
        "fetch_assignment",
        lambda canvas, cid, aid, out: calls.append((cid, aid, str(out))),
    )
    main_mod._run_fetch(FetchCliOptions(config=course / "222333" / "config.toml"))
    assert calls == [(111111, 222333, str((course / "222333" / "raw").resolve()))]
    fetch = tomllib.loads((course / "config.toml").read_text())["fetch"]
    assert fetch["course_id"] == 111111
    assert "mode" not in fetch
    assert [e["id"] for e in fetch["assignments"]] == [222333]


def test_run_fetch_non_numeric_assignment_dir_exits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An assignment config in a non-numeric dir with no --assignment exits
    instead of falling into the interactive picker (TUI raw-mode trap)."""
    main_mod = __import__("src.cli.main", fromlist=["_"])

    course = tmp_path / "data" / "111111"
    (course / "alpha").mkdir(parents=True)
    (course / "config.toml").write_text(
        "[fetch]\ncourse_id = 111111\n", encoding="utf-8"
    )
    (course / "alpha" / "config.toml").write_text(
        "[grading]\n"
        'rubric = "rubrics/a.toml"\n'
        'system_prompt = "prompt/system.md"\n'
        'provider = "deepseek_chat_tool"\n',
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exc:
        main_mod._run_fetch(FetchCliOptions(config=course / "alpha" / "config.toml"))
    assert "not a numeric id" in str(exc.value)


def test_remember_never_writes_mode_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An entry fetched under the course config records no mode key — the
    [[fetch.assignments]] entry is id-only."""
    main_mod = __import__("src.cli.main", fromlist=["_"])

    course = tmp_path / "data" / "111111"
    (course / "222333").mkdir(parents=True)
    (course / "config.toml").write_text(
        "[fetch]\ncourse_id = 111111\n", encoding="utf-8"
    )
    (course / "222333" / "config.toml").write_text(
        "[grading]\n"
        'rubric = "rubrics/a.toml"\n'
        'system_prompt = "prompt/system.md"\n'
        'provider = "deepseek_chat_tool"\n',
        encoding="utf-8",
    )

    calls: list[tuple[int, int, str]] = []
    monkeypatch.setattr(main_mod, "load_env", lambda: ("https://x", "t"))
    monkeypatch.setattr(main_mod, "Canvas", lambda *a, **k: object())
    monkeypatch.setattr(
        main_mod,
        "fetch_assignment",
        lambda canvas, cid, aid, out: calls.append((cid, aid, str(out))),
    )
    main_mod._run_fetch(FetchCliOptions(config=course / "222333" / "config.toml"))
    assert calls == [(111111, 222333, str((course / "222333" / "raw").resolve()))]
    fetch = tomllib.loads((course / "config.toml").read_text())["fetch"]
    assert "mode" not in fetch
    assert [e["id"] for e in fetch["assignments"]] == [222333]
