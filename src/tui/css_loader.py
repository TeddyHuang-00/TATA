from __future__ import annotations

from pathlib import Path


def load_css(name: str) -> str:
    return (Path(__file__).parent / "styles" / name).read_text()
