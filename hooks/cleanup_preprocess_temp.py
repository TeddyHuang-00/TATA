from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys


def main() -> None:
    payload = json.load(sys.stdin)

    processed_dir = Path(str(payload.get("processed_dir", "")))
    temp_dir = processed_dir / ".hook_tmp"
    if temp_dir.exists() and temp_dir.is_dir():
        shutil.rmtree(temp_dir)

    json.dump(payload, sys.stdout)


if __name__ == "__main__":
    main()
