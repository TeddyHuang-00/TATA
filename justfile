# These are developer commands, not intended for end-users.
# You can safely ignore this file if you only intend to use the framework.

[doc("Format code and apply safe linting fixes")]
fix:
    ruff format .
    ruff check --fix .
    uvx -w mdformat-gfm mdformat --number README.md docs/**/*.md

[doc("Check code formatting and linting")]
check:
    ruff format --check .
    ruff check .
    uvx -w mdformat-gfm mdformat --check --number README.md docs/**/*.md

[doc("Run cross-assignment plagiarism analysis")]
plagiarism:
    uv run main.py plagiarism -c data/config.toml --aggregate -o data/plagiarism-report.txt

[doc("Run all 9 headless TUI check scripts")]
test-e2e:
    uv run python tests/tata_app_check.py
    uv run python tests/tata_dash_check.py
    uv run python tests/tata_fetchall_check.py
    uv run python tests/tata_modal_check.py
    uv run python tests/tata_plagiarism_check.py
    uv run python tests/tata_settings_check.py
    uv run python tests/tata_workspace_check.py
    uv run python tests/review_screen_check.py
    uv run python tests/preview_check.py
