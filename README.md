# TATA

TATA stands for TATA-Assisted Teaching Assistant.

TATA is a configuration-driven grading pipeline for human TAs. It preprocesses submissions, runs plagiarism detection, performs LLM-based grading against a rubric, and generates score summaries.

## Core Features

- **Config-first architecture**: define assignment behavior in TOML, not ad-hoc scripts.
- **Dynamic schema validation**: assignment/provider/rubric models are validated with generated JSON schemas for safer edits and faster onboarding.
- **Rubric-driven LLM grading**: grading response schemas are generated from rubric criteria, keeping evaluation structure consistent and auditable.
- **Parallel grading engine**: bounded concurrency boosts throughput while preserving checkpointed progress and deterministic outputs.
- **Stage-level control**: run only what you need (`preprocess`, `plagiarism`, `grade`, `score`, `analyze`, `fetch`, `view`).
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
   uv run main.py schema
   ```

3. Create assignment config from [data/example/config.toml](data/example/config.toml)

4. Run all stages:

   ```bash
   uv run main.py preprocess -c data/my-assignment/config.toml
   uv run main.py plagiarism -c data/my-assignment/config.toml
   uv run main.py grade -c data/my-assignment/config.toml
   uv run main.py score -c data/my-assignment/config.toml
   uv run main.py analyze -c data/my-assignment/config.toml
   ```

   Each stage runs in the order: plagiarism -> preprocess -> grade -> score -> analyze.

5. Run plagiarism detection only (optional):

   ```bash
   uv run main.py plagiarism -c data/my-assignment/config.toml
   ```

6. Run post-scoring meta analysis (optional):

   ```bash
   uv run main.py analyze -c data/my-assignment/config.toml
   ```

7. Audit reference notebook TODO/instruction mismatches (optional):

   ```bash
   uv run misc/reference_mismatch_audit.py \
      --notebook data/my-assignment/reference.ipynb
   ```

8. Aggregate plagiarism results across assignments (optional):

   ```bash
   uv run main.py plagiarism -c data/config.toml --aggregate -o data/plagiarism-report.txt
   ```

   `--config data/config.toml` runs plagiarism for every assignment
   listed in its `[[fetch.assignments]]` (code submissions via copydetect on
   extracted notebook code, text/report submissions via copydetect on
   processed markdown blended 95/5 with embedding similarity). `--aggregate`
   appends the cross-assignment report: pairwise
   deletion + logit transform + per-assignment z-score + Stouffer aggregation,
   plus an individual-level detector using per-assignment max similarity with
   Gumbel fitting. It outputs statistically significant pairs and students under
   the alphas configured in the root config's `[plagiarism]` section.
   Data source is `plagiarism/all_pairs.json` (full pair export).

## Layered Config

Config is layered: the course-level root config `data/config.toml` holds
persistent fetch/plagiarism state shared across assignments (course id, fetch
mode, plagiarism weights/alphas) plus the course's assignment list; each
`data/<name>/config.toml` holds grading/scoring and assignment-specific
overrides. Assignment values win per key; all paths resolve against the
assignment directory. Standalone assignment configs (no root config) work
exactly as before.

The root `[[fetch.assignments]]` list drives batch fetch and the plagiarism
aggregate: `fetch -c data/config.toml` fetches every listed entry in
one shot (per-entry `mode`/`out` win), and `plagiarism -c data/config.toml --aggregate` runs and aggregates exactly the listed
assignments.

```toml
# data/config.toml (root layer, gitignored)
[fetch]
course_id = 271218
mode = "attach"   # default for entries without their own

[[fetch.assignments]]
assignment_id = 2978557
out = "0-10-first-colab/raw"  # fetch dir; assignment root = its parent

# data/0-10-first-colab/config.toml (assignment layer)
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

- Example assignment config: [data/example/config.toml](data/example/config.toml)
- Example rubric: [rubrics/example_rubric.toml](rubrics/example_rubric.toml)
- Generic system prompt: [prompt/system.md](prompt/system.md)
- Reference mismatch audit script: [misc/reference_mismatch_audit.py](misc/reference_mismatch_audit.py)
- Plagiarism: `uv run main.py plagiarism -c data/config.toml --aggregate`
