# These are developer commands, not intended for end-users.
# You can safely ignore this file if you only intend to use the framework.

[doc("Format code and apply safe linting fixes")]
fix:
    ruff format .
    ruff check --fix .
    uvx -w mdformat-gfm mdformat --number README.md docs/*.md docs/config/*.md

[doc("Check code formatting and linting")]
check:
    ruff format --check .
    ruff check .
    uvx -w mdformat-gfm mdformat --check --number README.md docs/*.md docs/config/*.md

[doc("Run cross-assignment plagiarism analysis")]
plagiarism:
    uv run main.py plagiarism -c data/config.toml --aggregate -o data/plagiarism-report.txt

[doc("Run the full pytest suite (serial)")]
test:
    uv run pytest -q

[doc("Run the full pytest suite in parallel (pytest-xdist)")]
test-fast:
    uv run pytest -q -n auto

[doc("Run all 13 headless TUI check scripts in parallel (JOBS=N to override, default 8)")]
test-e2e:
    #!/usr/bin/env bash
    set -uo pipefail
    JOBS="${JOBS:-8}"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    export tmp
    scripts=(
        tests/tata_app_check.py
        tests/tata_dash_check.py
        tests/tata_fetchall_check.py
        tests/tata_library_check.py
        tests/tata_modal_check.py
        tests/tata_palette_check.py
        tests/tata_plagiarism_check.py
        tests/tata_rubric_check.py
        tests/tata_settings_check.py
        tests/tata_workspace_check.py
        tests/review_screen_check.py
        tests/preview_check.py
        tests/tata_realtime_check.py
    )
    printf '%s\n' "${scripts[@]}" | xargs -P "$JOBS" -I{} bash -c '
        name="$(basename "$1")"
        if uv run python "$1" >"$tmp/$name.log" 2>&1; then rc=0; else rc=$?; fi
        printf "%s\n" "$rc" >"$tmp/$name.rc"
    ' _ {}
    failed=0
    for s in "${scripts[@]}"; do
        name="$(basename "$s")"
        rc="$(cat "$tmp/$name.rc" 2>/dev/null || echo 1)"
        if [ "$rc" = "0" ]; then
            echo "OK   $s"
        else
            echo "FAIL $s (exit $rc)"
            echo "--- last 30 lines of $name.log ---"
            tail -n 30 "$tmp/$name.log"
            echo "--- end $name.log ---"
            failed=1
        fi
    done
    if [ "$failed" = "0" ]; then
        echo "test-e2e: 13/13 OK"
    else
        echo "test-e2e: FAILED"
        exit 1
    fi
