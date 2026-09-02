# TATA

TATA stands for TATA-Assisted Teaching Assistant.

TATA is a configuration-driven grading pipeline for human TAs. It preprocesses submissions, runs plagiarism detection, performs LLM-based grading against a rubric, and generates score summaries.

## Core Features

- **Config-first architecture**: define assignment behavior in TOML, not ad-hoc scripts.
- **Config validation**: assignment/provider/rubric models are parsed and validated with pydantic models on load; `cli validate` checks a config end to end (rubric, prompts, provider, reference).
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

2. Validate an assignment config (rubric, prompts, provider, reference):

   ```bash
   uv run cli validate -c data/<course>/<assignment>/config.toml
   ```

   The bare `data/example/config.toml` copy source validates only at its
   destination depth (`data/<course>/<assignment>/`), where rubric/prompt
   paths resolve against `data/`.

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

7. Audit reference notebook TODO/instruction mismatches (optional; repo-dev
   utility — runs only from a source checkout, it is not part of the
   installed package):

   ```bash
   uv run misc/reference_mismatch_audit.py \
      --notebook data/my-assignment/reference.ipynb
   ```

8. Aggregate plagiarism results across assignments (optional):

   ```bash
   uv run main.py plagiarism -c data/<course>/config.toml --aggregate -o data/plagiarism-report.txt
   ```

   `--config data/<course>/config.toml` runs plagiarism for every assignment
   listed in that course config's `[[fetch.assignments]]` (code submissions
   via copydetect on extracted notebook code, text/report submissions via
   copydetect on processed markdown blended 95/5 with embedding similarity).
   `--aggregate` appends the cross-assignment report: pairwise
   deletion + logit transform + per-assignment z-score + Stouffer aggregation,
   plus an individual-level detector using per-assignment max similarity with
   Gumbel fitting. It outputs statistically significant pairs and students under
   the alphas configured in the config's `[plagiarism]` section.
   Data source is `plagiarism/all_pairs.json` (full pair export).
   The global config `data/config.toml` also works: with no assignment list
   of its own it runs over the discovered course configs
   (`data/*/config.toml`); use a course config for a per-course run.
   Likewise, `fetch --retry` scans the global plus every course config.

9. Edit one config value from the CLI (optional; comments and unrelated keys
   are preserved, and the result is validated against the same pydantic models
   the settings screen uses before writing):

   ```bash
   uv run main.py config set -c data/my-assignment/config.toml grading.max_parallel_tasks 4
   uv run main.py config set -c data/my-assignment/config.toml processing.remove_base64_images false
   ```

## Layered Config

Config is layered (three levels): the global base config `data/config.toml`
holds defaults shared across courses (`[fetch]` course_id, `[plagiarism]`
settings) but no assignment list; each course config
`data/<course>/config.toml` holds course-level fetch state plus the course's
assignment list (`[[fetch.assignments]]`); each
`data/<course>/<assignment>/config.toml` holds grading/scoring and
assignment-specific overrides. Assignment values win per key; all paths
resolve against the assignment directory. The legacy two-level layout
(global `data/config.toml` + assignment configs directly under it, with the
assignment list in the global file) still works as an abbreviation.

The course `[[fetch.assignments]]` list drives batch fetch and the plagiarism
aggregate: `fetch -c data/<course>/config.toml` fetches every listed entry in
one shot (fetch auto-collects every submission type — body text plus
attachments — for each listed entry; fetch output dirs are always
`<course dir>/<id>/raw`, derived from the entry id, never stored), and
`plagiarism -c data/<course>/config.toml --aggregate` runs and aggregates
exactly the listed assignments.

Fetch writes one raw item per student: a single-file submission stays flat
at `raw/<file>`; a multi-file student (e.g. text-box answer plus an
attachment) lands in `raw/<uid>/`. `preprocess` auto-detects the format of
each file (ipynb/html/txt/md/docx/pdf/image — scanned jpg/jpeg/png use Firecrawl
hosted OCR, needs FIRECRAWL_API_KEY) and merges a multi-file student into one
`processed/<uid>.md` with per-file `<!--- file: <name>, submitted: <stamp> -->` headers; single-file students stay unchanged. The global config `data/config.toml` carries
no assignment list: `plagiarism -c data/config.toml` falls back to the
discovered course configs (`data/*/config.toml`) and `fetch --retry` scans
the global plus every course config.

```toml
# data/config.toml (global base layer, gitignored)
[fetch]
course_id = 271218   # defaults merged under each course config

# data/271218/config.toml (course layer, gitignored)
[fetch]
course_id = 271218

[[fetch.assignments]]
id = 2978557

[[fetch.assignments]]
id = 2979509

# data/271218/2978557/config.toml (assignment layer; no [fetch] — the
# assignment id comes from the numeric dir name)
[grading]
rubric = "rubrics/0-10-first-colab.toml"
...
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
- Reference mismatch audit script (repo-dev utility; not installed):
  [misc/reference_mismatch_audit.py](misc/reference_mismatch_audit.py)
- Plagiarism: `uv run main.py plagiarism -c data/<course>/config.toml --aggregate`
