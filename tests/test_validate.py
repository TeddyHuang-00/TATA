from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from instructor import Mode
from src.cli.main import _run_validate
from src.shared.cli_options import ValidateCliOptions
from src.shared.provider import ProviderInfo, ProviderList

# `import src.cli.main` resolves to the re-exported `main` function (the
# package __init__ star-imports it), so fetch the module via importlib.
cli_main = importlib.import_module("src.cli.main")


def _setup_config_tree(tmp_path: Path) -> Path:
    """Course/assignment layout so config paths resolve like real data."""
    a_dir = tmp_path / "data" / "c1" / "a1"
    a_dir.mkdir(parents=True)
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


def _providers() -> ProviderList:
    return ProviderList(
        providers={
            "test": ProviderInfo(
                base_url="http://test",
                api_key="sk-test",
                model="m1",
                mode=Mode.TOOLS,
            )
        }
    )


def _patch_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_main, "get_providers", _providers)


def test_validate_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    config_path = _setup_config_tree(tmp_path)
    _patch_providers(monkeypatch)
    _run_validate(ValidateCliOptions(config=config_path))
    out = capsys.readouterr().out
    assert "rubric OK: r.toml (1 criteria)" in out
    assert "prompt OK: 1 file(s)" in out
    assert "provider OK: test" in out
    assert f"OK {config_path}" in out


def test_validate_missing_rubric_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    config_path = _setup_config_tree(tmp_path)
    (tmp_path / "data" / "rubrics" / "r.toml").unlink()
    _patch_providers(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        _run_validate(ValidateCliOptions(config=config_path))
    assert exc.value.code == 1
    assert "ERROR rubric" in capsys.readouterr().out


def test_validate_missing_provider_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    config_path = _setup_config_tree(tmp_path)
    monkeypatch.setattr(cli_main, "get_providers", lambda: ProviderList(providers={}))
    with pytest.raises(SystemExit) as exc:
        _run_validate(ValidateCliOptions(config=config_path))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "ERROR provider" in out
    assert "'test' not found" in out


def test_validate_missing_prompt_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    config_path = _setup_config_tree(tmp_path)
    (tmp_path / "data" / "prompt" / "system.md").unlink()
    _patch_providers(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        _run_validate(ValidateCliOptions(config=config_path))
    assert exc.value.code == 1
    assert "ERROR prompt file not found" in capsys.readouterr().out


def test_validate_invalid_config_exits_with_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[grading]\n", encoding="utf-8")
    _patch_providers(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        _run_validate(ValidateCliOptions(config=config_path))
    assert "error: Invalid assignment config" in str(exc.value.code)
