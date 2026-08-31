"""CLI entry point. Re-exports ``src.cli.main`` symbols for backward compatibility."""

from __future__ import annotations

from .main import *
from .main import (
    _STAGES,
    _ask_choice,
    _ask_number,
    _classify_config,
    _coerce_config_value,
    _fetch_course,
    _fetch_entries,
    _format_job_summary,
    _is_container,
    _load_config,
    _pick_interactive,
    _print_options,
    _remember,
    _repo_root,
    _retry_fetch,
    _root_fetch,
    _run_config_set,
    _run_fetch,
)
