# TATA

TATA stands for TATA-Assisted Teaching Assistant.

TATA is a configuration-driven grading pipeline for human TAs. It preprocesses submissions, runs plagiarism detection, performs LLM-based grading against a rubric, and generates score summaries.

## Core Features

- **Config-first architecture**: define assignment behavior in TOML, not ad-hoc scripts.
- **Dynamic schema validation**: assignment/provider/rubric models are validated with generated JSON schemas for safer edits and faster onboarding.
- **Rubric-driven LLM grading**: grading response schemas are generated from rubric criteria, keeping evaluation structure consistent and auditable.
- **Parallel grading engine**: bounded concurrency boosts throughput while preserving checkpointed progress and deterministic outputs.
- **Stage-level control**: run only what you need (`preprocess`, `plagiarism`, `grade`, `score`, `analyze`) or run the full pipeline with `all`.
- **Plagiarism coverage at two levels**: intra-assignment detection with boilerplate/template filtering plus inter-assignment aggregation with statistical significance analysis.
- **Extensible hook lifecycle**: inject custom logic before/after key events across preprocess, grade, score, analyze, and plagiarism without modifying core pipeline code.
- **Reference quality safeguards**: built-in TODO instruction/implementation audit to catch mismatch risks before grading at scale.

## Quick Start

1. Install dependencies:

   ```bash
   uv sync
   ```

1. Generate schemas:

   ```bash
   uv run main.py schema
   ```

1. Create assignment config from [assignments/example/config.toml](assignments/example/config.toml)

1. Run all stages:

   ```bash
   uv run main.py preprocess -c assignments/my-assignment/config.toml
   uv run main.py plagiarism -c assignments/my-assignment/config.toml
   uv run main.py grade -c assignments/my-assignment/config.toml
   uv run main.py score -c assignments/my-assignment/config.toml
   uv run main.py analyze -c assignments/my-assignment/config.toml
   ```

   Each stage runs in the order: plagiarism -> preprocess -> grade -> score -> analyze.

1. Run plagiarism detection only (optional):

   ```bash
   uv run main.py plagiarism -c assignments/my-assignment/config.toml
   ```

1. Run post-scoring meta analysis (optional):

   ```bash
   uv run main.py analyze -c assignments/my-assignment/config.toml
   ```

1. Audit reference notebook TODO/instruction mismatches (optional):

   ```bash
   uv run misc/reference_mismatch_audit.py \
      --notebook assignments/my-assignment/reference.ipynb
   ```

1. Aggregate plagiarism results across assignments (optional):

   ```bash
   uv run main.py plagiarism -c assignments/config.toml --aggregate -o assignments/plagiarism-report.txt
   ```

   `--config assignments/config.toml` runs plagiarism for every assignment under it
   (code submissions via copydetect on extracted notebook code, text/report
   submissions via copydetect on processed markdown blended 95/5 with embedding
   similarity). `--aggregate` appends the cross-assignment report: pairwise
   deletion + logit transform + per-assignment z-score + Stouffer aggregation,
   plus an individual-level detector using per-assignment max similarity with
   Gumbel fitting. It outputs statistically significant pairs and students under
   the alphas configured in the root config's `[plagiarism]` section.
   Data source is `plagiarism/all_pairs.json` (full pair export).

## Layered Config

Config is layered: the course-level root config `assignments/config.toml` holds
persistent fetch/plagiarism state shared across assignments (course id, fetch
mode, plagiarism weights/alphas); each `assignments/<name>/config.toml` holds
grading/scoring and assignment-specific overrides. Assignment values win per
key; all paths resolve against the assignment directory. Standalone assignment
configs (no root config) work exactly as before.

```toml
# assignments/config.toml (root layer, gitignored)
[fetch]
course_id = 271218
mode = "attach"

# assignments/0-10-first-colab/config.toml (assignment layer)
[grading]
rubric = "rubrics/0-10-first-colab.toml"
...
[fetch]
assignment_id = 2978557
```

## Documentation

- Onboarding: [docs/onboarding.md](docs/onboarding.md)
- Hooks lifecycle and IO contract: [docs/hooks.md](docs/hooks.md)
- Reuse and template guidance: [docs/reuse.md](docs/reuse.md)
- FAQ: [docs/faq.md](docs/faq.md)
- Troubleshooting: [docs/troubleshooting.md](docs/troubleshooting.md)
- Assignment config format (no schema required): [docs/config/assignment.md](docs/config/assignment.md)
- Provider config format (no schema required): [docs/config/provider.md](docs/config/provider.md)
- Rubric config format (no schema required): [docs/config/rubric.md](docs/config/rubric.md)

## Starter Assets

- Example assignment config: [assignments/example/config.toml](assignments/example/config.toml)
- Example rubric: [rubrics/example_rubric.toml](rubrics/example_rubric.toml)
- Generic system prompt: [prompt/system.md](prompt/system.md)
- Reference mismatch audit script: [misc/reference_mismatch_audit.py](misc/reference_mismatch_audit.py)
- Plagiarism: `uv run main.py plagiarism -c assignments/config.toml --aggregate`
