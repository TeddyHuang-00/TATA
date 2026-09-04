from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from instructor import Mode
from pydantic_settings import get_subcommand
from src.shared.cli_options import (
    RubricCliOptions,
    RubricGenCliOptions,
    TataCli,
    parse_cli_args,
)
from src.shared.provider import ProviderInfo, ProviderList
from src.shared.rubric import RubricDefinition, get_rubric_definition
from src.shared.rubric_gen import _validate_rubric_content, generate_rubric

GRADING_CONFIG = (
    "[grading]\n"
    'rubric = "rubrics/r.toml"\n'
    'system_prompt = ["prompt/system.md"]\n'
    'provider = "test"\n'
)


def _setup_env(
    tmp_path: Path, *, with_md: bool = True, config: str = GRADING_CONFIG
) -> tuple[Path, Path]:
    """Course/assignment layout so config paths resolve like real data."""
    a_dir = tmp_path / "data" / "c1" / "a1"
    a_dir.mkdir(parents=True)
    (a_dir / "config.toml").write_text(config, encoding="utf-8")
    if with_md:
        (a_dir / "assignment.md").write_text(
            "# HW1\nWrite a program that prints hello.\n", encoding="utf-8"
        )
    out = tmp_path / "data" / "rubrics" / "a1.toml"
    return a_dir / "config.toml", out


def _fake_client(rubric: RubricDefinition) -> MagicMock:
    """Instructor client whose create returns a response_model instance."""
    client = MagicMock()
    client.chat.completions.create.return_value = rubric
    return client


def _patch_deps(monkeypatch: pytest.MonkeyPatch, rubric: RubricDefinition) -> MagicMock:
    client = _fake_client(rubric)
    monkeypatch.setattr(
        "src.shared.rubric_gen.build_client", lambda name: (client, "m1")
    )
    monkeypatch.setattr(
        "src.shared.rubric_gen.get_providers",
        lambda: ProviderList(
            providers={
                "test": ProviderInfo(
                    base_url="http://test",
                    api_key="sk-test",
                    model="m1",
                    mode=Mode.TOOLS,
                    temperature=0.0,
                )
            }
        ),
    )
    return client


def _valid_rubric() -> RubricDefinition:
    return RubricDefinition.model_validate({
        "criterion": [
            {
                "name": "Correctness",
                "desc": "The program prints hello exactly; any deviation fails.",
                "pts": 10,
                "rating": "binary",
                "grading": "standard",
            },
            {
                "name": "Style",
                "desc": "Code is commented and follows PEP 8.",
                "pts": 5,
                "rating": "likert",
                "grading": "custom",
                "custom_scale": [0, 0.25, 0.5, 0.75, 1.0],
            },
        ]
    })


def test_generate_rubric_writes_readable_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Normal path: TOML written and readable back with the same criteria."""
    config_path, out = _setup_env(tmp_path)
    rubric = _valid_rubric()
    client = _patch_deps(monkeypatch, rubric)

    result = generate_rubric(config_path, out)

    assert result is rubric
    assert (
        client.chat.completions.create.call_args.kwargs["response_model"]
        is RubricDefinition
    )
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    assert "[[criterion]]" in text
    assert "# schema" not in text
    loaded = get_rubric_definition(out)
    assert [c.name for c in loaded.criterion] == ["Correctness", "Style"]
    assert loaded.criterion[0].pts == 10
    assert loaded.criterion[1].grading == "custom"
    assert loaded.criterion[1].custom_scale == [0, 0.25, 0.5, 0.75, 1.0]


def test_generate_rubric_missing_assignment_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """assignment.md absent -> error telling the user to fetch first."""
    config_path, out = _setup_env(tmp_path, with_md=False)
    _patch_deps(monkeypatch, _valid_rubric())

    with pytest.raises(ValueError, match="fetch"):
        generate_rubric(config_path, out)
    assert not out.exists()


def test_generate_rubric_rejects_empty_criteria(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM returned a structurally valid but empty rubric -> content check fails."""
    config_path, out = _setup_env(tmp_path)
    _patch_deps(monkeypatch, RubricDefinition(criterion=[]))

    with pytest.raises(ValueError, match="at least one"):
        generate_rubric(config_path, out)
    assert not out.exists()


def test_generate_rubric_refuses_existing_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing out file -> ValueError; original content untouched, no LLM call."""
    config_path, out = _setup_env(tmp_path)
    out.parent.mkdir(parents=True)
    out.write_text("# keep me\n", encoding="utf-8")
    client = _patch_deps(monkeypatch, _valid_rubric())

    with pytest.raises(ValueError, match="already exists"):
        generate_rubric(config_path, out)

    assert out.read_text(encoding="utf-8") == "# keep me\n"
    client.chat.completions.create.assert_not_called()


class TestValidateRubricContent:
    def test_rejects_nonpositive_pts(self) -> None:
        rubric = _valid_rubric()
        rubric.criterion[0].pts = 0
        with pytest.raises(ValueError, match="non-positive"):
            _validate_rubric_content(rubric)

    def test_rejects_empty_name(self) -> None:
        rubric = _valid_rubric()
        rubric.criterion[0].name = "  "
        with pytest.raises(ValueError, match="empty name"):
            _validate_rubric_content(rubric)

    def test_rejects_empty_desc(self) -> None:
        rubric = _valid_rubric()
        rubric.criterion[0].desc = ""
        with pytest.raises(ValueError, match="empty desc"):
            _validate_rubric_content(rubric)


def test_generate_rubric_missing_grading_section(tmp_path: Path) -> None:
    """Config without [grading] -> load_assignment_file error."""
    config_path, out = _setup_env(
        tmp_path, config='[scoring]\nreport_detail = "slim"\n'
    )

    with pytest.raises(ValueError, match="grading"):
        generate_rubric(config_path, out)
    assert not out.exists()


def test_generate_rubric_unknown_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider named in config is not in the provider list -> error."""
    config_path, out = _setup_env(tmp_path)
    monkeypatch.setattr(
        "src.shared.rubric_gen.get_providers",
        lambda: ProviderList(
            providers={
                "other": ProviderInfo(
                    base_url="http://test",
                    api_key="sk-test",
                    model="m1",
                    mode=Mode.TOOLS,
                    temperature=0.0,
                )
            }
        ),
    )

    with pytest.raises(ValueError, match="Provider 'test' not found"):
        generate_rubric(config_path, out)


def test_parse_rubric_generate_cli(tmp_path: Path) -> None:
    """CLI wiring: `rubric generate -c ... -o ...` parses into the options."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(GRADING_CONFIG, encoding="utf-8")
    out = tmp_path / "out.toml"

    cmd = parse_cli_args(
        TataCli, argv=["rubric", "generate", "-c", str(cfg), "-o", str(out)]
    )
    sub = get_subcommand(cmd, is_required=False)
    assert isinstance(sub, RubricCliOptions)
    assert sub.generate is not None
    assert isinstance(sub.generate, RubricGenCliOptions)
    assert sub.generate.config == cfg
    assert sub.generate.out == out


def test_rubric_generate_default_out_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rubric generate -c ...` without -o writes to data/rubrics/<dir name>.toml."""
    from src.cli.main import _run_rubric_generate

    # src/cli/__init__.py re-exports main() (function); get the real module.
    cli_main = sys.modules["src.cli.main"]

    config_path, _ = _setup_env(tmp_path, with_md=False)
    rubric = _valid_rubric()
    monkeypatch.setattr(cli_main, "REPO_ROOT", tmp_path)

    def fake_generate(config: Path, out: Path) -> RubricDefinition:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("[[criterion]]\n", encoding="utf-8")
        return rubric

    monkeypatch.setattr(cli_main, "generate_rubric", fake_generate)

    cmd = parse_cli_args(TataCli, argv=["rubric", "generate", "-c", str(config_path)])
    _run_rubric_generate(get_subcommand(cmd, is_required=False).generate)

    assert (tmp_path / "data" / "rubrics" / "a1.toml").is_file()


def test_rubric_without_subcommand_exits_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare `rubric` -> error message, exit status 1."""
    from src.cli.main import main

    monkeypatch.setattr(sys, "argv", ["tata", "rubric"])
    with pytest.raises(SystemExit, match="rubric requires a subcommand") as exc:
        main()
    # sys.exit(str) sets the code to that string; the process status is 1.
    assert str(exc.value.code).startswith("error:")
