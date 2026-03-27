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

2. Generate schemas:

   ```bash
   uv run main.py --stage schema
   ```

3. Create assignment config from [assignments/example/config.toml](assignments/example/config.toml)

4. Run all stages:

   ```bash
   uv run main.py --stage all --config assignments/my-assignment/config.toml
   ```

   `all` runs in this order: plagiarism -> preprocess -> grade -> score -> analyze.

5. Run plagiarism detection only (optional):

   ```bash
   uv run main.py --stage plagiarism --config assignments/my-assignment/config.toml
   ```

6. Run post-scoring meta analysis (optional):

   ```bash
   uv run main.py --stage analyze --config assignments/my-assignment/config.toml
   ```

7. Audit reference notebook TODO/instruction mismatches (optional):

   ```bash
   uv run python misc/reference_mismatch_audit.py \
      --notebook assignments/my-assignment/reference.ipynb
   ```

8. Aggregate plagiarism results across assignments (optional):

   ```bash
   uv run python misc/plagiarism_report_aggregate.py \
      --pairwise-alpha 0.01 \
      --individual-alpha 0.01 \
      --output misc/plagiarism_summary.md
   ```

   The report uses pairwise deletion + logit transform + per-assignment z-score + Stouffer aggregation,
   and also includes an individual-level detector using per-assignment max similarity with Gumbel fitting.
   It outputs statistically significant pairs and students under separate thresholds.
   Data source is `plagiarism/all_pairs.json` (full pair export).
   CLI options are parsed via Pydantic/pydantic-settings.

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
- Plagiarism aggregate report script: [misc/plagiarism_report_aggregate.py](misc/plagiarism_report_aggregate.py)
