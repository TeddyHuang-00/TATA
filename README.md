# TATA

TATA stands for TATA-Assisted Teaching Assistant.

TATA is a configuration-driven grading pipeline for human TAs. It preprocesses submissions, runs LLM-based grading against a rubric, and generates score summaries.

## Core Features

- Config-driven assignment workflow
- Optional default paths for assignment folders
- Dynamic rubric-based grading schema generation
- Parallel grading with bounded concurrency
- Stage-based CLI: preprocess, grade, score, analyze, all, schema

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

5. Run post-scoring meta analysis (optional):

   ```bash
   uv run main.py --stage analyze --config assignments/my-assignment/config.toml
   ```

## Documentation

- Onboarding: [docs/onboarding.md](docs/onboarding.md)
- Reuse and template guidance: [docs/reuse.md](docs/reuse.md)
- FAQ: [docs/faq.md](docs/faq.md)
- Troubleshooting: [docs/troubleshooting.md](docs/troubleshooting.md)

## Starter Assets

- Example assignment config: [assignments/example/config.toml](assignments/example/config.toml)
- Example rubric: [rubrics/example_rubric.toml](rubrics/example_rubric.toml)
- Generic lab prompt: [prompt/lab.md](prompt/lab.md)
