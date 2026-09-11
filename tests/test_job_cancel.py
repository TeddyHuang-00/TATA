"""Guard: every callable handed to a job runner accepts ``cancel_event``.

``src.tui.jobs.run_stage_worker`` always calls the stage function with
``cancel_event=`` as a keyword argument (v10 batch 2 cancel contract). A
callable that does not accept it fails only at runtime, inside a worker
thread. This test scans every ``_start_job`` call site of the three
job-running screens and checks the real signature of the callable handed in:

- module-level names (workspace/plagiarism stage wrappers, imported shared
  stage functions) resolve via the module;
- ``partial(self._x, …)`` resolves to the bound class method (dashboard);
- local closures (the dashboard fetch-all ``job``) are checked through their
  ``FunctionDef`` arguments.

Runs in the normal pytest suite: ``uv run pytest -q tests/test_job_cancel.py``.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from src.tui import app as app_tui, plagiarism as plag_tui, workspace as ws_tui

TUI_DIR = Path(__file__).resolve().parents[1] / "src" / "tui"

MODULES = {
    "workspace.py": ws_tui,
    "plagiarism.py": plag_tui,
    "app.py": app_tui,
}


def _start_job_sites(filename: str) -> list[ast.Call]:
    tree = ast.parse((TUI_DIR / filename).read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_start_job"
    ]


def _class_method(module: object, name: str) -> object | None:
    """First class in the module carrying ``name`` (self._x resolution)."""
    for value in vars(module).values():
        if isinstance(value, type) and hasattr(value, name):
            return getattr(value, name)
    return None


def _local_def(filename: str, name: str) -> ast.FunctionDef | None:
    tree = ast.parse((TUI_DIR / filename).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _partial_self_attr(fn_arg: ast.expr) -> str | None:
    """Attribute name when ``fn_arg`` is ``partial(self._x, …)``, else None."""
    if not isinstance(fn_arg, ast.Call) or not isinstance(fn_arg.func, ast.Name):
        return None
    if fn_arg.func.id != "partial" or not fn_arg.args:
        return None
    first = fn_arg.args[0]
    if (
        isinstance(first, ast.Attribute)
        and isinstance(first.value, ast.Name)
        and first.value.id == "self"
    ):
        return first.attr
    return None


def _check(filename: str, module: object, fn_arg: ast.expr, line: int) -> str:
    """Assert the callable takes cancel_event; return a label for the report."""
    where = f"{filename}:{line}"
    name = _partial_self_attr(fn_arg)
    if name is not None:
        fn = _class_method(module, name)
        assert fn is not None, f"{where}: cannot resolve self.{name}"
        assert "cancel_event" in inspect.signature(fn).parameters, where
        return f"{where} self.{name}"
    if isinstance(fn_arg, ast.Name):
        if hasattr(module, fn_arg.id):
            fn = getattr(module, fn_arg.id)
            assert "cancel_event" in inspect.signature(fn).parameters, where
            return f"{where} {fn_arg.id}"
        node = _local_def(filename, fn_arg.id)  # local closure
        assert node is not None, f"{where}: cannot resolve {fn_arg.id}"
        arg_names = {a.arg for a in (*node.args.args, *node.args.kwonlyargs)}
        assert "cancel_event" in arg_names, f"{where}: {fn_arg.id} lacks cancel_event"
        return f"{where} closure {fn_arg.id}"
    message = f"{where}: unsupported fn argument {ast.dump(fn_arg)}"
    raise AssertionError(message)


def test_all_stage_callables_accept_cancel_event() -> None:
    checked: list[str] = []
    for filename, module in MODULES.items():
        sites = _start_job_sites(filename)
        assert sites, f"no _start_job call sites found in {filename}"
        for site in sites:
            assert len(site.args) >= 2, ast.dump(site)  # (stage, fn, ...)
            checked.append(_check(filename, module, site.args[1], site.lineno))
    # 5 workspace stage functions + 2 plagiarism wrappers + 2 dashboard paths
    assert len(checked) >= 9, checked
    print("\n".join(checked))  # pytest -s visibility
