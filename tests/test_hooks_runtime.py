"""Regression tests for hook script containment and subprocess timeout.

Audit findings fixed here:
- a hooks.mounts script entry with an absolute path or '..' escaped the
  hooks dir (any Python file on the machine became executable as a hook);
- hooks.dir itself could escape the project root the same way;
- hook subprocesses ran with no timeout — one hung script stalled the
  whole pipeline (and the TUI job behind it) forever.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from src.shared import hooks_runtime as hooks_mod
from src.shared.assignment_config import load_assignment_file
from src.shared.hooks_runtime import HookRuntime

_NOOP_HOOK = "import sys, json\nprint(json.dumps(json.loads(sys.stdin.read())))\n"

_SLEEP_HOOK = (
    "import sys, json, time\n"
    "time.sleep(5)\n"
    "print(json.dumps(json.loads(sys.stdin.read())))\n"
)


def _setup(tmp_path: Path, hooks_toml: str) -> tuple[Path, Path]:
    """Create data/config.toml + hooks/ under tmp_path; return (config,
    hooks_dir)."""
    data_root = tmp_path / "data"
    hooks_dir = data_root / "hooks"
    hooks_dir.mkdir(parents=True)
    config_path = data_root / "config.toml"
    config_path.write_text(
        '[grading]\nrubric = "r.toml"\nsystem_prompt = ["p.md"]\n'
        'provider = "deepseek"\n\n' + hooks_toml,
        encoding="utf-8",
    )
    return config_path, hooks_dir


def test_hook_script_cannot_escape_hooks_dir_with_dotdot(tmp_path: Path) -> None:
    config_path, _ = _setup(
        tmp_path,
        '[hooks.mounts]\nafter_preprocess_file = "../escape.py"\n',
    )
    with pytest.raises(ValueError, match="escapes the hooks dir"):
        HookRuntime.from_config(
            load_assignment_file(config_path),
            assignment_config_path=config_path,
        )


def test_hook_script_cannot_escape_hooks_dir_with_absolute_path(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "evil.py"
    outside.write_text(_NOOP_HOOK, encoding="utf-8")
    config_path, _ = _setup(
        tmp_path,
        f'[hooks.mounts]\nafter_preprocess_file = "{outside.as_posix()}"\n',
    )
    with pytest.raises(ValueError, match="escapes the hooks dir"):
        HookRuntime.from_config(
            load_assignment_file(config_path),
            assignment_config_path=config_path,
        )


def test_hooks_dir_cannot_escape_project_root(tmp_path: Path) -> None:
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "h.py").write_text(_NOOP_HOOK, encoding="utf-8")
    config_path, _ = _setup(
        tmp_path,
        '[hooks]\ndir = "../outside"\n'
        '[hooks.mounts]\nafter_preprocess_file = "h.py"\n',
    )
    with pytest.raises(ValueError, match="must stay inside the project root"):
        HookRuntime.from_config(
            load_assignment_file(config_path),
            assignment_config_path=config_path,
        )


def test_hook_inside_hooks_dir_still_runs(tmp_path: Path) -> None:
    """Containment must not break the normal case."""
    config_path, hooks_dir = _setup(
        tmp_path,
        '[hooks.mounts]\nafter_preprocess_file = "h.py"\n',
    )
    (hooks_dir / "h.py").write_text(_NOOP_HOOK, encoding="utf-8")
    rt = HookRuntime.from_config(
        load_assignment_file(config_path),
        assignment_config_path=config_path,
    )
    assert rt is not None
    out = rt.run("after_preprocess_file", {"a": 1})
    assert out == {"a": 1}


def test_hook_timeout_raises_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, hooks_dir = _setup(
        tmp_path,
        '[hooks.mounts]\nafter_preprocess_file = "sleep.py"\n',
    )
    (hooks_dir / "sleep.py").write_text(_SLEEP_HOOK, encoding="utf-8")
    monkeypatch.setattr(hooks_mod, "HOOK_TIMEOUT_SECONDS", 0.5)
    rt = HookRuntime.from_config(
        load_assignment_file(config_path),
        assignment_config_path=config_path,
    )
    assert rt is not None
    with pytest.raises(RuntimeError, match="timed out"):
        rt.run("after_preprocess_file", {"a": 1})
