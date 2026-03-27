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
    uv run misc/plagiarism_report_aggregate.py --pa 0.01 --ia 0.05 -o misc/plagiarism-report.txt
